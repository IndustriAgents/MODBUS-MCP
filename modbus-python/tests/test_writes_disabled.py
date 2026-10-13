"""Write tools are off unless MODBUS_WRITES_ENABLED=true, and then send nothing at all."""

from __future__ import annotations

import pytest
from conftest import make_ctx, run

from modbus_mcp import cli

TAGS = {
    "Valve": {"table": "holding", "address": 0},
    "Pump": {"table": "coil", "address": 0},
}

WRITE_CALLS = {
    "write_register": lambda ctx: cli.write_register(1, 50, ctx),
    "write_registers": lambda ctx: cli.write_registers(1, [1, 2], ctx),
    "write_coil": lambda ctx: cli.write_coil(0, True, ctx),
    "write_coils_bulk": lambda ctx: cli.write_coils_bulk(0, [True, False], ctx),
    "mask_write_register": lambda ctx: cli.mask_write_register(5, 0xFFFF, 3, ctx),
    "write_tag (holding)": lambda ctx: cli.write_tag("Valve", 10, ctx),
    "write_tag (coil)": lambda ctx: cli.write_tag("Pump", True, ctx),
}


@pytest.mark.parametrize("name", list(WRITE_CALLS))
def test_writes_are_refused_by_default(client, name: str) -> None:
    ctx = make_ctx(client, TAGS)  # default settings
    result = run(WRITE_CALLS[name](ctx))
    assert result["success"] is False
    assert result["error"] == cli.WRITES_DISABLED
    assert "MODBUS_WRITES_ENABLED=true" in result["error"]
    assert result["meta"]["outcome"] == "not_sent" and result["meta"]["request_sent"] is False
    assert client.calls == []  # nothing reached the device


@pytest.mark.parametrize("name", list(WRITE_CALLS))
def test_writes_go_through_when_enabled(client, name: str) -> None:
    ctx = make_ctx(client, TAGS, writes_enabled=True)
    result = run(WRITE_CALLS[name](ctx))
    assert result["success"] is True, result
    assert result["meta"]["outcome"] == "written" and result["meta"]["request_sent"] is True
    assert client.writes


def test_ping_reports_writes_disabled(client, ctx) -> None:
    result = run(cli.ping(ctx))
    assert result["data"]["writes_enabled"] is False


def test_write_tag_checks_the_switch_before_anything_else(client) -> None:
    # write_tag must not depend on the gates of the tools it calls: with writes
    # disabled, even an unknown tag or an unusable value gets the same refusal.
    ctx = make_ctx(client, TAGS)
    for name, value in [("NoSuchTag", 1), ("Valve", "not a number"), ("Pump", 1)]:
        result = run(cli.write_tag(name, value, ctx))
        assert result["error"] == cli.WRITES_DISABLED, (name, result)
        assert result["meta"] == {"tag": name, "outcome": "not_sent", "request_sent": False}
    assert client.calls == []
