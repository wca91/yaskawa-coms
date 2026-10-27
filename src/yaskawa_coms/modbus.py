"""Modbus/TCP client (standard Modbus Application Protocol, big-endian).

MP2000 218IF-01/02 modules convert Modbus/TCP to MEMOBUS internally
(manual p.2-9, p.4-30), so a PC can talk to them as a normal Modbus/TCP
device. Register mapping when the controller is the slave:

    holding registers -> MW    input registers -> IW
    coils             -> MB    discrete inputs -> IB

Supported function codes and sizes (manual p.4-30):
01H/02H 1-2000 bits, 03H/04H 1-125 words, 05H, 06H, 0FH 1-800 bits,
10H 1-100 words.

Frame: MBAP header ``transaction(2) protocol=0(2) length(2) unit(1)`` + PDU.
The manual says to use unit ID 0 for an MP2000 controller.
"""
from __future__ import annotations

import struct
from typing import List, Optional, Sequence

from .base import RegisterClient
from .datatypes import bytes_to_words_be, pack_bits, unpack_bits, words_to_bytes_be
from .exceptions import ProtocolError, remote_error
from .transport import Transport

READ_COILS = 0x01
READ_DISCRETE_INPUTS = 0x02
READ_HOLDING = 0x03
READ_INPUT = 0x04
WRITE_COIL = 0x05
WRITE_REGISTER = 0x06
WRITE_COILS = 0x0F
WRITE_REGISTERS = 0x10

MBAP_LEN = 7


def build_adu(transaction: int, unit: int, pdu: bytes) -> bytes:
    """Prefix a PDU with the MBAP header."""
    return struct.pack(">HHHB", transaction & 0xFFFF, 0, len(pdu) + 1, unit & 0xFF) + pdu


def pdu_read(function: int, start: int, count: int) -> bytes:
    return struct.pack(">BHH", function, start, count)


def pdu_write_register(address: int, value: int) -> bytes:
    return struct.pack(">BHH", WRITE_REGISTER, address, value & 0xFFFF)


def pdu_write_registers(start: int, words: Sequence[int]) -> bytes:
    data = words_to_bytes_be(words)
    return struct.pack(">BHHB", WRITE_REGISTERS, start, len(words), len(data)) + data


def pdu_write_coil(number: int, on: bool) -> bytes:
    return struct.pack(">BHH", WRITE_COIL, number, 0xFF00 if on else 0x0000)


def pdu_write_coils(start: int, bits: Sequence[bool]) -> bytes:
    data = pack_bits(bits)
    return struct.pack(">BHHB", WRITE_COILS, start, len(bits), len(data)) + data


def read_adu(transport: Transport) -> bytes:
    header = transport.recv_exact(MBAP_LEN)
    _, protocol, length, _ = struct.unpack(">HHHB", header)
    if protocol != 0:
        raise ProtocolError(f"not a Modbus/TCP frame (protocol id {protocol})")
    if not 2 <= length <= 254:
        raise ProtocolError(f"invalid MBAP length {length}")
    return header + transport.recv_exact(length - 1)


def parse_adu(adu: bytes, transaction: int, function: int) -> bytes:
    """Validate a reply and return the PDU data after the function code."""
    tid, _, _, _ = struct.unpack_from(">HHHB", adu)
    if tid != transaction & 0xFFFF:
        raise ProtocolError(f"transaction id mismatch: sent {transaction}, got {tid}")
    fc = adu[MBAP_LEN]
    if fc == function | 0x80:
        raise remote_error(function, adu[MBAP_LEN + 1] if len(adu) > MBAP_LEN + 1 else 0)
    if fc != function:
        raise ProtocolError(f"function code mismatch: sent {function:02X}H, got {fc:02X}H")
    return adu[MBAP_LEN + 1 :]


class ModbusTcpClient(RegisterClient):
    """Modbus/TCP master.

    Args:
        host: controller IP address.
        port: the Local Port of the controller's Modbus/TCP connection, as
            set in the 218 transmission settings. Required: there is no
            default because the 218 does not have to use 502.
        unit_id: Modbus unit identifier (0 for MP2000 per the manual).
        transport: ``"tcp"`` (standard). ``"udp"`` sends the same frames
            over UDP for devices that support it.
        timeout, retries, source_port: see :class:`MemobusClient`.

    Example::

        with ModbusTcpClient("192.168.1.1", 10002) as plc:
            plc.read("MW00100", 4)
            plc.write_coil(1600, True)   # MB00100 bit 0
    """

    max_read_words = 125
    max_write_words = 100
    max_read_bits = 2000
    max_write_bits = 800

    def __init__(
        self,
        host: str,
        port: int,
        *,
        unit_id: int = 0,
        transport: str = "tcp",
        timeout: float = 2.0,
        retries: int = 1,
        source_port: Optional[int] = None,
        source_ip: str = "",
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
        self.unit_id = unit_id
        self._transaction = 0

    def request(self, pdu: bytes) -> bytes:
        """Send a raw Modbus PDU and return the reply data after the function code."""
        function = pdu[0]

        def exchange(t: Transport) -> bytes:
            self._transaction = (self._transaction + 1) & 0xFFFF
            tid = self._transaction
            t.send(build_adu(tid, self.unit_id, pdu))
            return parse_adu(read_adu(t), tid, function)

        return self._transact(exchange)

    # -- primitives --------------------------------------------------------
    def _read_registers(self, function: int, start: int, count: int) -> List[int]:
        data = self.request(pdu_read(function, start, count))
        if not data or data[0] != 2 * count or len(data) < 1 + 2 * count:
            raise ProtocolError(f"asked for {count} registers, got a {len(data)}-byte reply")
        return bytes_to_words_be(data[1 : 1 + 2 * count])

    def _read_bits(self, function: int, start: int, count: int) -> List[bool]:
        data = self.request(pdu_read(function, start, count))
        nbytes = (count + 7) // 8
        if not data or data[0] != nbytes or len(data) < 1 + nbytes:
            raise ProtocolError(f"asked for {count} bits, got a {len(data)}-byte reply")
        return unpack_bits(data[1 : 1 + nbytes], count)

    def _read_holding(self, start: int, count: int) -> List[int]:
        return self._read_registers(READ_HOLDING, start, count)

    def _read_input(self, start: int, count: int) -> List[int]:
        return self._read_registers(READ_INPUT, start, count)

    def _write_holding(self, start: int, values: Sequence[int]) -> None:
        if len(values) == 1:
            self.request(pdu_write_register(start, values[0]))
        else:
            self.request(pdu_write_registers(start, values))

    def _read_coils(self, start: int, count: int) -> List[bool]:
        return self._read_bits(READ_COILS, start, count)

    def _read_input_relays(self, start: int, count: int) -> List[bool]:
        return self._read_bits(READ_DISCRETE_INPUTS, start, count)

    def _write_coil(self, number: int, on: bool) -> None:
        self.request(pdu_write_coil(number, on))

    def _write_coils(self, start: int, values: Sequence[bool]) -> None:
        self.request(pdu_write_coils(start, values))
