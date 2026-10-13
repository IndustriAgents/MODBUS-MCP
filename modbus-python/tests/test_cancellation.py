"""A cancelled call, or one pending when the client disconnects, never sends afterwards."""

from __future__ import annotations

import asyncio
import json

import pytest
from conftest import make_ctx

from modbus_mcp import cli


def test_the_send_gate_refuses_when_the_call_may_no_longer_send() -> None:
    async def scenario():
        closing = False
        gate = cli.SendGate(lambda: closing)
        assert gate(True, b"x") == b"x" and gate.sent == 1  # no call in progress

        state = cli._CallState.start(10)
        token = cli._CALL.set(state)
        try:
            gate(True, b"x")
            assert state.sent and gate.sent == 2
            state.cancelled = True
            with pytest.raises(cli.RequestNotSent, match="cancelled"):
                gate(True, b"x")
            state.cancelled, state.expires = False, 0  # deadline passed
            with pytest.raises(cli.RequestNotSent, match="deadline"):
                gate(True, b"x")
            state.expires = asyncio.get_running_loop().time() + 10
            closing = True
            with pytest.raises(cli.RequestNotSent, match="disconnected"):
                gate(True, b"x")
            assert gate(False, b"reply") == b"reply"  # received data is never refused
        finally:
            cli._CALL.reset(token)
        assert gate.sent == 2

    asyncio.run(scenario())


def test_a_call_that_ends_without_a_result_is_marked_cancelled() -> None:
    async def scenario():
        state = cli._CallState.start(10)
        task = asyncio.create_task(cli.run_with_deadline(10, asyncio.sleep(5), state))
        await asyncio.sleep(0.05)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        return state.cancelled

    assert asyncio.run(scenario()) is True


@pytest.fixture
def connected_app(client, monkeypatch):
    """call_tool() outside an MCP session, with an in-memory device and writes enabled."""
    monkeypatch.setattr(cli.mcp, "client_gone", False)
    ctx = make_ctx(client, writes_enabled=True, max_retries=10, retry_backoff_base=0.1)
    app = ctx.request_context.lifespan_context
    app.closing = lambda: cli.mcp.client_gone
    monkeypatch.setattr(cli, "_app", lambda ctx: app)

    async def ignore(*args, **kwargs):  # the Context's MCP log calls need a session
        return None

    monkeypatch.setattr(cli.Context, "info", ignore)
    monkeypatch.setattr(cli.Context, "error", ignore)
    return app


def envelope_of(result) -> dict:
    if isinstance(result, tuple):
        result = result[0]
    return json.loads(result[0].text)


def test_a_pending_write_is_dropped_when_the_client_disconnects(
    client, connected_app, monkeypatch
) -> None:
    client.connected = False
    device_up = asyncio.Event()

    async def connect() -> bool:  # the device is down until device_up is set
        if device_up.is_set():
            client.connected = True
        return client.connected

    client.connect = connect

    async def scenario():
        call = asyncio.create_task(cli.mcp.call_tool("write_register", {"address": 1, "value": 5}))
        await asyncio.sleep(0.3)  # the write keeps trying to reconnect
        cli.mcp.client_disconnected()
        device_up.set()  # the device comes back after the client has gone
        result = envelope_of(await asyncio.wait_for(call, 5))
        await asyncio.sleep(0.5)
        entered = []  # a tool body that runs looks up the server state first
        monkeypatch.setattr(cli, "_app", lambda ctx: entered.append(1) or connected_app)
        later = envelope_of(await cli.mcp.call_tool("write_register", {"address": 2, "value": 6}))
        assert entered == [], "a new call started after the client had gone"
        return result, later

    result, later = asyncio.run(scenario())
    assert result["meta"]["outcome"] == "not_sent" and result["meta"]["request_sent"] is False
    assert "disconnected" in result["error"]
    assert later["meta"]["outcome"] == "not_sent"  # no new call starts once the client is gone
    assert client.sent == []


def test_disconnect_while_waiting_for_a_reply_is_unknown(client, connected_app) -> None:
    async def write_register(address, value, *, device_id=1):
        client._enter("write_register", address=address, value=value)  # sent
        await asyncio.Event().wait()  # no reply

    client.write_register = write_register

    async def scenario():
        call = asyncio.create_task(cli.mcp.call_tool("write_register", {"address": 1, "value": 5}))
        await asyncio.sleep(0.2)
        cli.mcp.client_disconnected()
        return envelope_of(await asyncio.wait_for(call, 5))

    result = asyncio.run(scenario())
    assert result["meta"]["outcome"] == "unknown" and result["meta"]["request_sent"] is True
    assert result["error"].endswith("the write may have been applied; check before retrying")


def test_the_gate_refuses_a_frame_when_the_connection_is_gone() -> None:
    """pymodbus drops a frame silently when its transport is gone; counting it as sent
    would report a write that never left as "unknown"."""
    from conftest import free_port

    from modbus_mcp.config import Settings

    async def scenario():
        gate = cli.SendGate()
        client = cli._make_client(Settings(port=free_port()), gate)
        gate.client = client
        try:
            with pytest.raises(cli.NotConnected):
                gate(True, b"frame")  # never connected: no transport
            return gate.sent
        finally:
            client.close()

    assert asyncio.run(scenario()) == 0
