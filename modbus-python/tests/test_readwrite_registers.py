"""FC23 semantics, validation and no-replay behavior on the common transport."""

import pytest
from conftest import FakeResponse, make_ctx, run

from modbus_mcp import cli


def test_overlap_is_read_after_write_in_one_request(client):
    ctx = make_ctx(client, writes_enabled=True)
    result = run(cli.readwrite_registers(1, 3, 2, [65535, 42], ctx))
    assert result["data"] == {"written": 2, "registers": [0, 65535, 42]}
    assert result["meta"]["outcome"] == "written"
    assert client.sent_writes == ["readwrite_registers"]


@pytest.mark.parametrize(
    "read_address,read_count,write_address,values",
    [
        (0, 126, 0, [1]),
        (0, 0, 0, [1]),
        (0, 1, 0, [1] * 122),
        (65535, 2, 0, [1]),
        (0, 1, 65535, [1, 2]),
        (0, 1, 0, [True]),
        (0, 1, 0, [65536]),
        (True, 1, 0, [1]),
        (0, 1, 0, []),
    ],
)
def test_preflight_sends_nothing(client, read_address, read_count, write_address, values):
    result = run(
        cli.readwrite_registers(
            read_address, read_count, write_address, values, make_ctx(client, writes_enabled=True)
        )
    )
    assert result["meta"]["outcome"] == "not_sent"
    assert not client.calls


def test_disabled_sends_nothing(client):
    result = run(cli.readwrite_registers(0, 1, 0, [1], make_ctx(client)))
    assert result["meta"]["outcome"] == "not_sent"
    assert not client.calls


def test_lost_reply_never_repeats_combined_write(client):
    client.drop_write_replies = True
    result = run(
        cli.readwrite_registers(0, 1, 0, [7], make_ctx(client, writes_enabled=True, max_retries=5))
    )
    assert result["meta"]["outcome"] == "unknown"
    assert result["meta"]["request_sent"] is True
    assert client.sent_writes == ["readwrite_registers"]
    assert client.hr[0] == 7


@pytest.mark.parametrize(
    "response",
    [
        FakeResponse(function_code=23, registers=[]),
        FakeResponse(function_code=3, registers=[7]),
        FakeResponse(function_code=23, error=5),
    ],
)
def test_invalid_or_deferred_reply_is_unknown_and_not_replayed(client, response):
    async def reply(_):
        return response

    client._reply = reply
    result = run(
        cli.readwrite_registers(0, 1, 0, [7], make_ctx(client, writes_enabled=True, max_retries=5))
    )
    assert result["meta"]["outcome"] == "unknown"
    assert client.sent_writes == ["readwrite_registers"]


def test_fc23_maximum_quantities_fit_one_request(client):
    client.SIZE = 300
    client.hr = [0] * 300
    result = run(
        cli.readwrite_registers(0, 125, 0, list(range(121)), make_ctx(client, writes_enabled=True))
    )
    assert result["success"]
    assert result["data"]["registers"] == list(range(121)) + [0] * 4
    assert client.sent_writes == ["readwrite_registers"]


@pytest.mark.parametrize("transport,success", [("serial", False), ("tcp", True)])
def test_unit_zero_requires_unicast_only_on_serial(client, transport, success):
    result = run(
        cli.readwrite_registers(
            0, 1, 0, [7], make_ctx(client, writes_enabled=True, modbus_type=transport), slave_id=0
        )
    )
    assert result["success"] is success
    if not success:
        assert result["meta"]["outcome"] == "not_sent"
        assert client.calls == []


@pytest.mark.parametrize("unit", [248, 255])
def test_reserved_serial_unit_id_is_not_sent(client, unit):
    result = run(
        cli.readwrite_registers(
            0, 1, 0, [7], make_ctx(client, writes_enabled=True, modbus_type="serial"), slave_id=unit
        )
    )
    assert result["meta"]["outcome"] == "not_sent"
    assert not client.calls
