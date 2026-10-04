"""Every tool call has an overall deadline (MODBUS_CALL_TIMEOUT); nothing can hang.

On expiry a read is a clean error, a write that went out is "unknown" (it may
have been applied), and a write that never left is "not_sent".
"""

from __future__ import annotations

import asyncio
import json
import time

import pytest
from conftest import make_ctx, run

from modbus_mcp import cli
from modbus_mcp.config import ConfigError, Settings


def forever():
    return asyncio.Event().wait()


def timed(coro, seconds: float):
    start = time.monotonic()
    result = run(cli.run_with_deadline(seconds, coro))
    return result, time.monotonic() - start


def test_the_setting() -> None:
    assert Settings.from_env({}).call_timeout == 30.0
    assert Settings.from_env({"MODBUS_CALL_TIMEOUT": "2.5"}).call_timeout == 2.5
    with pytest.raises(ConfigError, match="MODBUS_CALL_TIMEOUT"):
        Settings.from_env({"MODBUS_CALL_TIMEOUT": "0"})


def test_a_read_that_never_answers_is_a_clean_error(client) -> None:
    async def read_holding_registers(address, *, count=1, device_id=1):
        client._enter("read_holding_registers", address=address, count=count)
        await forever()

    client.read_holding_registers = read_holding_registers
    result, elapsed = timed(cli.read_register(0, make_ctx(client, max_retries=5)), 0.3)
    assert elapsed < 1.5
    assert result["success"] is False
    assert result["error"].startswith("no result within the 0.3 s deadline")


def test_a_write_that_went_out_and_never_got_a_reply_is_unknown(client) -> None:
    async def write_register(address, value, *, device_id=1):
        client._enter("write_register", address=address, value=value)  # sent
        await forever()

    client.write_register = write_register
    ctx = make_ctx(client, writes_enabled=True)
    result, elapsed = timed(cli.write_register(1, 5, ctx), 0.3)
    assert elapsed < 1.5
    assert result["error"] == cli.WRITE_NO_REPLY
    assert result["meta"]["outcome"] == "unknown" and result["meta"]["request_sent"] is True


def test_a_write_that_never_left_is_not_sent(client) -> None:
    client.connected = False

    async def connect() -> bool:
        await forever()  # the connection attempt hangs

    client.connect = connect
    ctx = make_ctx(client, writes_enabled=True, max_retries=5)
    result, elapsed = timed(cli.write_register(1, 5, ctx), 0.3)
    assert elapsed < 1.5
    assert result["meta"]["outcome"] == "not_sent" and result["meta"]["request_sent"] is False
    assert "may have been applied" not in result["error"]
    assert client.sent == []


def test_the_backstop_ends_a_call_that_ignores_the_deadline(client, monkeypatch) -> None:
    """Even code outside the retry loop cannot hang a call: it is cut off shortly after."""
    app = make_ctx(client, writes_enabled=True).request_context.lifespan_context
    monkeypatch.setattr(cli, "_app", lambda ctx: app)

    async def stuck(*args, **kwargs):
        await asyncio.sleep(6)  # longer than deadline + backstop; then it would "finish"

    monkeypatch.setattr(cli, "_retry_call", stuck)
    monkeypatch.setattr(cli.ModbusMCP, "_call_timeout", lambda self: 0.2)
    for tool, arguments, outcome in [
        ("read_register", {"address": 0}, None),
        ("write_register", {"address": 0, "value": 1}, "not_sent"),  # it never got to send
    ]:
        start = time.monotonic()
        result = asyncio.run(cli.mcp.call_tool(tool, arguments))
        elapsed = time.monotonic() - start
        if isinstance(result, tuple):
            result = result[0]
        envelope = json.loads(result[0].text)
        assert elapsed < 0.2 + cli.DEADLINE_BACKSTOP + 1.0
        assert envelope["success"] is False
        assert envelope["error"].startswith("did not finish within the 0.2 s deadline")
        assert envelope["meta"].get("outcome") == outcome
