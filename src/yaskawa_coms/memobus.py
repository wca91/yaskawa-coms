"""Extended MEMOBUS over Ethernet (TCP or UDP), binary mode.

Message layout (MP2000 Communication Module manual SIEP C880700 04L,
Appendix B p.A-15..A-23 and the sample C programs in Appendix E/F)::

    218 header (12 bytes)
      [0]    command type   11H request / 19H response / 12H general message
      [1]    serial number  00H..FFH, echoed in the response
      [2]    dest channel   0 when the PC is the master
      [3]    source channel 0 when the PC is the master
      [4-5]  not used (0)
      [6-7]  total length   header + everything after it, LITTLE-endian
      [8-11] not used (0)
    MEMOBUS part
      [12-13] length        bytes from MFC to the end, little-endian
      [14]    MFC           always 20H
      [15]    SFC           function code (error reply: SFC + 80H)
      [16]    CPU no.       high nibble = destination CPU, low = source CPU
      [17..]  function-specific fields

Byte order of the function-specific fields:

* Extended codes 09H/0AH/0BH/0DH/0EH: a spare byte after the CPU number,
  then little-endian 16-bit fields.
* Standard codes 01H-06H/08H/0FH/10H: no spare byte, big-endian 16-bit
  fields, no byte-count field (as in the Appendix F sample program, e.g.
  ``data_size = rbuf[19] << 8 | rbuf[20]`` for 03H).

All of these were verified on a real MP-series controller (2026-10-27).

A request larger than the controller accepts gets error 03 for reads, but an
oversized WRITE frame may get no reply at all and the controller then drops
the connection - so keep ``max_write_words`` within the controller's limit.
"""
from __future__ import annotations

import struct
from typing import List, Optional, Sequence, Tuple

from .base import RegisterClient
from .datatypes import (
    bytes_to_words_be,
    bytes_to_words_le,
    pack_bits,
    unpack_bits,
    words_to_bytes_be,
    words_to_bytes_le,
)
from .exceptions import ProtocolError, remote_error
from .transport import Transport

# 218 header command types
CMD_REQUEST = 0x11
CMD_GENERAL = 0x12
CMD_RESPONSE = 0x19
MFC = 0x20

HEADER_LEN = 12

# Function codes (SFC)
READ_COILS = 0x01
READ_INPUT_RELAYS = 0x02
READ_HOLDING = 0x03
READ_INPUT = 0x04
WRITE_COIL = 0x05
WRITE_REGISTER = 0x06
LOOPBACK = 0x08
EXT_READ_HOLDING = 0x09
EXT_READ_INPUT = 0x0A
EXT_WRITE_HOLDING = 0x0B
EXT_READ_SCATTERED = 0x0D
EXT_WRITE_SCATTERED = 0x0E
WRITE_COILS = 0x0F
WRITE_REGISTERS = 0x10

#: Words per request (MP2000 manual tables p.4-25 and p.5-25). Some
#: controllers differ: one tested unit took 1020 words per read and 1019 per
#: write, so ``max_read_words`` / ``max_write_words`` can override these.
MODULE_LIMITS = {
    "218IF-01": {"read": 508, "write": 507, "scattered_read": 508, "scattered_write": 254},
    "218IF-02": {"read": 2044, "write": 2043, "scattered_read": 2044, "scattered_write": 1022},
}


# ---------------------------------------------------------------------------
# Frame building / parsing (pure functions, no I/O)
# ---------------------------------------------------------------------------
def cpu_byte(dest_cpu: int, source_cpu: int = 0) -> int:
    """Build the CPU-number byte: destination in the high nibble.

    For an MP2000 controller use ``dest_cpu=1`` (manual p.4-47), i.e. 0x10.
    """
    if not (0 <= dest_cpu <= 15 and 0 <= source_cpu <= 15):
        raise ValueError("CPU numbers must be 0..15")
    return (dest_cpu << 4) | source_cpu


def build_frame(serial: int, pdu: bytes, command: int = CMD_REQUEST) -> bytes:
    """Wrap a MEMOBUS PDU (MFC, SFC, CPU, fields...) in the length field and
    the 218 header."""
    memobus = struct.pack("<H", len(pdu)) + pdu
    total = HEADER_LEN + len(memobus)
    header = struct.pack("<BBBBHHI", command, serial & 0xFF, 0, 0, 0, total, 0)
    return header + memobus


def _pdu(sfc: int, cpu: int, fields: bytes) -> bytes:
    return bytes((MFC, sfc, cpu)) + fields


# Extended codes: spare byte + little-endian fields
def pdu_ext_read(sfc: int, cpu: int, start: int, count: int) -> bytes:
    """09H (holding / MW) or 0AH (input / IW) extended read."""
    return _pdu(sfc, cpu, struct.pack("<BHH", 0, start, count))


def pdu_ext_write(cpu: int, start: int, words: Sequence[int]) -> bytes:
    """0BH extended write of consecutive holding registers."""
    return _pdu(EXT_WRITE_HOLDING, cpu, struct.pack("<BHH", 0, start, len(words)) + words_to_bytes_le(words))


def pdu_ext_read_scattered(cpu: int, addresses: Sequence[int]) -> bytes:
    """0DH: read non-consecutive holding registers."""
    return _pdu(
        EXT_READ_SCATTERED,
        cpu,
        struct.pack("<BH", 0, len(addresses)) + words_to_bytes_le(addresses),
    )


def pdu_ext_write_scattered(cpu: int, items: Sequence[Tuple[int, int]]) -> bytes:
    """0EH: write non-consecutive holding registers, ``[(address, value), ...]``."""
    body = b"".join(struct.pack("<HH", a, v & 0xFFFF) for a, v in items)
    return _pdu(EXT_WRITE_SCATTERED, cpu, struct.pack("<BH", 0, len(items)) + body)


# Standard codes: no spare byte, big-endian fields (Appendix F sample)
def pdu_std_read(sfc: int, cpu: int, start: int, count: int) -> bytes:
    """01H/02H/03H/04H."""
    return _pdu(sfc, cpu, struct.pack(">HH", start, count))


def pdu_write_register(cpu: int, address: int, value: int) -> bytes:
    """06H."""
    return _pdu(WRITE_REGISTER, cpu, struct.pack(">HH", address, value & 0xFFFF))


def pdu_write_registers(cpu: int, start: int, words: Sequence[int]) -> bytes:
    """10H (no byte-count field in Extended MEMOBUS)."""
    return _pdu(WRITE_REGISTERS, cpu, struct.pack(">HH", start, len(words)) + words_to_bytes_be(words))


def pdu_write_coil(cpu: int, number: int, on: bool) -> bytes:
    """05H: ON = FF00H, OFF = 0000H."""
    return _pdu(WRITE_COIL, cpu, struct.pack(">HBB", number, 0xFF if on else 0x00, 0))


def pdu_write_coils(cpu: int, start: int, bits: Sequence[bool]) -> bytes:
    """0FH."""
    return _pdu(WRITE_COILS, cpu, struct.pack(">HH", start, len(bits)) + pack_bits(bits))


def pdu_loopback(cpu: int, data: int = 0xAAAA) -> bytes:
    """08H: test code 0000H + test data."""
    return _pdu(LOOPBACK, cpu, struct.pack(">HH", 0, data))


def parse_response(frame: bytes, serial: int, sfc: int) -> bytes:
    """Validate a response frame and return the bytes after the CPU number.

    Raises :class:`ProtocolError` for malformed / mismatched frames and a
    :class:`RemoteError` subclass for an error reply.
    """
    if len(frame) < HEADER_LEN + 5:
        raise ProtocolError(f"response too short ({len(frame)} bytes)")
    command, rx_serial = frame[0], frame[1]
    total = struct.unpack_from("<H", frame, 6)[0]
    if command != CMD_RESPONSE:
        raise ProtocolError(f"expected response type 19H, got {command:02X}H")
    if rx_serial != serial & 0xFF:
        raise ProtocolError(f"serial number mismatch: sent {serial & 0xFF}, got {rx_serial}")
    if total != len(frame):
        raise ProtocolError(f"length field says {total} bytes, frame has {len(frame)}")
    length = struct.unpack_from("<H", frame, 12)[0]
    if length != len(frame) - 14:
        raise ProtocolError(f"MEMOBUS length field {length} does not match frame")
    mfc, rx_sfc = frame[14], frame[15]
    if mfc != MFC:
        raise ProtocolError(f"expected MFC 20H, got {mfc:02X}H")
    if rx_sfc == (sfc | 0x80):
        code = frame[17] if len(frame) > 17 else 0
        raise remote_error(sfc, code)
    if rx_sfc != sfc:
        raise ProtocolError(f"function code mismatch: sent {sfc:02X}H, got {rx_sfc:02X}H")
    return frame[17:]


def decode_ext_read(payload: bytes, count: int) -> List[int]:
    """Decode a 09H/0AH/0DH reply payload: spare, count (LE), data (LE)."""
    if len(payload) < 3:
        raise ProtocolError("read reply too short")
    n = struct.unpack_from("<H", payload, 1)[0]
    data = payload[3:]
    if n != count or len(data) < 2 * n:
        raise ProtocolError(f"asked for {count} registers, reply holds {n} ({len(data)} bytes)")
    return bytes_to_words_le(data[: 2 * n])


def decode_std_read(payload: bytes, count: int) -> List[int]:
    """Decode a 03H/04H reply payload: data words, big-endian."""
    if len(payload) < 2 * count:
        raise ProtocolError(f"asked for {count} registers, reply has {len(payload)} bytes")
    return bytes_to_words_be(payload[: 2 * count])


def read_frame(transport: Transport) -> bytes:
    """Read one complete Extended MEMOBUS frame from the transport."""
    header = transport.recv_exact(HEADER_LEN)
    total = struct.unpack_from("<H", header, 6)[0]
    if total < HEADER_LEN or total > 65535:
        raise ProtocolError(f"invalid total length {total} in 218 header")
    return header + transport.recv_exact(total - HEADER_LEN)


# ---------------------------------------------------------------------------
# Client
# ---------------------------------------------------------------------------
class MemobusClient(RegisterClient):
    """Extended MEMOBUS master (PC asks, controller answers).

    Args:
        host: controller IP address.
        port: the controller's "Local Port" from its 218IF connection settings.
            There is no fixed default port.
        transport: ``"tcp"`` or ``"udp"`` - must match the connection setting.
        cpu: destination CPU number (1 for MP2000 controllers). The CPU byte
            sent is ``cpu << 4``.
        module: ``"218IF-01"`` (limits 508/507 words, the safe default) or
            ``"218IF-02"`` (2044/2043 words). Large requests are split
            automatically.
        max_read_words, max_write_words: override the module's per-request
            limits (e.g. 1020 / 1019 for controllers that take that much).
        timeout: seconds to wait for a reply.
        retries: how many times to reconnect and resend after a connection
            problem.
        source_port: bind this local port (match the controller's "Node Port"
            if it isn't 0).

    Example::

        with MemobusClient("192.168.1.1", 10001) as plc:
            plc.read("MW00100", 10)
            plc.write("MF21000", 8.68)
    """

    def __init__(
        self,
        host: str,
        port: int,
        *,
        transport: str = "tcp",
        cpu: int = 1,
        module: str = "218IF-01",
        timeout: float = 2.0,
        retries: int = 1,
        source_port: Optional[int] = None,
        source_ip: str = "",
        max_read_words: Optional[int] = None,
        max_write_words: Optional[int] = None,
    ):
        super().__init__(
            host,
            port,
            transport=transport,
            timeout=timeout,
            retries=retries,
            source_port=source_port,
            source_ip=source_ip,
        )
        try:
            limits = MODULE_LIMITS[module.upper()]
        except KeyError:
            raise ValueError(f"module must be one of {sorted(MODULE_LIMITS)}") from None
        self.module = module.upper()
        self.cpu_no = cpu_byte(cpu)
        self.max_read_words = int(max_read_words or limits["read"])
        self.max_write_words = int(max_write_words or limits["write"])
        if not (1 <= self.max_read_words <= 2044 and 1 <= self.max_write_words <= 2043):
            raise ValueError("max_read_words must be 1..2044 and max_write_words 1..2043")
        self.max_scattered_read = limits["scattered_read"]
        self.max_scattered_write = limits["scattered_write"]
        self._serial = 0xFF  # first request uses 00H, as the manual recommends

    # -- core exchange ---------------------------------------------------
    def _next_serial(self) -> int:
        self._serial = (self._serial + 1) & 0xFF
        return self._serial

    def request(self, pdu: bytes) -> bytes:
        """Send a raw MEMOBUS PDU (``MFC SFC CPU ...``) and return the reply
        payload after the CPU byte. Useful for function codes not wrapped below."""
        sfc = pdu[1]

        def exchange(t: Transport) -> bytes:
            serial = self._next_serial()
            t.send(build_frame(serial, pdu))
            return parse_response(read_frame(t), serial, sfc)

        return self._transact(exchange)

    # -- primitives used by the address API (extended codes)
    def _read_holding(self, start: int, count: int) -> List[int]:
        return decode_ext_read(self.request(pdu_ext_read(EXT_READ_HOLDING, self.cpu_no, start, count)), count)

    def _read_input(self, start: int, count: int) -> List[int]:
        return decode_ext_read(self.request(pdu_ext_read(EXT_READ_INPUT, self.cpu_no, start, count)), count)

    def _write_holding(self, start: int, values: Sequence[int]) -> None:
        payload = self.request(pdu_ext_write(self.cpu_no, start, values))
        if len(payload) >= 5:
            _, rx_start, rx_count = struct.unpack_from("<BHH", payload)
            if (rx_start, rx_count) != (start, len(values)):
                raise ProtocolError(
                    f"write echo mismatch: sent {start}/{len(values)}, got {rx_start}/{rx_count}"
                )

    # -- coils / relays (standard codes; coil = word * 16 + bit) --------
    def _read_coils(self, start: int, count: int) -> List[bool]:
        return unpack_bits(self.request(pdu_std_read(READ_COILS, self.cpu_no, start, count)), count)

    def _read_input_relays(self, start: int, count: int) -> List[bool]:
        return unpack_bits(self.request(pdu_std_read(READ_INPUT_RELAYS, self.cpu_no, start, count)), count)

    def _write_coil(self, number: int, on: bool) -> None:
        self.request(pdu_write_coil(self.cpu_no, number, on))

    def _write_coils(self, start: int, values: Sequence[bool]) -> None:
        self.request(pdu_write_coils(self.cpu_no, start, values))

    # -- extra Extended MEMOBUS functions --------------------------------
    def read_scattered(self, addresses: Sequence[int]) -> List[int]:
        """Read non-consecutive MW registers in one request (0DH).

        ``read_scattered([100, 205, 3000])`` -> ``[MW100, MW205, MW3000]``.
        """
        out: List[int] = []
        addrs = list(addresses)
        for i in range(0, len(addrs), self.max_scattered_read):
            part = addrs[i : i + self.max_scattered_read]
            out += decode_ext_read(self.request(pdu_ext_read_scattered(self.cpu_no, part)), len(part))
        return out

    def write_scattered(self, items) -> None:
        """Write non-consecutive MW registers in one request (0EH).

        Accepts a dict ``{100: 1, 205: 2}`` or a list of ``(address, value)``.
        """
        pairs = list(items.items()) if isinstance(items, dict) else list(items)
        for i in range(0, len(pairs), self.max_scattered_write):
            self.request(pdu_ext_write_scattered(self.cpu_no, pairs[i : i + self.max_scattered_write]))

    def read_holding_std(self, start: int, count: int) -> List[int]:
        """Read holding registers with the standard 03H code (max 125)."""
        return decode_std_read(self.request(pdu_std_read(READ_HOLDING, self.cpu_no, start, count)), count)

    def read_input_std(self, start: int, count: int) -> List[int]:
        """Read input registers with the standard 04H code (max 125)."""
        return decode_std_read(self.request(pdu_std_read(READ_INPUT, self.cpu_no, start, count)), count)

    def write_register_std(self, address: int, value: int) -> None:
        """Write one holding register with the standard 06H code."""
        self.request(pdu_write_register(self.cpu_no, address, value))

    def write_registers_std(self, start: int, values: Sequence[int]) -> None:
        """Write holding registers with the standard 10H code (max 100)."""
        self.request(pdu_write_registers(self.cpu_no, start, values))

    def loopback(self, data: int = 0xAAAA) -> bool:
        """08H loopback test. Returns True when the controller echoes the data."""
        payload = self.request(pdu_loopback(self.cpu_no, data))
        return payload[:4] == struct.pack(">HH", 0, data)
