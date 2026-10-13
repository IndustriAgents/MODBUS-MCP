"""Shared fakes for unit tests: an in-memory Modbus device and an MCP Context stand-in."""

from __future__ import annotations

import asyncio
import contextlib
import os
import shutil
import signal
import socket
import subprocess
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from pymodbus.exceptions import ConnectionException, ModbusIOException


class FakeResponse:
    def __init__(
        self,
        registers=None,
        bits=None,
        error: int | None = None,
        function_code=3,
        address=0,
        count=0,
        and_mask=None,
        or_mask=None,
    ):
        self.registers = list(registers or [])
        self.bits = list(bits or [])
        self.address, self.count = address, count
        self.and_mask, self.or_mask = and_mask, or_mask
        self.exception_code = error or 0
        self.function_code = function_code | 0x80 if error else function_code
        self.information: dict[int, bytes] = {}

    def isError(self) -> bool:  # noqa: N802 - pymodbus API name
        return bool(self.exception_code)

    def __str__(self) -> str:
        return f"FakeResponse(fc={self.function_code}, exception_code={self.exception_code})"


class FakeClient:
    """In-memory device with 100 entries per table, using pymodbus 3.10+ keywords.

    `calls` records every request, so tests can assert that nothing was sent.
    """

    SIZE = 100
    FUNCTION_CODES = {
        "read_coils": 1,
        "read_discrete_inputs": 2,
        "read_holding_registers": 3,
        "read_input_registers": 4,
        "write_coil": 5,
        "write_register": 6,
        "write_coils": 15,
        "write_registers": 16,
        "mask_write_register": 22,
        "read_device_information": 43,
    }

    def __init__(self) -> None:
        self.hr = [0] * self.SIZE
        self.ir = [0] * self.SIZE
        self.co = [False] * self.SIZE
        self.di = [False] * self.SIZE
        # Every request the server tried to make, and the ones that reached the device
        # (pymodbus raises ConnectionException before sending, so those never do).
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.sent: list[tuple[str, dict[str, Any]]] = []
        # Apply writes but lose the reply (ModbusIOException), or delay it.
        self.drop_write_replies = False
        self.write_reply_delay = 0.0
        self.connected = True
        self.connect_calls = 0
        self.close_calls = 0
        # Optional hook: called with the method name before each request; may raise.
        self.before: Callable[[str], None] | None = None
        # Like pymodbus' trace_packet: called right before a request goes out (make_ctx
        # installs cli.SendGate here, which may refuse the send).
        self.trace_packet: Callable[[bool, bytes], bytes] | None = None

    # --- connection -------------------------------------------------------
    async def connect(self) -> bool:
        self.connect_calls += 1
        self.connected = True
        return True

    def close(self) -> None:
        self.close_calls += 1
        self.connected = False

    # --- helpers ----------------------------------------------------------
    def _enter(self, name: str, **kwargs: Any) -> FakeResponse | None:
        self.calls.append((name, kwargs))
        if self.before is not None:
            self.before(name)
        if not self.connected:
            raise ConnectionException("Not connected[fake]")
        if self.trace_packet is not None:
            self.trace_packet(True, b"")
        self.sent.append((name, kwargs))
        address, count = kwargs.get("address", 0), kwargs.get("count", 1)
        if address < 0 or address + count > self.SIZE:
            return FakeResponse(error=2, function_code=self.FUNCTION_CODES[name])
        return None

    # --- reads ------------------------------------------------------------
    async def read_holding_registers(self, address, *, count=1, device_id=1):
        err = self._enter(
            "read_holding_registers", address=address, count=count, device_id=device_id
        )
        return err or FakeResponse(registers=self.hr[address : address + count])

    async def read_input_registers(self, address, *, count=1, device_id=1):
        err = self._enter("read_input_registers", address=address, count=count, device_id=device_id)
        return err or FakeResponse(registers=self.ir[address : address + count], function_code=4)

    async def read_coils(self, address, *, count=1, device_id=1):
        err = self._enter("read_coils", address=address, count=count, device_id=device_id)
        # pymodbus pads bit responses to a multiple of 8
        bits = self.co[address : address + count] + [False] * (-count % 8)
        return err or FakeResponse(bits=bits, function_code=1)

    async def read_discrete_inputs(self, address, *, count=1, device_id=1):
        err = self._enter("read_discrete_inputs", address=address, count=count, device_id=device_id)
        bits = self.di[address : address + count] + [False] * (-count % 8)
        return err or FakeResponse(bits=bits, function_code=2)

    async def read_device_information(self, *, read_code=None, object_id=0, device_id=1):
        self._enter("read_device_information", read_code=read_code, object_id=object_id)
        response = FakeResponse(function_code=0x2B)
        response.information = {0: b"Fake Vendor", 1: b"FAKE-1", 2: b"1.0"}
        return response

    # --- writes -----------------------------------------------------------
    async def write_register(self, address, value, *, device_id=1):
        err = self._enter("write_register", address=address, value=value, device_id=device_id)
        if not err:
            self.hr[address] = value
        return err or await self._reply(
            FakeResponse(function_code=6, address=address, registers=[value])
        )

    async def write_registers(self, address, values, *, device_id=1):
        err = self._enter(
            "write_registers",
            address=address,
            count=len(values),
            values=values,
            device_id=device_id,
        )
        if not err:
            self.hr[address : address + len(values)] = list(values)
        return err or await self._reply(
            FakeResponse(function_code=16, address=address, count=len(values))
        )

    async def write_coil(self, address, value, *, device_id=1):
        err = self._enter("write_coil", address=address, value=value, device_id=device_id)
        if not err:
            self.co[address] = bool(value)
        return err or await self._reply(
            FakeResponse(function_code=5, address=address, bits=[bool(value)])
        )

    async def write_coils(self, address, values, *, device_id=1):
        err = self._enter(
            "write_coils", address=address, count=len(values), values=values, device_id=device_id
        )
        if not err:
            self.co[address : address + len(values)] = [bool(v) for v in values]
        return err or await self._reply(
            FakeResponse(function_code=15, address=address, count=len(values))
        )

    async def mask_write_register(self, *, address=0, and_mask=0xFFFF, or_mask=0, device_id=1):
        err = self._enter("mask_write_register", address=address, device_id=device_id)
        if not err:
            current = self.hr[address]
            self.hr[address] = (current & and_mask) | (or_mask & ~and_mask & 0xFFFF)
        return err or await self._reply(
            FakeResponse(function_code=22, address=address, and_mask=and_mask, or_mask=or_mask)
        )

    async def _reply(self, response: FakeResponse) -> FakeResponse:
        """A write reached the device and was applied; now the reply may get lost."""
        if self.write_reply_delay:
            await asyncio.sleep(self.write_reply_delay)
        if self.drop_write_replies:
            raise ModbusIOException("No response received after 0 retries")
        return response

    @property
    def writes(self) -> list[str]:
        return [name for name, _ in self.calls if name.startswith(("write", "mask"))]

    @property
    def sent_writes(self) -> list[str]:
        return [name for name, _ in self.sent if name.startswith(("write", "mask"))]


class FakeContext:
    """Stands in for mcp's Context: records log calls, which only happen if they are awaited."""

    def __init__(self, lifespan_context: Any) -> None:
        self.request_context = SimpleNamespace(lifespan_context=lifespan_context)
        self.logs: list[tuple[str, str]] = []

    async def info(self, message: str) -> None:
        self.logs.append(("info", message))

    async def error(self, message: str) -> None:
        self.logs.append(("error", message))

    async def warning(self, message: str) -> None:
        self.logs.append(("warning", message))

    async def debug(self, message: str) -> None:
        self.logs.append(("debug", message))


def run(coro):
    return asyncio.run(coro)


@pytest.fixture
def client() -> FakeClient:
    return FakeClient()


def make_ctx(client: FakeClient, tags: dict | None = None, **settings: Any) -> FakeContext:
    """A context whose lifespan state holds `client`, Settings(**settings) and the tag map."""
    from modbus_mcp import cli
    from modbus_mcp.config import Settings, parse_tag_map

    app = cli.AppContext(
        modbus_client=client,
        settings=Settings(**{"retry_backoff_base": 0.0, **settings}),
        tags=parse_tag_map(tags or {}),
        frames_sent=lambda: len(client.sent),
    )
    client.trace_packet = cli.SendGate(lambda: app.closing())  # as the lifespan does
    return FakeContext(app)


@pytest.fixture
def ctx(client: FakeClient) -> FakeContext:
    """Default settings: writes disabled."""
    return make_ctx(client)


@pytest.fixture
def wctx(client: FakeClient) -> FakeContext:
    """Writes enabled."""
    return make_ctx(client, writes_enabled=True)


# --- the mock device, as a real process --------------------------------------------------

MOCK_DIR = Path(__file__).resolve().parents[2] / "modbus-mock-server"


def free_port() -> int:
    """A TCP port nothing is listening on (the OS picks it), so parallel runs do not clash."""
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


@dataclass
class MockDevice:
    host: str
    port: int
    process: subprocess.Popen
    log_path: Path

    def log(self) -> str:
        return self.log_path.read_text(errors="replace")


def stop_process(process: subprocess.Popen) -> None:
    """Stop a mock started by start_mock(): close its stdin, then signal its group."""
    if process.stdin is not None and not process.stdin.closed:
        with contextlib.suppress(OSError):
            process.stdin.close()  # --exit-on-stdin-eof: the mock stops by itself
        with contextlib.suppress(subprocess.TimeoutExpired):
            process.wait(timeout=5)
    if process.poll() is not None:
        return
    # `uv run` starts the mock as a child; signal the whole process group.
    with contextlib.suppress(ProcessLookupError):
        if hasattr(os, "killpg"):
            os.killpg(process.pid, signal.SIGTERM)
        else:  # pragma: no cover - Windows
            process.terminate()
    try:
        process.wait(timeout=10)
    except subprocess.TimeoutExpired:
        with contextlib.suppress(ProcessLookupError):
            if hasattr(os, "killpg"):
                os.killpg(process.pid, signal.SIGKILL)
            else:  # pragma: no cover
                process.kill()
        process.wait(timeout=10)


def start_mock(log_path: Path, port: int, update_interval: float = 0.2) -> subprocess.Popen:
    """Start modbus-mock-server (in its own uv project) on 127.0.0.1:`port`.

    Its stdin is a pipe this process holds, and the mock runs with
    --exit-on-stdin-eof: if this process dies, even by SIGKILL, the pipe closes
    and the mock exits instead of being orphaned.
    """
    uv = shutil.which("uv")
    if uv is None:
        pytest.skip(
            "uv is needed to run the mock device (modbus-mock-server is its own uv project)"
        )
    env = {
        **os.environ,
        "MODBUS_HOST": "127.0.0.1",
        "MODBUS_PORT": str(port),
        "UPDATE_INTERVAL": str(update_interval),
    }
    env.pop("VIRTUAL_ENV", None)  # the mock has its own environment, not this one
    command = [uv, "run", "--locked", "--quiet", "--directory", str(MOCK_DIR)]
    command += ["modbus-mock-server", "--exit-on-stdin-eof"]
    with open(log_path, "wb") as log_file:
        return subprocess.Popen(
            command,
            env=env,
            stdin=subprocess.PIPE,
            stdout=log_file,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )


def wait_until_listening(host: str, port: int, process: subprocess.Popen, log_path: Path) -> None:
    deadline = time.monotonic() + 180  # the first run may have to create the mock's venv
    while True:
        if process.poll() is not None:
            log = log_path.read_text(errors="replace")
            pytest.fail(f"mock device exited with {process.returncode}:\n{log}")
        try:
            socket.create_connection((host, port), timeout=0.5).close()
            return
        except OSError:
            if time.monotonic() > deadline:
                log = log_path.read_text(errors="replace")
                pytest.fail(f"mock device did not start listening:\n{log}")
            time.sleep(0.2)


@contextlib.contextmanager
def running_mock(log_dir: Path, update_interval: float = 0.2, port: int | None = None):
    """Start the mock on `port` or a free port; stop it afterwards."""
    port = port or free_port()
    log_path = log_dir / f"mock-{port}.log"
    process = start_mock(log_path, port, update_interval)
    device = MockDevice("127.0.0.1", port, process, log_path)
    try:
        wait_until_listening(device.host, port, process, log_path)
        yield device
    finally:
        stop_process(process)
