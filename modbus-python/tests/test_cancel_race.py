"""The cancel race: notifications/cancelled lands in the same event-loop step as the
write is granted pymodbus' lock.

mcp's RequestResponder.cancel() cancels the request's cancel scope synchronously and
answers "Request cancelled". anyio does not interrupt a task whose wait has already
completed, so the write runs one more step and would reach the send. SendGate must
refuse it (it checks the scope's cancel_called), and the call must then end as a
cancellation: a second reply would trip the SDK's "Request already responded to".

These run the real server (cli.mcp) over mcp's in-memory transport, with an
in-memory device, and drive the real RequestResponder.cancel().
"""

from __future__ import annotations

import asyncio
import json

import anyio
import pytest
from conftest import FakeClient
from mcp.shared.exceptions import McpError
from mcp.shared.memory import create_connected_server_and_client_session

from modbus_mcp import cli
from modbus_mcp.config import Settings

pytestmark = pytest.mark.anyio

ROUNDS = 100


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.fixture
def server(monkeypatch):
    """cli.mcp with an in-memory device; records each request's mcp RequestResponder."""
    device = FakeClient()

    def make_client(settings, gate):
        device.trace_packet = gate
        return device

    monkeypatch.setattr(cli, "_config", (Settings(writes_enabled=True, retry_backoff_base=0), {}))
    monkeypatch.setattr(cli, "_make_client", make_client)
    monkeypatch.setattr(cli.mcp, "client_gone", False)
    responders: list = []
    original = cli.ModbusMCP._request_cancel_scope

    def capture(self):
        context = self._mcp_server.request_context
        responders.append(context.session._in_flight.get(context.request_id))
        return original(self)

    monkeypatch.setattr(cli.ModbusMCP, "_request_cancel_scope", capture)
    return device, responders


def envelope(result) -> dict:
    return json.loads(result.content[0].text)


async def test_the_request_cancel_scope_is_captured(server, monkeypatch) -> None:
    """Pins the private mcp internals the cancel check relies on (session._in_flight,
    RequestResponder._cancel_scope): this fails if a new mcp release moves them."""
    states = []
    original_start = cli._CallState.start

    def start(seconds):
        states.append(original_start(seconds))
        return states[-1]

    monkeypatch.setattr(cli._CallState, "start", staticmethod(start))
    async with create_connected_server_and_client_session(cli.mcp._mcp_server) as client:
        assert envelope(await client.call_tool("ping", {}))["success"] is True
    assert isinstance(states[-1].request_scope, anyio.CancelScope)
    assert not states[-1].request_scope.cancel_called


async def test_a_write_cancelled_as_it_is_granted_the_lock_is_never_sent(server) -> None:
    device, responders = server
    async with create_connected_server_and_client_session(cli.mcp._mcp_server) as client:
        for round_ in range(ROUNDS):
            lock = asyncio.Lock()
            await lock.acquire()  # held by "another request"

            async def write_register(address, value, *, device_id=1, lock=lock):
                async with lock:  # like pymodbus: one request at a time
                    return await FakeClient.write_register(
                        device, address, value, device_id=device_id
                    )

            device.write_register = write_register
            call = asyncio.create_task(
                client.call_tool("write_register", {"address": 1, "value": round_})
            )
            with anyio.fail_after(5):
                while not getattr(lock, "_waiters", None):  # the write waits for the lock
                    await asyncio.sleep(0.001)
            responder = responders[-1]
            lock.release()  # the write's wait completes...
            await responder.cancel()  # ...and the client cancels, in the same step
            with anyio.fail_after(5), pytest.raises(McpError, match="cancelled"):
                await call
        # The server survived every round (no "Request already responded to").
        with anyio.fail_after(10):
            assert envelope(await client.call_tool("ping", {}))["success"] is True
    assert device.sent_writes == []
