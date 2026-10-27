"""Non-procedure ("no protocol") communication.

The controller's MSG-SND sends the contents of consecutive MW registers as
raw bytes; MSG-RCV stores whatever arrives into MW registers. There is no
header, no function code and NO RESPONSE (manual p.4-31, Appendix D).

* Word mode (PRO-TYP = 2): each MW register is sent low byte first.
* Byte mode (PRO-TYP = 3): one byte per register.
* ASCII code setting: every byte travels as two ASCII hex characters
  (12H -> "12").
* Size: 1 to 254 words (217IF serial) / 1 to 510 words (218IF Ethernet).

TCP is a byte stream, so two quick sends can arrive glued together or split.
The manual recommends a send interval of 1 s or more, or building your own
command/response scheme on top. :meth:`receive_words` with an explicit
``count`` reads exactly that many words, which avoids the problem when the
message size is fixed.
"""
from __future__ import annotations

import binascii
from typing import List, Optional, Sequence

from .datatypes import bytes_to_words_le, to_unsigned16, words_to_bytes_le
from .exceptions import ProtocolError
from .transport import make_transport

MAX_WORDS = 510


class NonProcedureClient:
    """Send and receive raw MW register data.

    Args:
        host, port: the controller's IP and the port of its non-procedure
            connection.
        transport: ``"tcp"`` or ``"udp"``.
        code: ``"binary"`` or ``"ascii"`` - must match the connection's
            code setting.
        timeout: seconds to wait when receiving.
        source_port: bind this local port (match the controller's Node Port).

    Example::

        with NonProcedureClient("192.168.1.1", 10003) as link:
            link.send_words([1, 2, 3])        # lands in MW registers via MSG-RCV
            data = link.receive_words(10)     # 10 words sent by MSG-SND
    """

    def __init__(
        self,
        host: str,
        port: int,
        *,
        transport: str = "tcp",
        code: str = "binary",
        timeout: float = 2.0,
        source_port: Optional[int] = None,
        source_ip: str = "",
    ):
        if code not in ("binary", "ascii"):
            raise ValueError("code must be 'binary' or 'ascii'")
        self.code = code
        self.transport = make_transport(
            transport, host, port, timeout=timeout, source_port=source_port, source_ip=source_ip
        )

    # -- lifecycle -------------------------------------------------------
    def connect(self) -> "NonProcedureClient":
        self.transport.open()
        return self

    def close(self) -> None:
        self.transport.close()

    def __enter__(self):
        return self.connect()

    def __exit__(self, *exc) -> None:
        self.close()

    def _ensure_open(self) -> None:
        if not self.transport.is_open:
            self.transport.open()

    # -- encoding --------------------------------------------------------
    def _encode(self, data: bytes) -> bytes:
        return binascii.hexlify(data).upper() if self.code == "ascii" else data

    def _decode(self, data: bytes) -> bytes:
        if self.code != "ascii":
            return data
        try:
            return binascii.unhexlify(data)
        except (binascii.Error, ValueError) as exc:
            raise ProtocolError(f"invalid ASCII hex data: {data[:40]!r}") from exc

    def _wire_size(self, nbytes: int) -> int:
        return nbytes * 2 if self.code == "ascii" else nbytes

    # -- sending ---------------------------------------------------------
    def send_words(self, values: Sequence[int]) -> None:
        """Send register values (word mode). Values may be -32768..65535."""
        words = [to_unsigned16(int(v)) for v in values]
        if not 1 <= len(words) <= MAX_WORDS:
            raise ValueError(f"non-procedure messages hold 1..{MAX_WORDS} words")
        self.send_bytes(words_to_bytes_le(words))

    def send_bytes(self, data: bytes) -> None:
        """Send raw bytes (byte mode, or your own message format)."""
        if not data:
            raise ValueError("nothing to send")
        self._ensure_open()
        self.transport.send(self._encode(bytes(data)))

    # -- receiving -------------------------------------------------------
    def receive_bytes(self, count: Optional[int] = None) -> bytes:
        """Receive data.

        With ``count``: wait for exactly ``count`` data bytes.
        Without: return the next message / TCP chunk that arrives.
        """
        self._ensure_open()
        if count is None:
            raw = self.transport.recv_some()
            if self.code == "ascii" and len(raw) % 2:
                raw += self.transport.recv_exact(1)
        else:
            raw = self.transport.recv_exact(self._wire_size(count))
        return self._decode(raw)

    def receive_words(self, count: Optional[int] = None) -> List[int]:
        """Receive register values (word mode, low byte first)."""
        data = self.receive_bytes(None if count is None else 2 * count)
        if count is None and len(data) % 2:
            data += self.receive_bytes(1)
        return bytes_to_words_le(data)
