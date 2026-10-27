"""Byte-for-byte checks against the manuals and values verified on a real PLC."""
import struct

import pytest

from yaskawa_coms import memobus as mb
from yaskawa_coms import modbus as mt
from yaskawa_coms.exceptions import IllegalAddress, IllegalFunction, ProtocolError, RemoteError


# --- MP2000 manual, p.A-37 (Appendix E sample program) and p.4-47 ---------
MANUAL_FC09_REQUEST = bytes.fromhex(
    "11 00 00 00 00 00 16 00 00 00 00 00"  # 218 header, total length 22
    "08 00"                                # MEMOBUS length 8
    "20 09 10 00"                          # MFC, SFC 09, CPU 0x10, spare
    "00 00 0A 00"                          # start MW0, count 10
)


def test_fc09_request_matches_manual():
    pdu = mb.pdu_ext_read(mb.EXT_READ_HOLDING, mb.cpu_byte(1), 0, 10)
    assert mb.build_frame(0, pdu) == MANUAL_FC09_REQUEST


def _fc09_response(serial, words, cpu=0x01):
    data = b"".join(struct.pack("<H", w) for w in words)
    pdu = bytes((0x20, 0x09, cpu, 0x00)) + struct.pack("<H", len(words)) + data
    return mb.build_frame(serial, pdu, command=mb.CMD_RESPONSE)


def test_fc09_response_layout_matches_manual():
    words = list(range(10))
    frame = _fc09_response(0, words)
    # Manual: 40 bytes, total length 0x28, MEMOBUS length 0x1A, count at 18-19,
    # data from byte 20.
    assert len(frame) == 40
    assert frame[6:8] == b"\x28\x00"
    assert frame[12:14] == b"\x1a\x00"
    assert frame[18:20] == b"\x0a\x00"
    payload = mb.parse_response(frame, 0, 0x09)
    assert mb.decode_ext_read(payload, 10) == words


def test_error_response_maps_to_exception():
    frame = mb.build_frame(5, bytes((0x20, 0x89, 0x01, 0x02)), command=mb.CMD_RESPONSE)
    with pytest.raises(IllegalAddress) as info:
        mb.parse_response(frame, 5, 0x09)
    assert info.value.code == 2 and info.value.function == 0x09
    frame = mb.build_frame(5, bytes((0x20, 0x89, 0x01, 0x01)), command=mb.CMD_RESPONSE)
    with pytest.raises(IllegalFunction):
        mb.parse_response(frame, 5, 0x09)
    frame = mb.build_frame(5, bytes((0x20, 0x89, 0x01, 0x7F)), command=mb.CMD_RESPONSE)
    with pytest.raises(RemoteError):
        mb.parse_response(frame, 5, 0x09)


@pytest.mark.parametrize(
    "mutate, message",
    [
        (lambda f: bytes([0x11]) + f[1:], "type"),
        (lambda f: f[:1] + bytes([7]) + f[2:], "serial"),
        (lambda f: f[:15] + bytes([0x0B]) + f[16:], "function"),
        (lambda f: f[:-1], "length"),
    ],
)
def test_bad_responses_are_rejected(mutate, message):
    frame = mutate(_fc09_response(3, [1, 2]))
    with pytest.raises(ProtocolError, match=message):
        mb.parse_response(frame, 3, 0x09)


def test_fc0b_write_layout():
    pdu = mb.pdu_ext_write(0x10, 0x1234, [0xABCD, 0x0001])
    # MFC SFC CPU spare | start L,H | count L,H | data L,H ...
    assert pdu == bytes.fromhex("20 0B 10 00 34 12 02 00 CD AB 01 00")


def test_scattered_layouts():
    # 0DH: spare, count, addresses (Appendix B (11): length 06H + n*2)
    pdu = mb.pdu_ext_read_scattered(0x10, [1, 0x0203])
    assert pdu == bytes.fromhex("20 0D 10 00 02 00 01 00 03 02")
    assert len(struct.pack("<H", len(pdu))) == 2 and len(pdu) == 6 + 2 * 2
    # 0EH: spare, count, (address, value) pairs (length 06H + n*4)
    pdu = mb.pdu_ext_write_scattered(0x10, [(1, 2)])
    assert pdu == bytes.fromhex("20 0E 10 00 01 00 01 00 02 00")
    assert len(pdu) == 6 + 4


def test_standard_codes_follow_appendix_f():
    # Appendix F reads the 03H count as rbuf[19] << 8 | rbuf[20]: big-endian,
    # no spare byte, MEMOBUS length 7.
    frame = mb.build_frame(0, mb.pdu_std_read(mb.READ_HOLDING, 0x10, 0x0102, 0x0003))
    assert frame[12:14] == b"\x07\x00"
    assert frame[17:21] == bytes.fromhex("01 02 00 03")
    assert (frame[19] << 8) | frame[20] == 3


# --- Float layout verified on a real controller --------------------------
def test_float_conversion_matches_plc():
    from yaskawa_coms.datatypes import float_to_words, words_to_float

    # PLC-verified: MW21000=0xE4F7, MW21001=0x410A -> MF21000 = 8.6809
    assert words_to_float(0xE4F7, 0x410A) == pytest.approx(8.6809, abs=1e-4)
    for v in (0.0, -1.5, 8.6809, 123456.75):
        assert words_to_float(*float_to_words(v)) == pytest.approx(v, rel=1e-6)


# --- Modbus/TCP: standard example from the Modbus spec --------------------
def test_modbus_read_holding_adu():
    adu = mt.build_adu(1, 0x11, mt.pdu_read(mt.READ_HOLDING, 0x006B, 3))
    assert adu == bytes.fromhex("00 01 00 00 00 06 11 03 00 6B 00 03")


def test_modbus_write_registers_adu():
    adu = mt.build_adu(2, 0, mt.pdu_write_registers(1, [0x000A, 0x0102]))
    assert adu == bytes.fromhex("00 02 00 00 00 0B 00 10 00 01 00 02 04 00 0A 01 02")


def test_modbus_exception_reply():
    reply = mt.build_adu(9, 0, bytes((0x83, 0x02)))
    with pytest.raises(IllegalAddress):
        mt.parse_adu(reply, 9, 0x03)
    with pytest.raises(ProtocolError):
        mt.parse_adu(reply, 8, 0x03)
