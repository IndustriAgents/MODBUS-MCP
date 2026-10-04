"""Reconnect after a dropped connection; ping counts any Modbus reply as reachable.

The reconnect branch ported from IndustriConnect#29 caught ConnectionError/OSError,
but pymodbus raises ConnectionException (a ModbusException) when the connection
is down, so it never ran. These tests use that exception.
"""

from __future__ import annotations

from conftest import FakeResponse, make_ctx, run
from pymodbus.exceptions import ConnectionException, ModbusIOException

from modbus_mcp import cli


def test_reconnects_before_the_first_attempt_when_the_connection_is_down(client, ctx) -> None:
    client.connected = False
    client.hr[0] = 50
    result = run(cli.read_register(0, ctx))
    assert result["success"], result
    assert result["data"] == {"value": 50}
    assert (client.close_calls, client.connect_calls) == (1, 1)
    assert any("reconnected" in message for _, message in ctx.logs)


def test_reconnects_and_retries_when_the_connection_drops_mid_call(client, ctx) -> None:
    dropped = []

    def drop_once(name: str) -> None:
        if not dropped:
            dropped.append(name)
            client.connected = False
            raise ConnectionException("Not connected[fake]")

    client.before = drop_once
    client.ir[0:4] = [263, 1037, 0, 36]
    result = run(cli.read_input_registers(0, 4, ctx))
    assert result["success"], result
    assert result["data"]["registers"] == [263, 1037, 0, 36]
    assert result["meta"]["chunks"][0]["attempts"] == 2
    assert client.connect_calls == 1


def test_failed_reconnect_is_reported_not_hidden(client) -> None:
    ctx = make_ctx(client, max_retries=1)
    client.connected = False

    async def refuse() -> bool:
        client.connect_calls += 1
        return False

    client.connect = refuse
    result = run(cli.read_register(0, ctx))
    assert result["success"] is False
    assert "ConnectionException" in result["error"]
    assert client.connect_calls == 2  # once before each attempt
    assert any("reconnect to" in m and "failed" in m for level, m in ctx.logs if level == "error")


def test_no_reconnect_while_connected(client, ctx) -> None:
    def time_out(name: str) -> None:
        raise ModbusIOException("No response received after 3 retries")

    client.before = time_out
    result = run(cli.read_register(0, ctx))
    assert result["success"] is False
    assert result["meta"]["attempts"] == 3  # 1 + MODBUS_MAX_RETRIES (2)
    assert client.connect_calls == 0


def test_log_calls_are_awaited(client, ctx) -> None:
    # FakeContext only records a message when the coroutine actually runs.
    run(cli.read_register(0, ctx))
    assert ("info", "Read register 0 from slave 1: 0") in ctx.logs
    client.before = lambda name: (_ for _ in ()).throw(ModbusIOException("boom"))
    run(cli.read_coils(0, 1, ctx))
    assert any(level == "error" and "boom" in m for level, m in ctx.logs)


def test_ping_reports_a_healthy_device(client, ctx) -> None:
    result = run(cli.ping(ctx))
    assert result["success"] is True and result["error"] is None
    data = result["data"]
    assert data["connected"] is True and data["device_reachable"] is True
    assert data["probe_response"] == "ok" and data["probe_error"] is None
    assert data["probe_address"] == 0 and data["probe_slave_id"] == 1


def test_ping_counts_an_exception_reply_as_reachable(client, ctx) -> None:
    async def no_such_register(address, *, count=1, device_id=1):
        return FakeResponse(error=2, function_code=3)

    client.read_holding_registers = no_such_register
    result = run(cli.ping(ctx))
    assert result["success"] is True
    assert result["data"]["device_reachable"] is True
    assert result["data"]["probe_response"].startswith("Modbus exception 2 (Illegal Data Address)")


def test_ping_reports_an_unreachable_device_as_failure(client, ctx) -> None:
    client.before = lambda name: (_ for _ in ()).throw(ModbusIOException("No response"))
    result = run(cli.ping(ctx))
    assert result["success"] is False
    assert result["data"]["device_reachable"] is False
    assert "did not answer" in result["error"]
    assert result["data"]["probe_response"] is None


def test_ping_can_probe_another_address(client, ctx) -> None:
    result = run(cli.ping(ctx, address=7, slave_id=3))
    assert result["success"]
    assert client.calls[-1] == ("read_holding_registers", {"address": 7, "count": 1, "device_id": 3})
