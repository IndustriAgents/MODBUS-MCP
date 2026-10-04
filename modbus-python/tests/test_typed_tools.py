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
