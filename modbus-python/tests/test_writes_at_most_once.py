"""A write reaches the device at most once; a lost reply is reported, never re-sent.

Reads may be retried. Writes are retried only when nothing was sent: the
connection could not be opened, or pymodbus raised ConnectionException, which
it does before sending anything.
"""

from __future__ import annotations

import asyncio

import pytest
from conftest import make_ctx, run
from pymodbus.exceptions import ConnectionException

from modbus_mcp import cli
from modbus_mcp.config import Settings

TAGS = {
    "Speed": {"table": "holding", "address": 10, "dtype": "float32"},
    "Pump": {"table": "coil", "address": 0},
}

WRITE_CALLS = {
    "write_register": lambda ctx: cli.write_register(1, 50, ctx),
    "write_registers": lambda ctx: cli.write_registers(1, [1, 2], ctx),
    "write_coil": lambda ctx: cli.write_coil(0, True, ctx),
    "write_coils_bulk": lambda ctx: cli.write_coils_bulk(0, [True, False], ctx),
    "mask_write_register": lambda ctx: cli.mask_write_register(5, 0xFFFC, 3, ctx),
    "write_tag (holding)": lambda ctx: cli.write_tag("Speed", 42.5, ctx),
    "write_tag (coil)": lambda ctx: cli.write_tag("Pump", True, ctx),
}


@pytest.mark.parametrize("name", list(WRITE_CALLS))
def test_a_write_whose_reply_is_lost_is_sent_once(client, name: str) -> None:
    client.drop_write_replies = True
    ctx = make_ctx(client, TAGS, writes_enabled=True, max_retries=5)
    result = run(WRITE_CALLS[name](ctx))
    assert result["success"] is False
    assert result["error"] == cli.WRITE_NO_REPLY
    assert result["error"] == "no reply; the write may have been applied; check before retrying"
    assert result["meta"]["outcome"] == "unknown" and result["meta"]["request_sent"] is True
    assert "ModbusIOException" in result["meta"]["cause"]
    assert result["meta"]["attempts"] == 1
    assert len(client.sent_writes) == 1, client.sent


def test_the_lost_write_may_indeed_have_been_applied(client) -> None:
    client.drop_write_replies = True
    ctx = make_ctx(client, writes_enabled=True)
    assert run(cli.write_register(1, 50, ctx))["error"] == cli.WRITE_NO_REPLY
    assert client.hr[1] == 50
    assert run(cli.read_register(1, ctx))["data"] == {"value": 50}


def test_a_write_cut_off_by_the_tool_timeout_is_sent_once(client) -> None:
    client.write_reply_delay = 1.0
    ctx = make_ctx(client, writes_enabled=True, max_retries=5, tool_timeout=0.05)
    result = run(cli.write_registers(3, [7, 8], ctx))
    assert result["error"] == cli.WRITE_NO_REPLY
    assert "TimeoutError" in result["meta"]["cause"]
    assert client.sent_writes == ["write_registers"]


def test_a_write_that_was_not_sent_is_retried_after_reconnecting(client) -> None:
    def drop_connection_once(name: str) -> None:
        if not client.calls[:-1]:  # first request only
            client.connected = False
            raise ConnectionException("Not connected[fake]")  # pymodbus: before sending

    client.before = drop_connection_once
    ctx = make_ctx(client, writes_enabled=True)
    result = run(cli.write_register(1, 50, ctx))
    assert result["success"], result
    assert result["meta"]["attempts"] == 2
    assert result["meta"]["outcome"] == "written" and result["meta"]["request_sent"] is True
    assert client.connect_calls == 1
    assert client.sent_writes == ["write_register"]
    assert client.hr[1] == 50


def test_an_unreachable_device_gets_no_write(client) -> None:
    client.connected = False

    async def refuse() -> bool:
        return False

    client.connect = refuse
    ctx = make_ctx(client, writes_enabled=True)
    result = run(cli.write_register(1, 50, ctx))
    assert result["success"] is False
    assert result["error"] == "cannot reach device at 127.0.0.1:502"
    assert result["meta"]["outcome"] == "not_sent" and result["meta"]["request_sent"] is False
    assert client.calls == []


def test_a_write_the_device_rejects(client) -> None:
    ctx = make_ctx(client, writes_enabled=True)
    result = run(cli.write_register(150, 1, ctx))  # the fake device has 100 registers
    assert result["error"].startswith("Modbus exception 2 (Illegal Data Address)")
    assert result["meta"]["outcome"] == "rejected" and result["meta"]["request_sent"] is True


def test_a_write_refused_by_the_server_is_not_sent(client) -> None:
    ctx = make_ctx(client, writes_enabled=True)
    result = run(cli.write_registers(65535, [1, 2], ctx))  # runs past the address space
    assert "runs past" in result["error"]
    assert result["meta"]["outcome"] == "not_sent" and result["meta"]["request_sent"] is False
    assert client.calls == []


def test_reads_are_still_retried(client) -> None:
    def no_reply(name: str) -> None:
        if len(client.calls) < 3:
            raise asyncio.TimeoutError()

    client.before = no_reply
    ctx = make_ctx(client)  # MODBUS_MAX_RETRIES defaults to 2
    result = run(cli.read_register(0, ctx))
    assert result["success"], result
    assert result["meta"]["attempts"] == 3


@pytest.mark.parametrize("modbus_type", ["tcp", "udp", "serial"])
def test_pymodbus_does_not_resend_either(modbus_type: str) -> None:
    """pymodbus re-sends a request on timeout `retries` times (default 3); ours use 0."""

    async def build():
        try:
            client = cli._make_client(Settings(modbus_type=modbus_type, serial_port="/dev/null"))
        except ImportError as exc:  # serial without pyserial installed
            pytest.skip(str(exc))
        try:
            return client.ctx.retries
        finally:
            client.close()

    assert asyncio.run(build()) == 0
