"""Parse Yaskawa register addresses such as ``"MW00100"`` or ``"MB00100A"``.

Rules (System Controller Programming Manual, chapter 3, and the MP2000
communication manual):

* ``M`` registers (MB/MW/ML/MF) use DECIMAL word numbers.
* ``I`` registers (IB/IW/IL/IF) use HEXADECIMAL word numbers.
* A bit address is the word number followed by ONE hex digit for the bit:
  ``MB00100A`` is bit 10 (0xA) of ``MW00100``; ``IB0010F`` is bit 15 of
  ``IW0010``.
* ``L`` (32-bit integer) and ``F`` (32-bit float) occupy two words, low word
  at the given number: ``MF21000`` = ``MW21000`` (low) + ``MW21001`` (high).

Over MEMOBUS/Modbus a controller acting as slave exposes:
MW -> holding registers, IW -> input registers, MB -> coils, IB -> input relays.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Optional, Union

from .exceptions import AddressError

_PATTERN = re.compile(r"^\s*([MI])([BWLF])([0-9A-F]+)\s*$", re.IGNORECASE)

#: Register types that are 2 words long.
_DOUBLE = {"L", "F"}


@dataclass(frozen=True)
class Address:
    """A parsed register address.

    Attributes:
        area: ``"M"`` (data registers, read/write) or ``"I"`` (input, read only).
        kind: ``"B"`` bit, ``"W"`` word, ``"L"`` 32-bit int, ``"F"`` 32-bit float.
        word: word (register) number.
        bit: bit number 0-15 for ``kind == "B"``, otherwise ``None``.
    """

    area: str
    kind: str
    word: int
    bit: Optional[int] = None

    @property
    def width(self) -> int:
        """Number of 16-bit words one value of this type occupies."""
        return 2 if self.kind in _DOUBLE else 1

    @property
    def writable(self) -> bool:
        """Only M registers can be written over the network."""
        return self.area == "M"

    @property
    def coil(self) -> int:
        """Coil / relay number for a bit address (``word * 16 + bit``)."""
        if self.bit is None:
            raise AddressError(f"{self} is not a bit address")
        return self.word * 16 + self.bit

    def __str__(self) -> str:
        if self.area == "M":
            number = f"{self.word:05d}"
        else:
            number = f"{self.word:04X}"
        if self.kind == "B":
            number += f"{self.bit:X}"
        return f"{self.area}{self.kind}{number}"


AddressLike = Union[str, Address]


def parse_address(text: AddressLike) -> Address:
    """Parse an address string. Accepts upper or lower case and short forms
    (``"MW100"`` is the same as ``"MW00100"``).

    >>> parse_address("MB00100A")
    Address(area='M', kind='B', word=100, bit=10)
    >>> parse_address("IW0010").word
    16
    """
    if isinstance(text, Address):
        return text
    match = _PATTERN.match(str(text))
    if not match:
        raise AddressError(
            f"invalid address {text!r}: expected e.g. MW00100, MB00100A, MF21000, IW0010"
        )
    area, kind, digits = (g.upper() for g in match.groups())
    base = 10 if area == "M" else 16

    bit: Optional[int] = None
    if kind == "B":
        if len(digits) < 2:
            raise AddressError(f"invalid bit address {text!r}: needs word number + bit digit")
        bit = int(digits[-1], 16)
        digits = digits[:-1]

    try:
        word = int(digits, base)
    except ValueError:
        raise AddressError(
            f"invalid address {text!r}: {area} register numbers are "
            f"{'decimal' if base == 10 else 'hexadecimal'}"
        ) from None

    last_word = word + (2 if kind in _DOUBLE else 1) - 1
    if last_word > 0xFFFF:
        raise AddressError(f"address {text!r} is out of range (max word 65535)")
    return Address(area, kind, word, bit)
