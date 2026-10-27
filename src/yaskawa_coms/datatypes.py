"""Conversions between 16-bit register words and Python values.

Yaskawa MP-series controllers store 32-bit values (ML long integers and MF
floats) in two consecutive words, LOW word first:

    MF21000 = MW21000 (low word) + MW21001 (high word)

This was verified against a real PLC: MW21000=0xE4F7, MW21001=0x410A gives
MF21000 = 8.6809.
"""
from __future__ import annotations

import struct
from typing import Iterable, List, Sequence, Tuple


def to_unsigned16(value: int) -> int:
    """Accept -32768..65535 and return the raw 16-bit word (0..65535)."""
    if not -0x8000 <= value <= 0xFFFF:
        raise ValueError(f"{value} does not fit in a 16-bit register")
    return value & 0xFFFF


def to_signed16(word: int) -> int:
    """Interpret a raw 16-bit word as a signed integer (-32768..32767)."""
    word &= 0xFFFF
    return word - 0x10000 if word & 0x8000 else word


def words_to_float(low: int, high: int) -> float:
    """Join two words (low word first) into an IEEE-754 32-bit float."""
    return struct.unpack("<f", struct.pack("<HH", low & 0xFFFF, high & 0xFFFF))[0]


def float_to_words(value: float) -> Tuple[int, int]:
    """Split a float into ``(low_word, high_word)``."""
    low, high = struct.unpack("<HH", struct.pack("<f", value))
    return low, high


def words_to_int32(low: int, high: int) -> int:
    """Join two words (low word first) into a signed 32-bit integer (ML)."""
    return struct.unpack("<i", struct.pack("<HH", low & 0xFFFF, high & 0xFFFF))[0]


def int32_to_words(value: int) -> Tuple[int, int]:
    """Split a signed or unsigned 32-bit integer into ``(low_word, high_word)``."""
    if not -0x80000000 <= value <= 0xFFFFFFFF:
        raise ValueError(f"{value} does not fit in 32 bits")
    value &= 0xFFFFFFFF
    return value & 0xFFFF, value >> 16


def pack_bits(bits: Sequence[bool]) -> bytes:
    """Pack booleans into bytes, first bit in the least significant position
    (MEMOBUS / Modbus coil order)."""
    out = bytearray((len(bits) + 7) // 8)
    for i, bit in enumerate(bits):
        if bit:
            out[i // 8] |= 1 << (i % 8)
    return bytes(out)


def unpack_bits(data: bytes, count: int) -> List[bool]:
    """Inverse of :func:`pack_bits`."""
    if len(data) * 8 < count:
        raise ValueError(f"need {count} bits but only got {len(data)} bytes")
    return [bool(data[i // 8] & (1 << (i % 8))) for i in range(count)]


def words_to_bytes_le(words: Iterable[int]) -> bytes:
    """Words to bytes, low byte first (Extended MEMOBUS / non-procedure order)."""
    return b"".join(struct.pack("<H", w & 0xFFFF) for w in words)


def bytes_to_words_le(data: bytes) -> List[int]:
    """Bytes (low byte first) to words. A trailing odd byte is ignored."""
    n = len(data) // 2
    return list(struct.unpack(f"<{n}H", data[: n * 2]))


def words_to_bytes_be(words: Iterable[int]) -> bytes:
    """Words to bytes, high byte first (Modbus / standard MEMOBUS order)."""
    return b"".join(struct.pack(">H", w & 0xFFFF) for w in words)


def bytes_to_words_be(data: bytes) -> List[int]:
    """Bytes (high byte first) to words. A trailing odd byte is ignored."""
    n = len(data) // 2
    return list(struct.unpack(f">{n}H", data[: n * 2]))
