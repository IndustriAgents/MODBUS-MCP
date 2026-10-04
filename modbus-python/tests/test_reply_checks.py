"""A reply is used only if it belongs to the request (function code, count, echo)."""

from __future__ import annotations

import struct

import pytest
from conftest import FakeResponse, make_ctx, run

from modbus_mcp import cli


def respond_with(client, method: str, response: FakeResponse) -> None:
    async def reply(*args, **kwargs):
        client._enter(method, address=kwargs.get("address", args[0] if args else 0))
        return response

    setattr(client, method, reply)


def test_a_reply_with_another_function_code_is_not_a_read(client) -> None:
    respond_with(client, "read_holding_registers", FakeResponse(registers=[9], function_code=4))
    result = run(cli.read_register(0, make_ctx(client, max_retries=1)))
    assert result["success"] is False
    assert result["error"] == "the device's reply does not match the request"
    assert result["meta"]["reply_mismatch"]["function_code"] == {"expected": 3, "got": 4}
    assert result["meta"]["attempts"] == 2  # a read may be retried
    # Each mismatch closed the connection (desync), so the retry reconnected first,
    # and the connection is left closed after the second mismatch.
    assert client.connect_calls == 1 and client.connected is False


def test_an_exception_for_another_function_is_not_a_reads_answer(client) -> None:
    respond_with(client, "read_input_registers", FakeResponse(error=2, function_code=3))
    result = run(cli.read_input_registers(0, 1, make_ctx(client, max_retries=0)))
    assert result["success"] is False
    assert result["meta"]["reply_mismatch"]["function_code"] == {"expected": 0x84, "got": 0x83}


def test_an_exception_for_another_function_does_not_make_a_write_rejected(client) -> None:
    respond_with(client, "write_register", FakeResponse(error=2, function_code=3))
    result = run(cli.write_register(1, 5, make_ctx(client, writes_enabled=True)))
    assert result["meta"]["outcome"] == "unknown" and result["meta"]["request_sent"] is True
    assert result["error"].endswith("the write may have been applied; check before retrying")
    assert len(client.sent_writes) == 1


@pytest.mark.parametrize(("count", "registers", "ok"), [(2, 2, True), (2, 1, False), (2, 3, False)])
def test_register_counts_must_match_exactly(count, registers, ok) -> None:
    check = cli.reply_check(3, registers=count)
    problem = check(FakeResponse(registers=[0] * registers, function_code=3))
    assert (problem is None) is ok, problem


def test_a_reply_with_too_many_registers_is_not_a_read(client) -> None:
    """Too many is as wrong as too few: the reply belongs to another request."""
    respond_with(client, "read_holding_registers", FakeResponse(registers=[1, 2, 3]))
    result = run(cli.read_multiple_holding_registers(0, 2, make_ctx(client, max_retries=0)))
    assert result["success"] is False
    assert result["error"] == "the device's reply does not match the request"
    assert result["meta"]["reply_mismatch"]["registers"] == {"expected": 2, "got": 3}


@pytest.mark.parametrize(
    ("count", "bits", "ok"),
    [(1, 8, True), (8, 8, True), (9, 16, True), (9, 8, False), (2, 16, False)],
)
def test_bit_counts_are_checked_in_whole_bytes(count, bits, ok) -> None:
    check = cli.reply_check(1, bits=count)
    problem = check(FakeResponse(bits=[False] * bits, function_code=1))
    assert (problem is None) is ok, problem


def coil_frame(address: int, value: int) -> bytes:
    mbap = struct.pack(">HHHB", 1, 0, 6, 1)
    return mbap + struct.pack(">BHH", 5, address, value)


@pytest.mark.parametrize(
    ("raw_value", "outcome"), [(0xFF00, "written"), (0xFF01, "unknown"), (0x0001, "unknown")]
)
def test_a_coil_echo_is_checked_byte_for_byte(client, raw_value, outcome) -> None:
    """pymodbus decodes any non-zero FC 5 value as ON; the raw reply must say 0xFF00."""
    ctx = make_ctx(client, writes_enabled=True)
    ctx.request_context.lifespan_context.last_reply = lambda: coil_frame(3, raw_value)
    result = run(cli.write_coil(3, True, ctx))
    assert result["meta"]["outcome"] == outcome, result
    if outcome == "unknown":
        assert "raw_reply" in result["meta"]["reply_mismatch"]


def test_the_raw_check_accepts_an_rtu_frame_with_its_crc(client) -> None:
    pdu = struct.pack(">BHH", 5, 3, 0x0000)
    check = cli.reply_check(5, raw=(pdu, lambda: bytes([1]) + pdu + b"\x12\x34"))
    assert check(FakeResponse(function_code=5)) is None
