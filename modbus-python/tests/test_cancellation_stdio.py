"""Over stdio: a write the client cancels, or one pending when the client disconnects,
never reaches the device afterwards.

The device is a small Modbus TCP server in this process that counts every frame
(see test_write_reaches_device_once.py), so "nothing arrives" is checked on the wire.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys

import pytest
from conftest import free_port
from test_write_reaches_device_once import DropWriteRepliesDevice

pytestmark = [pytest.mark.integration, pytest.mark.anyio]


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


class RawClient:
    """Just enough of an MCP client, speaking JSON-RPC over the server's stdio."""

    def __init__(self, proc: asyncio.subprocess.Process) -> None:
        self.proc = proc
        self.responses: dict[int, dict] = {}
        self.arrived = asyncio.Event()
        self.next_id = 0
        self.reader = asyncio.create_task(self._read())

    @classmethod
    async def start(cls, port: int, **env: str) -> RawClient:
        environ = {k: v for k, v in os.environ.items() if not k.startswith("MODBUS_")}
        environ.update(
            MODBUS_HOST="127.0.0.1",
            MODBUS_PORT=str(port),
            MODBUS_WRITES_ENABLED="true",
            PYTHONUNBUFFERED="1",
            **env,
        )
        proc = await asyncio.create_subprocess_exec(
            sys.executable,
            "-m",
            "modbus_mcp",
            env=environ,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        client = cls(proc)
        await client.request(
            "initialize",
            {
                "protocolVersion": "2024-11-05",
                "capabilities": {},
                "clientInfo": {"name": "pytest", "version": "0"},
            },
        )
        await client.notify("notifications/initialized")
        return client

    async def _read(self) -> None:
        while line := await self.proc.stdout.readline():
            message = json.loads(line)
            if "id" in message:
                self.responses[message["id"]] = message
                self.arrived.set()

    async def notify(self, method: str, params: dict | None = None) -> None:
        message = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            message["params"] = params
        self.proc.stdin.write((json.dumps(message) + "\n").encode())
        await self.proc.stdin.drain()

    async def send(self, method: str, params: dict | None = None) -> int:
        self.next_id += 1
        message = {"jsonrpc": "2.0", "id": self.next_id, "method": method}
        if params is not None:
            message["params"] = params
        self.proc.stdin.write((json.dumps(message) + "\n").encode())
        await self.proc.stdin.drain()
        return self.next_id

    async def response(self, request_id: int, timeout: float = 30) -> dict:
        async with asyncio.timeout(timeout) if sys.version_info >= (3, 11) else _timeout(timeout):
            while request_id not in self.responses:
                self.arrived.clear()
                await self.arrived.wait()
        return self.responses[request_id]

    async def request(self, method: str, params: dict | None = None) -> dict:
        return await self.response(await self.send(method, params))

    async def call(self, tool: str, **arguments) -> int:
        return await self.send("tools/call", {"name": tool, "arguments": arguments})

    async def close(self) -> tuple[int | None, str]:
        """Close stdin (if still open), wait for the server, then make sure it is gone."""
        if not self.proc.stdin.is_closing():
            self.proc.stdin.close()
        try:
            await asyncio.wait_for(self.proc.wait(), 10)
            exited = self.proc.returncode
        except asyncio.TimeoutError:  # it should have exited by itself
            exited = None
            self.proc.terminate()
            await self.proc.wait()
        self.reader.cancel()
        stderr = (await self.proc.stderr.read()).decode(errors="replace")
        return exited, stderr


class _timeout:  # asyncio.timeout() for Python 3.10
    def __init__(self, seconds: float) -> None:
        self.seconds = seconds

    async def __aenter__(self):
        self.handle = asyncio.get_running_loop().call_later(
            self.seconds, asyncio.current_task().cancel
        )

    async def __aexit__(self, *exc):
        self.handle.cancel()


def envelope(response: dict) -> dict:
    return json.loads(response["result"]["content"][0]["text"])


# The write keeps reconnecting (backoff 0.3, 0.6, 1.2, 2.4 s, ...) while the device is down.
RECONNECTING = {"MODBUS_MAX_RETRIES": "10", "MODBUS_RETRY_BACKOFF_BASE": "0.3"}


async def test_disconnecting_stops_a_write_that_is_still_connecting() -> None:
    """Device down, write issued, stdin closed, device comes up: nothing may arrive."""
    port = free_port()
    client = await RawClient.start(port, **RECONNECTING)
    try:
        await client.call("write_register", address=1, value=5)
        await asyncio.sleep(0.8)  # it is retrying the connection
        client.proc.stdin.close()  # the client goes away
        async with DropWriteRepliesDevice(write_reply="echo", port=port) as device:
            await asyncio.sleep(5)  # long enough for several reconnect attempts
            assert sum(device.received.values()) == 0, device.received
    finally:
        exited, stderr = await client.close()
    assert "Traceback" not in stderr, stderr
    assert "MCP client disconnected" in stderr
    assert exited == 0, stderr  # a clean end, even with a reply it could not deliver


async def test_a_cancelled_write_that_is_still_connecting_is_never_sent() -> None:
    port = free_port()
    client = await RawClient.start(port, **RECONNECTING)
    try:
        write = await client.call("write_register", address=1, value=5)
        await asyncio.sleep(0.8)  # it is retrying the connection
        await client.notify(
            "notifications/cancelled", {"requestId": write, "reason": "user cancelled"}
        )
        async with DropWriteRepliesDevice(write_reply="echo", port=port) as device:
            await asyncio.sleep(5)
            assert sum(device.received.values()) == 0, device.received
            # Only that call was cancelled: the server is fine and reaches the device.
            ping = envelope(await client.response(await client.call("ping")))
            assert ping["success"] is True, ping
            assert device.writes() == {}
    finally:
        await client.close()


async def test_a_cancelled_write_queued_behind_a_hung_request_is_never_sent() -> None:
    """The device hangs on a read; a write queues behind it (pymodbus serialises
    requests); the write is cancelled, then the device recovers: it must not arrive."""
    hold = asyncio.Event()
    async with DropWriteRepliesDevice(write_reply="echo", hold=hold) as device:
        client = await RawClient.start(device.port, MODBUS_CALL_TIMEOUT="20")
        try:
            read = await client.call("read_register", address=0)
            await asyncio.sleep(0.3)  # the device has the read and does not answer
            write = await client.call("write_register", address=1, value=5)
            await asyncio.sleep(0.3)  # the write waits behind the read
            await client.notify("notifications/cancelled", {"requestId": write})
            await asyncio.sleep(0.2)
            hold.set()  # the device answers the read
            assert envelope(await client.response(read))["success"] is True
            await asyncio.sleep(2)
            assert device.writes() == {}, device.received
            # The connection still works for later calls.
            later = envelope(await client.response(await client.call("read_register", address=0)))
            assert later["success"] is True
        finally:
            await client.close()


async def test_a_client_that_vanishes_entirely_is_a_clean_end() -> None:
    """The client closes stdin AND stops reading stdout while a write is pending: the
    reply cannot be delivered, which is a normal end (exit 0, no traceback), and the
    write never reaches the device that appears afterwards."""
    port = free_port()
    client = await RawClient.start(port, **RECONNECTING)
    await client.call("write_register", address=1, value=5)
    await asyncio.sleep(0.8)  # it is retrying the connection
    client.reader.cancel()
    client.proc.stdout._transport.close()  # nobody reads the replies any more
    client.proc.stdin.close()
    async with DropWriteRepliesDevice(write_reply="echo", port=port) as device:
        try:
            await asyncio.wait_for(client.proc.wait(), 15)
        finally:
            if client.proc.returncode is None:
                client.proc.kill()
                await client.proc.wait()
        await asyncio.sleep(1)
        assert sum(device.received.values()) == 0, device.received
    stderr = (await client.proc.stderr.read()).decode(errors="replace")
    assert client.proc.returncode == 0, stderr
    assert "Traceback" not in stderr, stderr


# --- one deadline per tool call, from its start, over everything it waits for ----------


async def test_a_write_queued_behind_slow_reads_is_never_sent_after_its_deadline() -> None:
    """Slow reads hold pymodbus' lock; a write queues behind them. Its deadline (2 s,
    MODBUS_CALL_TIMEOUT) runs while it waits: whatever happens, its frame must not reach
    the device after that deadline. (The MCP SDK sends no notifications/cancelled when its
    own client-side timeout fires, so this deadline is the only protection.)"""
    loop = asyncio.get_running_loop()
    async with DropWriteRepliesDevice(write_reply="echo", read_delay=1.5) as device:
        client = await RawClient.start(device.port, MODBUS_CALL_TIMEOUT="2")
        try:
            reads = [await client.call("read_register", address=0) for _ in range(3)]
            issued = loop.time()
            write = await client.call("write_register", address=7, value=1)
            result = envelope(await client.response(write, 15))
            for read in reads:
                await client.response(read, 15)
            await asyncio.sleep(2)  # anything late would arrive by now
        finally:
            await client.close()
    landed = [t for t, fc, address in device.arrivals if (fc, address) == (6, 7)]
    assert all(t <= issued + 2.0 + 0.2 for t in landed), (issued, landed)
    if landed:
        assert result["meta"]["outcome"] == "written", result
    else:
        assert result["meta"]["outcome"] == "not_sent", result
        assert result["meta"]["request_sent"] is False


async def test_one_deadline_covers_every_request_of_a_call() -> None:
    """A read split into three requests (125 registers each) shares ONE deadline: the
    third request does not get a fresh 2 s, the call fails at 2 s, the device sees no
    request after that, and the two completed chunks are reported."""
    loop = asyncio.get_running_loop()
    async with DropWriteRepliesDevice(read_delay=0.9) as device:
        client = await RawClient.start(device.port, MODBUS_CALL_TIMEOUT="2")
        try:
            started = loop.time()
            call = await client.call("read_multiple_holding_registers", address=0, count=375)
            result = envelope(await client.response(call, 15))
            await asyncio.sleep(2)
        finally:
            await client.close()
    assert result["success"] is False
    assert result["error"].startswith("no result within the 2 s deadline"), result
    assert [c["address"] for c in result["meta"]["chunks"]] == [0, 125]  # these completed
    assert len(result["meta"]["partial"]) == 250
    reads = [t for t, fc, _ in device.arrivals if fc == 3]
    assert len(reads) == 3, device.arrivals  # the third went out, nothing after it
    assert all(t <= started + 2.0 + 0.2 for t in reads), (started, reads)


async def test_a_read_pending_at_disconnect_ends_quietly() -> None:
    """A read waiting on a hung device when stdin closes: no "Task exception was never
    retrieved" (asyncio.wait_for's inner task on Python 3.10), no traceback, exit 0."""
    hold = asyncio.Event()
    async with DropWriteRepliesDevice(hold=hold) as device:
        client = await RawClient.start(device.port)
        try:
            await client.call("read_register", address=0)
            await asyncio.sleep(0.5)  # the device has the read and does not answer
            client.proc.stdin.close()
        finally:
            exited, stderr = await client.close()
            hold.set()
    assert "never retrieved" not in stderr, stderr
    assert "Traceback" not in stderr, stderr
    assert exited == 0, stderr
