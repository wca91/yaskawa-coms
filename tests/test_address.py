import pytest

from yaskawa_coms import AddressError, parse_address
from yaskawa_coms.datatypes import (
    int32_to_words,
    pack_bits,
    to_signed16,
    to_unsigned16,
    unpack_bits,
    words_to_int32,
)


@pytest.mark.parametrize(
    "text, area, kind, word, bit",
    [
        ("MW00100", "M", "W", 100, None),
        ("mw100", "M", "W", 100, None),
        ("MB00100A", "M", "B", 100, 10),
        ("MB000000", "M", "B", 0, 0),
        ("MF21000", "M", "F", 21000, None),
        ("ML00200", "M", "L", 200, None),
        ("IW0010", "I", "W", 0x10, None),
        ("IB0010F", "I", "B", 0x10, 15),
        ("IWFFFF", "I", "W", 0xFFFF, None),
    ],
)
def test_parse(text, area, kind, word, bit):
    a = parse_address(text)
    assert (a.area, a.kind, a.word, a.bit) == (area, kind, word, bit)


def test_round_trip_and_properties():
    a = parse_address("MB00100A")
    assert str(a) == "MB00100A"
    assert a.coil == 100 * 16 + 10
    assert parse_address("MF21000").width == 2
    assert str(parse_address("iw10")) == "IW0010"
    assert parse_address("MW1").writable and not parse_address("IW1").writable


@pytest.mark.parametrize("bad", ["", "MX100", "MWABC", "MB1", "MW65536", "MF65535", "DW100", "MW-1"])
def test_bad_addresses(bad):
    with pytest.raises(AddressError):
        parse_address(bad)


def test_word_conversions():
    assert to_signed16(0xFFFF) == -1
    assert to_unsigned16(-1) == 0xFFFF
    with pytest.raises(ValueError):
        to_unsigned16(70000)
    assert int32_to_words(-2) == (0xFFFE, 0xFFFF)
    assert words_to_int32(0xFFFE, 0xFFFF) == -2
    assert words_to_int32(*int32_to_words(123456789)) == 123456789


def test_bit_packing():
    bits = [True, False, True, True, False, False, False, False, True]
    assert pack_bits(bits) == bytes((0b00001101, 0b00000001))
    assert unpack_bits(pack_bits(bits), len(bits)) == bits
