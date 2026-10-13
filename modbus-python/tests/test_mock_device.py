"""The mock device, driven with a plain pymodbus client.

Covers the mock's own bugs: every table used to answer with exception 4 (the
datastore was wrapped in a dict twice), the initial values sat one address too
low, and simulation errors were swallowed silently.
"""

from __future__ import annotations

import asyncio
import time

import pytest
from conftest import running_mock
from pymodbus.client import AsyncModbusTcpClient

from modbus_mcp import cli

pytestmark = pytest.mark.integration

HOLDING = [50, 0, 0, 0, 0, 0, 0, 0, 0, 0]  # README: Valve Position 50, the rest 0


@pytest.fixture(scope="module")
def mock(tmp_path_factory):
    with running_mock(tmp_path_factory.mktemp("mock-device")) as device:
        yield device


def modbus(mock, fn):
    """Run `fn(client, unit)` against the mock with a fresh connection."""

    async def go():
        client = AsyncModbusTcpClient(mock.host, port=mock.port)
        assert await client.connect()
        try:
            return await fn(client, cli._unit(client, 1))
        finally:
            client.close()

    return asyncio.run(go())


def test_initial_values_are_at_the_documented_addresses(mock) -> None:
    async def read(client, unit):
        hr = await client.read_holding_registers(0, count=10, **unit)
        co = await client.read_coils(0, count=2, **unit)
        di = await client.read_discrete_inputs(0, count=2, **unit)
        ir = await client.read_input_registers(0, count=9, **unit)
        return hr, co, di, ir

    hr, co, di, ir = modbus(mock, read)
    for response in (hr, co, di, ir):
        assert not response.isError(), response  # used to be exception 4 for every table
    assert hr.registers == HOLDING
    assert co.bits[:2] == [False, False]
    assert di.bits[:2] == [False, False]
    temperature, pressure, flow, level, _vib, ph, humidity, motor, _total = ir.registers
    assert 230 <= temperature <= 270  # 25.0 C +/- 2, x10
    assert 950 <= pressure <= 1100
    assert flow <= 10  # pump off
    assert 600 <= level <= 900  # ~75 %, x10
    assert 600 <= ph <= 800
    assert 200 <= humidity <= 800
    assert motor == 0


def test_tables_hold_100_entries(mock) -> None:
    async def read(client, unit):
        last = await client.read_holding_registers(99, count=1, **unit)
        past = await client.read_holding_registers(100, count=1, **unit)
        return last, past

    last, past = modbus(mock, read)
    assert not last.isError()
    assert past.isError() and past.exception_code == 2  # Illegal Data Address


def test_device_identification(mock) -> None:
    async def read(client, unit):
        return await client.read_device_information(read_code=3, **unit)

    info = modbus(mock, read).information
    assert info[0] == b"Mock ICS" and info[1] == b"MODBUS-MOCK" and info[2] == b"0.1"


def test_simulation_runs_and_processes_commands(mock) -> None:
    async def scenario(client, unit):
        async def until(read, predicate, timeout=5.0):
            deadline = time.monotonic() + timeout
            while True:
                value = await read()
                if predicate(value) or time.monotonic() > deadline:
                    return value
                await asyncio.sleep(0.1)

        assert not (await client.write_register(1, 100, **unit)).isError()  # heater 100 %
        temp = await until(
            lambda: client.read_input_registers(0, count=1, **unit),
            lambda r: r.registers[0] > 600,
        )
        assert not (await client.write_register(4, 10, **unit)).isError()  # production 10/h
        assert not (await client.write_register(6, 1, **unit)).isError()  # start command
        hr = await until(
            lambda: client.read_holding_registers(0, count=10, **unit),
            lambda r: r.registers[6] == 0 and r.registers[5] == 1,
        )
        pump = await client.read_coils(0, count=1, **unit)
        running = await until(
            lambda: client.read_discrete_inputs(0, count=2, **unit), lambda r: r.bits[1]
        )
        # Put the controls back for the next test
        await client.write_register(7, 1, **unit)  # stop
        await client.write_registers(1, [0, 0, 0, 0], **unit)
        return temp, hr, pump, running

    temp, hr, pump, running = modbus(mock, scenario)
    assert temp.registers[0] > 600  # 25 + 100 * 0.5 = 75 C
    assert hr.registers[6] == 0  # the start command was consumed
    assert hr.registers[5] == 1  # AUTO
    assert pump.bits[0] is True
    assert running.bits[1] is True


def test_simulation_errors_are_not_silent(mock) -> None:
    time.sleep(0.5)
    assert "Simulation step failed" not in mock.log()
