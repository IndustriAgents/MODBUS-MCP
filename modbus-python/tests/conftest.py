"""Shared fakes for unit tests: an in-memory Modbus device and an MCP Context stand-in."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from types import SimpleNamespace
from typing import Any

import pytest
from pymodbus.exceptions import ConnectionException


class FakeResponse:
    def __init__(self, registers=None, bits=None, error: int | None = None, function_code=3):
        self.registers = list(registers or [])
        self.bits = list(bits or [])
        self.exception_code = error or 0
        self.function_code = function_code | 0x80 if error else function_code
        self.information: dict[int, bytes] = {}

    def isError(self) -> bool:  # noqa: N802 - pymodbus API name
        return bool(self.exception_code)

    def __str__(self) -> str:
        return f"FakeResponse(function_code={self.function_code}, exception_code={self.exception_code})"


class FakeClient:
    """In-memory device with 100 entries per table, using pymodbus 3.10+ keywords.

    `calls` records every request, so tests can assert that nothing was sent.
    """

    SIZE = 100

    def __init__(self) -> None:
        self.hr = [0] * self.SIZE
        self.ir = [0] * self.SIZE
        self.co = [False] * self.SIZE
        self.di = [False] * self.SIZE
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.connected = True
        self.connect_calls = 0
        self.close_calls = 0
        # Optional hook: called with the method name before each request; may raise.
        self.before: Callable[[str], None] | None = None

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
        address, count = kwargs.get("address", 0), kwargs.get("count", 1)
        if address < 0 or address + count > self.SIZE:
            return FakeResponse(error=2)
        return None

    # --- reads ------------------------------------------------------------
    async def read_holding_registers(self, address, *, count=1, device_id=1):
        err = self._enter("read_holding_registers", address=address, count=count, device_id=device_id)
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
        return err or FakeResponse(function_code=6)

    async def write_registers(self, address, values, *, device_id=1):
        err = self._enter(
            "write_registers", address=address, count=len(values), values=values, device_id=device_id
        )
        if not err:
            self.hr[address : address + len(values)] = list(values)
        return err or FakeResponse(function_code=16)

    async def write_coil(self, address, value, *, device_id=1):
        err = self._enter("write_coil", address=address, value=value, device_id=device_id)
        if not err:
            self.co[address] = bool(value)
        return err or FakeResponse(function_code=5)

    async def write_coils(self, address, values, *, device_id=1):
        err = self._enter(
            "write_coils", address=address, count=len(values), values=values, device_id=device_id
        )
        if not err:
            self.co[address : address + len(values)] = [bool(v) for v in values]
        return err or FakeResponse(function_code=15)

    async def mask_write_register(self, *, address=0, and_mask=0xFFFF, or_mask=0, device_id=1):
        err = self._enter("mask_write_register", address=address, device_id=device_id)
        if not err:
            current = self.hr[address]
            self.hr[address] = (current & and_mask) | (or_mask & ~and_mask & 0xFFFF)
        return err or FakeResponse(function_code=22)

    @property
    def writes(self) -> list[str]:
        return [name for name, _ in self.calls if name.startswith(("write", "mask"))]


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


@pytest.fixture
def ctx(client: FakeClient) -> FakeContext:
    from modbus_mcp import cli

    return FakeContext(cli.AppContext(modbus_client=client))
