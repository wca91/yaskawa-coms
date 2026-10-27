"""Connection handling and the address-based API shared by all register
clients (Extended MEMOBUS and Modbus/TCP).

A protocol client only has to implement the primitives
(``_read_holding``, ``_write_holding``, ``_read_input``, ``_read_coils``,
``_read_input_relays``, ``_write_coil``, ``_write_coils``) and set the size limits;
everything else - splitting big requests, typed values, bit access -
lives here.
"""
from __future__ import annotations

import threading
from typing import Callable, List, Optional, Sequence, TypeVar, Union

from .address import Address, AddressLike, parse_address
from .datatypes import (
    float_to_words,
    int32_to_words,
    to_signed16,
    to_unsigned16,
    words_to_float,
    words_to_int32,
)
from .exceptions import (
    AddressError,
    CommunicationError,
    IllegalAddress,
    IllegalDataCount,
    ProtocolError,
    RemoteError,
)
from .transport import Transport, make_transport

T = TypeVar("T")
Value = Union[int, float, bool]

#: Highest coil / relay number. The coil number field is 16 bits, so coils
#: only reach MB04095F (word 4095, bit F). Verified on hardware.
_MAX_BIT = 0xFFFF


def _explain(exc: RemoteError, what: str) -> RemoteError:
    """Re-raise a controller error with a hint about the likely cause."""
    hints = {
        (IllegalAddress, "write"): "the controller only accepts writes inside its configured "
        "write range (check the connection's MSG-RCV write range settings)",
        (IllegalAddress, "read"): "the address is beyond the controller's register area",
        (IllegalDataCount, "read"): "the request may run past the end of the register area, "
        "or exceed the controller's per-request limit (set max_read_words lower)",
        (IllegalDataCount, "write"): "the request may run past the end of the register area, "
        "or exceed the controller's per-request limit (set max_write_words lower)",
    }
    kind = "read" if what.startswith("read") else "write"
    hint = hints.get((type(exc), kind))
    new = type(exc)(exc.function, exc.code)
    new.args = (f"{exc.args[0]} - {what}" + (f"; {hint}" if hint else ""),)
    return new


class BaseClient:
    """Owns the transport, the lock, reconnects and retries."""

    def __init__(
        self,
        host: str,
        port: int,
        *,
        transport: str = "tcp",
        timeout: float = 2.0,
        retries: int = 1,
        source_port: Optional[int] = None,
        source_ip: str = "",
    ):
        self.transport: Transport = make_transport(
            transport, host, port, timeout=timeout, source_port=source_port, source_ip=source_ip
        )
        self.retries = max(0, int(retries))
        self._lock = threading.RLock()

    # -- lifecycle -------------------------------------------------------
    @property
    def host(self) -> str:
        return self.transport.host

    @property
    def port(self) -> int:
        return self.transport.port

    @property
    def connected(self) -> bool:
        return self.transport.is_open

    def connect(self) -> "BaseClient":
        with self._lock:
            self.transport.open()
        return self

    def close(self) -> None:
        with self._lock:
            self.transport.close()

    def __enter__(self):
        return self.connect()

    def __exit__(self, *exc) -> None:
        self.close()

    def __repr__(self) -> str:
        return f"<{type(self).__name__} {self.transport.kind}://{self.host}:{self.port}>"

    # -- request/response with reconnect + retry --------------------------
    def _transact(self, exchange: Callable[[Transport], T]) -> T:
        """Run one request/response exchange.

        On a connection problem or garbled reply the socket is closed and the
        exchange is retried on a fresh connection up to ``retries`` times.
        Error replies from the controller (:class:`RemoteError`) are never
        retried.
        """
        with self._lock:
            attempts = self.retries + 1
            for attempt in range(attempts):
                try:
                    if not self.transport.is_open:
                        self.transport.open()
                    else:
                        self.transport.discard_input()
                    return exchange(self.transport)
                except (CommunicationError, ProtocolError):
                    self.transport.close()
                    if attempt == attempts - 1:
                        raise
            raise AssertionError("unreachable")


class RegisterClient(BaseClient):
    """Address-based API on top of the protocol primitives."""

    #: Largest register / bit counts per request. Subclasses override.
    max_read_words = 125
    max_write_words = 100
    max_read_bits = 2000
    max_write_bits = 800

    # -- primitives (implemented by each protocol) -----------------------
    def _read_holding(self, start: int, count: int) -> List[int]:
        raise NotImplementedError

    def _read_input(self, start: int, count: int) -> List[int]:
        raise NotImplementedError

    def _write_holding(self, start: int, values: Sequence[int]) -> None:
        raise NotImplementedError

    def _read_coils(self, start: int, count: int) -> List[bool]:
        raise NotImplementedError

    def _read_input_relays(self, start: int, count: int) -> List[bool]:
        raise NotImplementedError

    def _write_coil(self, number: int, on: bool) -> None:
        raise NotImplementedError

    def _write_coils(self, start: int, values: Sequence[bool]) -> None:
        raise NotImplementedError

    # -- raw word / bit access (automatic splitting) ---------------------
    @staticmethod
    def _check_range(start: int, count: int, top: int = 0xFFFF) -> None:
        if count < 1:
            raise ValueError("count must be at least 1")
        if start < 0 or start + count - 1 > top:
            raise AddressError(f"registers {start}..{start + count - 1} are out of range")

    def _chunked_read(self, fn, start: int, count: int, limit: int, top: int = 0xFFFF) -> list:
        self._check_range(start, count, top)
        out: list = []
        while count:
            n = min(count, limit)
            try:
                part = fn(start, n)
            except RemoteError as exc:
                raise _explain(exc, f"reading {n} values from {start}") from None
            if len(part) != n:
                raise ProtocolError(f"asked for {n} values, controller returned {len(part)}")
            out.extend(part)
            start += n
            count -= n
        return out

    def read_words(self, start: int, count: int = 1) -> List[int]:
        """Read ``count`` raw holding registers (MW) as unsigned 0..65535."""
        return self._chunked_read(self._read_holding, start, count, self.max_read_words)

    def read_input_words(self, start: int, count: int = 1) -> List[int]:
        """Read ``count`` raw input registers (IW) as unsigned 0..65535."""
        return self._chunked_read(self._read_input, start, count, self.max_read_words)

    def write_words(self, start: int, values: Sequence[int]) -> None:
        """Write holding registers (MW). Values may be -32768..65535."""
        words = [to_unsigned16(int(v)) for v in values]
        self._check_range(start, len(words))
        for i in range(0, len(words), self.max_write_words):
            chunk = words[i : i + self.max_write_words]
            try:
                self._write_holding(start + i, chunk)
            except RemoteError as exc:
                raise _explain(exc, f"writing {len(chunk)} words at {start + i}") from None

    def read_coils(self, start: int, count: int = 1) -> List[bool]:
        """Read coils (MB bits). Coil number = word * 16 + bit, so coils
        cover MB000000..MB04095F only (16-bit coil numbers)."""
        return self._chunked_read(self._read_coils, start, count, self.max_read_bits, _MAX_BIT)

    def read_input_relays(self, start: int, count: int = 1) -> List[bool]:
        """Read input relays (IB bits). Relay number = word * 16 + bit."""
        return self._chunked_read(self._read_input_relays, start, count, self.max_read_bits, _MAX_BIT)

    def write_coil(self, number: int, on: bool) -> None:
        """Set one coil (MB bit) with a single-coil write command. Unlike
        ``write("MB...")`` this is atomic, but only reaches MB04095F."""
        self._check_range(number, 1, _MAX_BIT)
        self._write_coil(number, bool(on))

    def write_coils(self, start: int, values: Sequence[bool]) -> None:
        """Set consecutive coils (MB bits)."""
        bits = [bool(v) for v in values]
        self._check_range(start, len(bits), _MAX_BIT)
        for i in range(0, len(bits), self.max_write_bits):
            self._write_coils(start + i, bits[i : i + self.max_write_bits])

    # -- bit helpers via word read-modify-write --------------------------
    def read_word_bit(self, word: int, bit: int) -> bool:
        """Read one bit of a holding register."""
        if not 0 <= bit <= 15:
            raise ValueError("bit must be 0..15")
        return bool(self.read_words(word, 1)[0] & (1 << bit))

    def write_word_bit(self, word: int, bit: int, on: bool) -> tuple:
        """Set or clear one bit of MW<word> by reading the word, changing the
        bit and writing it back. Returns ``(old_word, new_word)``.

        This is not atomic: if the PLC program changes another bit of the same
        word between the read and the write, that change is lost. Use
        :meth:`write_coil` when atomic bit writes are needed.
        """
        if not 0 <= bit <= 15:
            raise ValueError("bit must be 0..15")
        with self._lock:
            old = self.read_words(word, 1)[0]
            new = (old | (1 << bit)) if on else (old & ~(1 << bit) & 0xFFFF)
            if new != old:
                self.write_words(word, [new])
            return old, new

    # -- typed, address-based API ---------------------------------------
    def read(self, address: AddressLike, count: Optional[int] = None):
        """Read values by address string.

        ``read("MW00100")`` returns one value; ``read("MW00100", 10)`` returns a
        list of 10. The type follows the address:

        * ``MW`` / ``IW``: signed 16-bit int (as MPE720 shows it)
        * ``ML`` / ``IL``: signed 32-bit int (2 words each, low word first)
        * ``MF`` / ``IF``: float (2 words each, low word first)
        * ``MB`` / ``IB``: bool
        """
        addr = parse_address(address)
        n = 1 if count is None else int(count)
        values = self._read_typed(addr, n)
        return values[0] if count is None else values

    def _read_typed(self, addr: Address, n: int) -> List[Value]:
        reader = self.read_words if addr.area == "M" else self.read_input_words
        if addr.kind == "B":
            # Read the words that contain the bits, then pick the bits out.
            first = addr.coil
            last = first + n - 1
            words = reader(first // 16, last // 16 - first // 16 + 1)
            base = (first // 16) * 16
            return [bool(words[(b - base) // 16] & (1 << ((b - base) % 16))) for b in range(first, last + 1)]
        words = reader(addr.word, n * addr.width)
        if addr.kind == "W":
            return [to_signed16(w) for w in words]
        pairs = zip(words[0::2], words[1::2])
        if addr.kind == "L":
            return [words_to_int32(lo, hi) for lo, hi in pairs]
        return [words_to_float(lo, hi) for lo, hi in pairs]

    def write(self, address: AddressLike, value) -> None:
        """Write one value or a list of values by address string.

        ``write("MW00100", 5)``, ``write("MF21000", 8.68)``,
        ``write("ML00200", [1, -2])``, ``write("MB00100A", True)``.

        Bit writes (``MB``) read the word, change the bit and write the word
        back (see :meth:`write_word_bit`).
        """
        addr = parse_address(address)
        if not addr.writable:
            raise AddressError(f"{addr} is an input register and cannot be written")
        values = list(value) if isinstance(value, (list, tuple)) else [value]
        if not values:
            raise ValueError("nothing to write")

        if addr.kind == "B":
            self._write_bits_rmw(addr.coil, [bool(v) for v in values])
            return
        words: List[int] = []
        for v in values:
            if addr.kind == "W":
                words.append(to_unsigned16(int(v)))
            elif addr.kind == "L":
                words.extend(int32_to_words(int(v)))
            else:
                words.extend(float_to_words(float(v)))
        self.write_words(addr.word, words)

    def _write_bits_rmw(self, first: int, bits: List[bool]) -> None:
        with self._lock:
            last = first + len(bits) - 1
            w0, w1 = first // 16, last // 16
            words = self.read_words(w0, w1 - w0 + 1)
            new = list(words)
            for i, on in enumerate(bits):
                b = first + i - w0 * 16
                if on:
                    new[b // 16] |= 1 << (b % 16)
                else:
                    new[b // 16] &= ~(1 << (b % 16)) & 0xFFFF
            if new != words:
                self.write_words(w0, new)

    # Convenience aliases --------------------------------------------------
    def read_float(self, address: AddressLike) -> float:
        """Read one MF/IF float, e.g. ``read_float("MF21000")``. A plain int
        is taken as the MW word number."""
        if isinstance(address, int):
            lo, hi = self.read_words(address, 2)
            return words_to_float(lo, hi)
        addr = parse_address(address)
        return float(self.read(Address(addr.area, "F", addr.word)))

    def write_float(self, address: AddressLike, value: float) -> None:
        if isinstance(address, int):
            self.write_words(address, list(float_to_words(value)))
            return
        addr = parse_address(address)
        self.write(Address(addr.area, "F", addr.word), float(value))
