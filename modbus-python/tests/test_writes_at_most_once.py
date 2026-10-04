"""A write reaches the device at most once; a lost reply is reported, never re-sent.

Reads may be retried. Writes are retried only when nothing was sent: the
connection could not be opened, or pymodbus raised ConnectionException, which
it does before sending anything.
"""

from __future__ import annotations

import asyncio

import pytest
from conftest import FakeResponse, make_ctx, run
from pymodbus.exceptions import ConnectionException

from modbus_mcp import cli
from modbus_mcp.config import Settings

TAGS = {
    "Speed": {"table": "holding", "address": 10, "dtype": "float32"},
    "Pump": {"table": "coil", "address": 0},
}

WRITE_CALLS = {
    "write_register": lambda ctx: cli.write_register(1, 50, ctx),
    "write_registers": lambda ctx: cli.write_registers(1, [1, 2], ctx),
    "write_coil": lambda ctx: cli.write_coil(0, True, ctx),
    "write_coils_bulk": lambda ctx: cli.write_coils_bulk(0, [True, False], ctx),
    "mask_write_register": lambda ctx: cli.mask_write_register(5, 0xFFFC, 3, ctx),
    "write_tag (holding)": lambda ctx: cli.write_tag("Speed", 42.5, ctx),
    "write_tag (coil)": lambda ctx: cli.write_tag("Pump", True, ctx),
}


@pytest.mark.parametrize("name", list(WRITE_CALLS))
def test_a_write_whose_reply_is_lost_is_sent_once(client, name: str) -> None:
    client.drop_write_replies = True
    ctx = make_ctx(client, TAGS, writes_enabled=True, max_retries=5)
    result = run(WRITE_CALLS[name](ctx))
    assert result["success"] is False
    assert result["error"] == cli.WRITE_NO_REPLY
    assert result["error"] == "no reply; the write may have been applied; check before retrying"
    assert result["meta"]["outcome"] == "unknown" and result["meta"]["request_sent"] is True
    assert "ModbusIOException" in result["meta"]["cause"]
    assert result["meta"]["attempts"] == 1
    assert len(client.sent_writes) == 1, client.sent


def test_the_lost_write_may_indeed_have_been_applied(client) -> None:
    client.drop_write_replies = True
    ctx = make_ctx(client, writes_enabled=True)
    assert run(cli.write_register(1, 50, ctx))["error"] == cli.WRITE_NO_REPLY
    assert client.hr[1] == 50
    assert run(cli.read_register(1, ctx))["data"] == {"value": 50}


def test_a_write_cut_off_by_the_tool_timeout_is_sent_once(client) -> None:
    client.write_reply_delay = 1.0
    ctx = make_ctx(client, writes_enabled=True, max_retries=5, tool_timeout=0.05)
    result = run(cli.write_registers(3, [7, 8], ctx))
    assert result["error"] == cli.WRITE_NO_REPLY
    assert "TimeoutError" in result["meta"]["cause"]
    assert client.sent_writes == ["write_registers"]


def test_a_write_that_was_not_sent_is_retried_after_reconnecting(client) -> None:
    def drop_connection_once(name: str) -> None:
        if not client.calls[:-1]:  # first request only
            client.connected = False
            raise ConnectionException("Not connected[fake]")  # pymodbus: before sending

    client.before = drop_connection_once
    ctx = make_ctx(client, writes_enabled=True)
    result = run(cli.write_register(1, 50, ctx))
    assert result["success"], result
    assert result["meta"]["attempts"] == 2
    assert result["meta"]["outcome"] == "written" and result["meta"]["request_sent"] is True
    assert client.connect_calls == 1
    assert client.sent_writes == ["write_register"]
    assert client.hr[1] == 50


def test_an_unreachable_device_gets_no_write(client) -> None:
    client.connected = False

    async def refuse() -> bool:
        return False

    client.connect = refuse
    ctx = make_ctx(client, writes_enabled=True)
    result = run(cli.write_register(1, 50, ctx))
    assert result["success"] is False
    assert result["error"] == "cannot reach device at 127.0.0.1:502"
    assert result["meta"]["outcome"] == "not_sent" and result["meta"]["request_sent"] is False
    assert client.calls == []


def test_a_write_the_device_rejects(client) -> None:
    ctx = make_ctx(client, writes_enabled=True)
    result = run(cli.write_register(150, 1, ctx))  # the fake device has 100 registers
    assert result["error"].startswith("Modbus exception 2 (Illegal Data Address)")
    assert result["meta"]["outcome"] == "rejected" and result["meta"]["request_sent"] is True


def test_a_write_refused_by_the_server_is_not_sent(client) -> None:
    ctx = make_ctx(client, writes_enabled=True)
    result = run(cli.write_registers(65535, [1, 2], ctx))  # runs past the address space
    assert "runs past" in result["error"]
    assert result["meta"]["outcome"] == "not_sent" and result["meta"]["request_sent"] is False
    assert client.calls == []


def test_reads_are_still_retried(client) -> None:
    def no_reply(name: str) -> None:
        if len(client.calls) < 3:
            raise asyncio.TimeoutError()

    client.before = no_reply
    ctx = make_ctx(client)  # MODBUS_MAX_RETRIES defaults to 2
    result = run(cli.read_register(0, ctx))
    assert result["success"], result
    assert result["meta"]["attempts"] == 3


@pytest.mark.parametrize("modbus_type", ["tcp", "udp", "serial"])
def test_pymodbus_does_not_resend_either(modbus_type: str) -> None:
    """pymodbus re-sends a request on timeout `retries` times (default 3); ours use 0."""

    async def build():
        try:
            client = cli._make_client(Settings(modbus_type=modbus_type, serial_port="/dev/null"))
        except ImportError as exc:  # serial without pyserial installed
            pytest.skip(str(exc))
        try:
            return client.ctx.retries
        finally:
            client.close()

    assert asyncio.run(build()) == 0


# --- what counts as "rejected" after a send (suite rule 1a) ---------------------------

REFUSAL_CODES = [1, 2, 3, 6, 10]  # the request was refused: nothing was done
NOT_A_REFUSAL = [4, 5, 8, 11, 99]  # failed while acting, accepted, or no answer from the target


def reply_with(client, response) -> None:
    async def write_register(address, value, *, device_id=1):
        client.sent.append(("write_register", {"address": address, "value": value}))
        return response(address, value)

    client.write_register = write_register


@pytest.mark.parametrize("code", REFUSAL_CODES)
def test_a_refusing_exception_is_rejected(client, code: int) -> None:
    reply_with(client, lambda a, v: FakeResponse(error=code, function_code=6))
    result = run(cli.write_register(1, 50, make_ctx(client, writes_enabled=True)))
    assert result["meta"]["outcome"] == "rejected" and result["meta"]["request_sent"] is True
    assert result["meta"]["exception_code"] == code
    assert "may have been applied" not in result["error"]


@pytest.mark.parametrize("code", NOT_A_REFUSAL)
def test_other_exceptions_leave_the_write_unknown(client, code: int) -> None:
    reply_with(client, lambda a, v: FakeResponse(error=code, function_code=6))
    result = run(cli.write_register(1, 50, make_ctx(client, writes_enabled=True)))
    assert result["success"] is False
    assert result["meta"]["outcome"] == "unknown" and result["meta"]["request_sent"] is True
    assert result["error"].endswith("the write may have been applied; check before retrying")
    assert len(client.sent) == 1


@pytest.mark.parametrize(
    ("reply", "field"),
    [
        (lambda a, v: FakeResponse(function_code=6, address=a, registers=[v + 1]), "registers"),
        (lambda a, v: FakeResponse(function_code=6, address=a + 1, registers=[v]), "address"),
        (lambda a, v: FakeResponse(function_code=3, address=a, registers=[v]), "function_code"),
        (lambda a, v: FakeResponse(function_code=6, address=a), "registers"),  # empty echo
    ],
)
def test_a_reply_that_does_not_echo_the_write_is_unknown(client, reply, field) -> None:
    reply_with(client, reply)
    result = run(cli.write_register(1, 50, make_ctx(client, writes_enabled=True)))
    assert result["success"] is False
    assert result["meta"]["outcome"] == "unknown" and result["meta"]["request_sent"] is True
    assert field in result["meta"]["reply_mismatch"]
    assert result["error"] == (
        "the device's reply does not match the write; "
        "the write may have been applied; check before retrying"
    )


@pytest.mark.parametrize("name", list(WRITE_CALLS))
def test_a_correct_echo_is_written(client, name: str) -> None:
    result = run(WRITE_CALLS[name](make_ctx(client, TAGS, writes_enabled=True)))
    assert result["success"], result
    assert result["meta"]["outcome"] == "written"


# --- failures before the request went out are not_sent (rule 1a, clarified) -------------


def test_a_local_encoding_failure_is_not_sent(client) -> None:
    async def write_register(address, value, *, device_id=1):
        raise ValueError("cannot encode")  # e.g. while building the frame; nothing sent

    client.write_register = write_register
    result = run(cli.write_register(1, 50, make_ctx(client, writes_enabled=True)))
    assert result["meta"]["outcome"] == "not_sent" and result["meta"]["request_sent"] is False
    assert result["error"] == "the write was not sent: Unexpected ValueError: cannot encode"
    assert "may have been applied" not in result["error"]


def test_a_timeout_before_sending_is_not_sent(client) -> None:
    async def write_register(address, value, *, device_id=1):
        await asyncio.sleep(1)  # e.g. waiting for pymodbus' lock behind another request
        return client._enter("write_register", address=address, value=value)

    client.write_register = write_register
    ctx = make_ctx(client, writes_enabled=True, tool_timeout=0.05)
    result = run(cli.write_register(1, 50, ctx))
    assert result["meta"]["outcome"] == "not_sent" and result["meta"]["request_sent"] is False
    assert result["error"].startswith("the write was not sent: TimeoutError")
    assert client.sent == []


def test_an_unexpected_failure_after_sending_is_unknown(client) -> None:
    async def write_register(address, value, *, device_id=1):
        client._enter("write_register", address=address, value=value)  # it went out
        raise ValueError("bad reply")

    client.write_register = write_register
    result = run(cli.write_register(1, 50, make_ctx(client, writes_enabled=True)))
    assert result["meta"]["outcome"] == "unknown" and result["meta"]["request_sent"] is True
    assert result["error"].endswith("the write may have been applied; check before retrying")


def test_the_frame_counter_counts_only_frames_being_sent() -> None:
    counter = cli.FrameCounter()
    assert counter(True, b"\x01") == b"\x01" and counter(False, b"\x02") == b"\x02"
    assert counter.sent == 1


def test_the_real_client_gets_the_frame_counter() -> None:
    async def build():
        counter = cli.FrameCounter()
        client = cli._make_client(Settings(), counter)
        try:
            return client.ctx.trace_packet is counter
        finally:
            client.close()

    assert asyncio.run(build())
