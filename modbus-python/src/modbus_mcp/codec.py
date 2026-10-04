"""Typed values to and from 16-bit Modbus registers.

A Modbus register holds 16 bits. Wider values span several consecutive
registers, and devices disagree about how their bytes are laid out, so two
orders are configurable:

- ``byteorder``: the order of the two bytes inside each register
  (``"big"`` = most significant byte first, the Modbus default);
- ``wordorder``: the order of the registers that make up one value
  (``"big"`` = most significant register first).

This is the layout pymodbus' ``BinaryPayloadBuilder``/``BinaryPayloadDecoder``
used. Those classes were removed in pymodbus 3.10, so the conversion is done
here with :mod:`struct` and no longer depends on the pymodbus version.
"""

from __future__ import annotations

import math
import struct
from collections.abc import Sequence

# dtype -> (struct format character, number of 16-bit registers per value)
DTYPES: dict[str, tuple[str, int]] = {
    "int16": ("h", 1),
    "uint16": ("H", 1),
    "int32": ("i", 2),
    "uint32": ("I", 2),
    "float32": ("f", 2),
    "int64": ("q", 4),
    "uint64": ("Q", 4),
    "float64": ("d", 4),
}

ORDERS = ("big", "little")

_INT_RANGES: dict[str, tuple[int, int]] = {
    "int16": (-(2**15), 2**15 - 1),
    "uint16": (0, 2**16 - 1),
    "int32": (-(2**31), 2**31 - 1),
    "uint32": (0, 2**32 - 1),
    "int64": (-(2**63), 2**63 - 1),
    "uint64": (0, 2**64 - 1),
}

_FLOAT32_MAX = 3.4028234663852886e38


class CodecError(ValueError):
    """A value, dtype or order that cannot be encoded or decoded."""


def check_dtype(dtype: str) -> str:
    """Return ``dtype`` if it is supported, else raise :class:`CodecError`."""
    if dtype not in DTYPES:
        raise CodecError(f"Unsupported dtype {dtype!r}; expected one of {', '.join(DTYPES)}")
    return dtype


def check_order(order: str, name: str = "order") -> str:
    """Normalise ``"big"``/``"little"`` (any case), else raise :class:`CodecError`."""
    normalised = str(order).strip().lower()
    if normalised not in ORDERS:
        raise CodecError(f"{name} must be 'big' or 'little', got {order!r}")
    return normalised


def register_count(dtype: str) -> int:
    """Number of 16-bit registers one value of ``dtype`` occupies."""
    return DTYPES[check_dtype(dtype)][1]


def _words_to_bytes(words: Sequence[int], byteorder: str, wordorder: str) -> bytes:
    if wordorder == "little":
        words = list(reversed(words))
    return b"".join(int(w).to_bytes(2, byteorder) for w in words)  # type: ignore[arg-type]


def _bytes_to_words(raw: bytes, byteorder: str, wordorder: str) -> list[int]:
    words = [int.from_bytes(raw[i : i + 2], byteorder) for i in range(0, len(raw), 2)]  # type: ignore[arg-type]
    if wordorder == "little":
        words.reverse()
    return words


def decode(
    registers: Sequence[int],
    dtype: str,
    count: int = 1,
    byteorder: str = "big",
    wordorder: str = "big",
) -> list[int | float]:
    """Decode ``count`` values of ``dtype`` from the start of ``registers``."""
    fmt, size = DTYPES[check_dtype(dtype)]
    byteorder = check_order(byteorder, "byteorder")
    wordorder = check_order(wordorder, "wordorder")
    if count < 1:
        raise CodecError("count must be at least 1")
    needed = size * count
    if len(registers) < needed:
        raise CodecError(
            f"Insufficient registers: have {len(registers)}, need {needed} for {count} {dtype}"
        )
    for reg in registers[:needed]:
        if not 0 <= int(reg) <= 0xFFFF:
            raise CodecError(f"Register value {reg} is not a 16-bit word")
    values: list[int | float] = []
    for i in range(count):
        raw = _words_to_bytes(registers[i * size : (i + 1) * size], byteorder, wordorder)
        values.append(struct.unpack(">" + fmt, raw)[0])
    return values


def _check_value(value: object, dtype: str) -> int | float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise CodecError(f"{dtype} value must be a number, got {value!r}")
    if dtype in _INT_RANGES:
        if isinstance(value, float):
            if not math.isfinite(value) or not value.is_integer():
                raise CodecError(f"{dtype} value must be a whole number, got {value!r}")
            value = int(value)
        lo, hi = _INT_RANGES[dtype]
        if not lo <= value <= hi:
            raise CodecError(f"Value {value} out of {dtype} range ({lo}..{hi})")
        return value
    value = float(value)
    if not math.isfinite(value):
        raise CodecError(f"{dtype} value must be finite, got {value!r}")
    if dtype == "float32" and abs(value) > _FLOAT32_MAX:
        raise CodecError(f"Value {value} out of float32 range")
    return value


def encode(
    values: Sequence[object],
    dtype: str,
    byteorder: str = "big",
    wordorder: str = "big",
) -> list[int]:
    """Encode ``values`` as ``dtype`` into a list of 16-bit register values.

    Raises :class:`CodecError` for a value the dtype cannot hold exactly (out of
    range, not a whole number for an integer dtype, not finite), rather than
    truncating or wrapping it.
    """
    fmt, _size = DTYPES[check_dtype(dtype)]
    byteorder = check_order(byteorder, "byteorder")
    wordorder = check_order(wordorder, "wordorder")
    registers: list[int] = []
    for value in values:
        raw = struct.pack(">" + fmt, _check_value(value, dtype))
        registers.extend(_bytes_to_words(raw, byteorder, wordorder))
    return registers
