"""The unit-id keyword pymodbus expects: `slave` up to 3.9, `device_id` from 3.10."""

from __future__ import annotations

import inspect

from pymodbus.client import AsyncModbusSerialClient, AsyncModbusTcpClient, AsyncModbusUdpClient

from modbus_mcp import cli

CLIENT_METHODS = [
    "read_holding_registers",
    "read_input_registers",
    "read_coils",
    "read_discrete_inputs",
    "write_register",
    "write_registers",
    "write_coil",
    "write_coils",
    "mask_write_register",
    "read_device_information",
]


class _OldClient:
    async def read_holding_registers(self, address, *, count=1, slave=1):  # pymodbus <= 3.9
        raise NotImplementedError


class _NewClient:
    async def read_holding_registers(self, address, *, count=1, device_id=1):  # pymodbus >= 3.10
        raise NotImplementedError


def test_unit_keyword_follows_the_client_signature() -> None:
    assert cli._unit(_OldClient(), 7) == {"slave": 7}
    assert cli._unit(_NewClient(), 7) == {"device_id": 7}


def test_installed_pymodbus_accepts_the_chosen_keyword_everywhere() -> None:
    for client_type in (AsyncModbusTcpClient, AsyncModbusUdpClient, AsyncModbusSerialClient):
        keyword = cli._unit_kwarg_for(client_type)
        for method in CLIENT_METHODS:
            params = inspect.signature(getattr(client_type, method)).parameters
            assert keyword in params, (client_type.__name__, method, keyword)


def test_serial_client_can_be_created() -> None:
    """pymodbus' serial client needs pyserial (the pymodbus[serial] extra)."""
    import asyncio

    from modbus_mcp.config import Settings

    async def build():
        client = cli._make_client(Settings(modbus_type="serial", serial_port="/dev/null"))
        client.close()
        return type(client)

    assert asyncio.run(build()) is AsyncModbusSerialClient
