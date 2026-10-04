"""Typed tools end to end through the tool functions, against an in-memory device."""

from __future__ import annotations

import pytest
from conftest import make_ctx, run

from modbus_mcp import cli, codec


def test_read_holding_typed_decodes_with_the_requested_orders(client, ctx) -> None:
    client.hr[0:2] = codec.encode([1.5], "float32", "little", "big")
    result = run(
        cli.read_holding_typed(0, "float32", ctx, count=1, byteorder="little", wordorder="big")
    )
    assert result["success"], result
    assert result["data"]["values"] == [1.5]
    assert result["error"] is None


def test_read_input_typed_applies_scale_and_offset(client, ctx) -> None:
    client.ir[4:8] = codec.encode([-250, 1000], "int32")
    result = run(cli.read_input_typed(4, "int32", ctx, count=2, scale=0.1, offset=1.0))
    assert result["success"], result
    assert result["data"]["values"] == pytest.approx([-24.0, 101.0])


def test_unscaled_64_bit_values_keep_full_precision(client, ctx) -> None:
    big = 2**64 - 1
    client.hr[0:4] = codec.encode([big], "uint64")
    result = run(cli.read_holding_typed(0, "uint64", ctx))
    assert result["data"]["values"] == [big]


def test_non_finite_floats_are_reported_as_strings(client, ctx) -> None:
    client.hr[0:2] = [0x7FC0, 0x0000]  # float32 NaN
    client.hr[2:4] = [0xFF80, 0x0000]  # float32 -inf
    result = run(cli.read_holding_typed(0, "float32", ctx, count=2))
    assert result["success"], result
    assert result["data"]["values"] == ["NaN", "-Infinity"]


def test_bad_dtype_and_order_come_back_as_envelopes(ctx) -> None:
    result = run(cli.read_holding_typed(0, "float16", ctx))
    assert result["success"] is False and "Unsupported dtype" in result["error"]
    result = run(cli.read_input_typed(0, "float32", ctx, byteorder="middle"))
    assert result["success"] is False and "byteorder" in result["error"]


def test_write_tag_encodes_typed_holding_values(client) -> None:
    tags = {"Speed": {"table": "holding", "address": 10, "dtype": "float32", "wordorder": "little"}}
    ctx = make_ctx(client, tags, writes_enabled=True)
    result = run(cli.write_tag("Speed", 42.5, ctx))
    assert result["success"], result
    assert client.hr[10:12] == codec.encode([42.5], "float32", "big", "little")
    result = run(cli.read_tag("Speed", ctx))
    assert result["data"]["values"] == [42.5]


def test_write_tag_rejects_out_of_range_values_before_sending(client) -> None:
    ctx = make_ctx(client, {"Raw": {"table": "holding", "address": 3}}, writes_enabled=True)
    result = run(cli.write_tag("Raw", 70000, ctx))
    assert result["success"] is False
    assert "out of uint16 range" in result["error"]
    assert client.writes == []


SCALED = {
    "Temp": {"table": "holding", "address": 20, "dtype": "int16", "scale": 0.1, "offset": -40},
    "Flow": {"table": "holding", "address": 30, "dtype": "float32", "scale": 2.0},
}


def test_write_tag_reverses_scale_and_offset(client) -> None:
    ctx = make_ctx(client, SCALED, writes_enabled=True)
    result = run(cli.write_tag("Temp", 25.3, ctx))
    assert result["success"], result
    assert client.hr[20] == 653  # (25.3 + 40) / 0.1
    assert result["meta"]["raw_values"] == [653]
    assert run(cli.read_tag("Temp", ctx))["data"]["values"] == [pytest.approx(25.3)]

    assert run(cli.write_tag("Flow", 3.0, ctx))["success"]
    assert codec.decode(client.hr[30:32], "float32") == [1.5]
    assert run(cli.read_tag("Flow", ctx))["data"]["values"] == [3.0]


def test_write_tag_refuses_values_between_scale_steps(client) -> None:
    ctx = make_ctx(client, SCALED, writes_enabled=True)
    result = run(cli.write_tag("Temp", 25.35, ctx))
    assert result["success"] is False
    assert "whole number of steps" in result["error"]
    assert client.calls == []


EXACT = {
    "Offset": {"table": "holding", "address": 40, "dtype": "int32", "offset": 0.5},
    "Tenths": {"table": "holding", "address": 42, "dtype": "uint32", "scale": 0.1},
    "Big": {"table": "holding", "address": 44, "dtype": "int64", "scale": 2},
}


@pytest.mark.parametrize(
    ("tag", "value"),
    [
        ("Offset", 1000000000),  # raw 999999999.5; float math accepted it
        ("Tenths", 300000000.05),  # raw 3000000000.5; float math rounded it
        ("Big", 2**60 + 1),  # raw 2**59 + 0.5
    ],
)
def test_write_tag_refuses_values_that_are_not_exact_steps(client, tag, value) -> None:
    ctx = make_ctx(client, EXACT, writes_enabled=True)
    result = run(cli.write_tag(tag, value, ctx))
    assert result["success"] is False and "whole number of steps" in result["error"], result
    assert client.calls == []


def test_write_tag_is_exact_for_large_values_and_decimal_steps(client) -> None:
    ctx = make_ctx(client, EXACT, writes_enabled=True)
    assert run(cli.write_tag("Big", 2**60 + 2, ctx))["success"]
    assert codec.decode(client.hr[44:48], "int64") == [2**59 + 1]  # not ...488 via float
    assert run(cli.write_tag("Tenths", 300000000.1, ctx))["success"]
    assert codec.decode(client.hr[42:44], "uint32") == [3000000001]
    assert run(cli.write_tag("Offset", 1000000000.5, ctx))["success"]
    assert codec.decode(client.hr[40:42], "int32") == [1000000000]
