"""Typed register encoding/decoding (the old `Endian.Big` bug made every call fail)."""

from __future__ import annotations

import itertools
import math

import pytest

from modbus_mcp import codec

ORDERS = list(itertools.product(codec.ORDERS, codec.ORDERS))

# float32 1.0 is 0x3F800000; how it lands in two registers for each (byteorder, wordorder).
FLOAT32_ONE = {
    ("big", "big"): [0x3F80, 0x0000],
    ("big", "little"): [0x0000, 0x3F80],
    ("little", "big"): [0x803F, 0x0000],
    ("little", "little"): [0x0000, 0x803F],
}

SAMPLES = {
    "int16": [-32768, -1, 0, 1234, 32767],
    "uint16": [0, 1, 0xABCD, 65535],
    "int32": [-(2**31), -123456, 0, 2**31 - 1],
    "uint32": [0, 0x12345678, 2**32 - 1],
    "int64": [-(2**63), -1, 0x0123456789ABCDEF, 2**63 - 1],
    "uint64": [0, 2**53 + 1, 2**64 - 1],
    "float32": [0.0, 1.0, -2.5, 3.4028234663852886e38],
    "float64": [0.0, 1.0, -2.5, math.pi, 1e300],
}


@pytest.mark.parametrize(("byteorder", "wordorder"), ORDERS)
def test_float32_layout(byteorder: str, wordorder: str) -> None:
    regs = FLOAT32_ONE[(byteorder, wordorder)]
    assert codec.encode([1.0], "float32", byteorder, wordorder) == regs
    assert codec.decode(regs, "float32", 1, byteorder, wordorder) == [1.0]


@pytest.mark.parametrize(("byteorder", "wordorder"), ORDERS)
@pytest.mark.parametrize("dtype", list(codec.DTYPES))
def test_round_trip(dtype: str, byteorder: str, wordorder: str) -> None:
    values = SAMPLES[dtype]
    regs = codec.encode(values, dtype, byteorder, wordorder)
    assert len(regs) == codec.register_count(dtype) * len(values)
    assert all(0 <= r <= 0xFFFF for r in regs)
    decoded = codec.decode(regs, dtype, len(values), byteorder, wordorder)
    if dtype == "float32":
        assert decoded == pytest.approx(values, rel=1e-7)
    else:
        assert decoded == values


def test_known_vectors() -> None:
    assert codec.encode([0x12345678], "uint32") == [0x1234, 0x5678]
    assert codec.encode([0x12345678], "uint32", "big", "little") == [0x5678, 0x1234]
    assert codec.encode([0x12345678], "uint32", "little", "big") == [0x3412, 0x7856]
    assert codec.encode([-2], "int16") == [0xFFFE]
    assert codec.encode([-2], "int16", "little") == [0xFEFF]
    assert codec.decode([0xFFFF, 0xFFFE], "int32") == [-2]
    assert codec.decode([0x4049, 0x0FDB], "float32")[0] == pytest.approx(math.pi, rel=1e-7)


def test_orders_are_case_insensitive_and_validated() -> None:
    assert codec.encode([1.0], "float32", "BIG", "Little") == FLOAT32_ONE[("big", "little")]
    with pytest.raises(codec.CodecError, match="byteorder"):
        codec.encode([1], "uint16", "middle", "big")
    with pytest.raises(codec.CodecError, match="wordorder"):
        codec.decode([1], "uint16", 1, "big", "b")


@pytest.mark.parametrize(
    ("value", "dtype", "message"),
    [
        (65536, "uint16", "out of uint16 range"),
        (-1, "uint16", "out of uint16 range"),
        (32768, "int16", "out of int16 range"),
        (2**64, "uint64", "out of uint64 range"),
        (42.5, "int32", "whole number"),
        (True, "uint16", "must be a number"),
        ("12", "uint16", "must be a number"),
        (None, "float32", "must be a number"),
        (float("nan"), "float32", "finite"),
        (float("inf"), "float64", "finite"),
        (1e39, "float32", "out of float32 range"),
    ],
)
def test_encode_rejects_values_it_cannot_hold(value: object, dtype: str, message: str) -> None:
    with pytest.raises(codec.CodecError, match=message):
        codec.encode([value], dtype)


def test_integral_float_is_accepted_for_integer_dtypes() -> None:
    assert codec.encode([42.0], "uint16") == [42]


def test_decode_errors() -> None:
    with pytest.raises(codec.CodecError, match="Unsupported dtype"):
        codec.decode([0, 0], "float16")
    with pytest.raises(codec.CodecError, match="Insufficient registers"):
        codec.decode([0], "float32")
    with pytest.raises(codec.CodecError, match="16-bit"):
        codec.decode([70000], "uint16")


def test_matches_pymodbus_binary_payload() -> None:
    """Same layout as pymodbus' BinaryPayloadBuilder/Decoder, which exist up to pymodbus 3.9."""
    payload = pytest.importorskip(
        "pymodbus.payload", reason="pymodbus >= 3.10 has no payload module"
    )
    from pymodbus.constants import Endian

    endian = {"big": Endian.BIG, "little": Endian.LITTLE}
    adders = {
        "int16": "add_16bit_int",
        "uint16": "add_16bit_uint",
        "int32": "add_32bit_int",
        "uint32": "add_32bit_uint",
        "float32": "add_32bit_float",
        "int64": "add_64bit_int",
        "uint64": "add_64bit_uint",
        "float64": "add_64bit_float",
    }
    for (byteorder, wordorder), (dtype, adder) in itertools.product(ORDERS, adders.items()):
        builder = payload.BinaryPayloadBuilder(
            byteorder=endian[byteorder], wordorder=endian[wordorder]
        )
        for value in SAMPLES[dtype]:
            getattr(builder, adder)(value)
        expected = builder.to_registers()
        assert codec.encode(SAMPLES[dtype], dtype, byteorder, wordorder) == expected, (
            dtype,
            byteorder,
            wordorder,
        )
