"""Bad arguments and failures come back as {success: false, ...} envelopes, never raw errors."""

from __future__ import annotations

import asyncio
import json

import pytest
from conftest import FakeResponse, make_ctx, run
from mcp.server.fastmcp.exceptions import ToolError

from modbus_mcp import cli


def call(name: str, arguments: dict) -> dict:
    """Call a tool the way the MCP server does and return the envelope it would send."""
    result = asyncio.run(cli.mcp.call_tool(name, arguments))
    structured = None
    if isinstance(result, tuple):  # mcp >= 1.10: (content, structured content)
        result, structured = result
    (content,) = result
    envelope = json.loads(content.text)
    if structured is not None:
        assert structured == envelope
    return envelope


@pytest.mark.parametrize(
    ("tool", "arguments", "fragment"),
    [
        ("read_register", {"address": -1}, "address: Input should be greater than or equal to 0"),
        (
            "read_register",
            {"address": 65536},
            "address: Input should be less than or equal to 65535",
        ),
        ("read_register", {"address": "abc"}, "address: Input should be a valid integer"),
        ("read_register", {"address": True}, "address: Input should be a valid integer"),
        ("read_register", {"address": "5"}, "address: Input should be a valid integer"),
        ("read_register", {"address": 5.0}, "address: Input should be a valid integer"),
        ("read_register", {"address": 0, "slave_id": True}, "slave_id"),
        ("write_register", {"address": 0, "value": True}, "value: Input should be a valid integer"),
        ("write_register", {"address": 0, "value": "7"}, "value: Input should be a valid integer"),
        ("write_coil", {"address": 0, "value": 1}, "value: Input should be a valid boolean"),
        ("write_coil", {"address": 0, "value": "true"}, "value: Input should be a valid boolean"),
        ("write_coils_bulk", {"address": 0, "values": [1, 0]}, "values.0"),
        ("write_registers", {"address": 0, "values": [1, True]}, "values.1"),
        ("mask_write_register", {"address": 0, "and_mask": 1.0, "or_mask": 0}, "and_mask"),
        ("read_coils", {"address": 0, "count": "2"}, "count"),
        ("ping", {"address": True}, "address"),
        ("read_device_information", {"object_id": False}, "object_id"),
        ("read_register", {}, "address: Field required"),
        ("read_register", {"address": 0, "slave_id": 256}, "slave_id"),
        (
            "read_coils",
            {"address": 0, "count": 0},
            "count: Input should be greater than or equal to 1",
        ),
        (
            "write_register",
            {"address": 0, "value": 70000},
            "value: Input should be less than or equal to 65535",
        ),
        ("write_register", {"address": 0, "value": -1}, "value"),
        ("write_coil", {"address": 0, "value": "maybe"}, "value: Input should be a valid boolean"),
        ("write_registers", {"address": 0, "values": []}, "values"),
        ("write_registers", {"address": 0, "values": [0] * 124}, "values"),
        ("write_registers", {"address": 0, "values": [1, 70000]}, "values.1"),
        ("write_coils_bulk", {"address": 0, "values": []}, "values"),
        ("mask_write_register", {"address": 0, "and_mask": 1 << 16, "or_mask": 0}, "and_mask"),
        ("read_holding_typed", {"address": 0, "dtype": "float16"}, "dtype"),
        ("read_input_typed", {"address": 0, "dtype": "int32", "byteorder": "middle"}, "byteorder"),
        ("read_input_typed", {"address": 0, "dtype": "int32", "count": 0}, "count"),
        ("read_holding_typed", {"address": 0, "dtype": "int32", "scale": "nan"}, "scale"),
        ("read_device_information", {"read_code": 9}, "read_code"),
        ("read_holding_typed", {"address": 0, "dtype": "int32", "scale": True}, "scale"),
        ("read_input_typed", {"address": 0, "dtype": "int32", "offset": "0.5"}, "offset"),
    ],
)
def test_invalid_arguments_return_an_envelope(tool: str, arguments: dict, fragment: str) -> None:
    envelope = call(tool, arguments)
    assert envelope["success"] is False
    assert envelope["data"] is None
    assert envelope["error"].startswith("Invalid arguments: ")
    assert fragment in envelope["error"]
    expected_meta = {"tool": tool}
    if tool in cli.WRITE_TOOLS:  # nothing was sent
        expected_meta.update(outcome="not_sent", request_sent=False)
    assert envelope["meta"] == expected_meta


def test_an_exception_inside_a_tool_also_returns_an_envelope() -> None:
    # Called outside an MCP session there is no lifespan context, so the tool body raises.
    envelope = call("ping", {})
    assert envelope["success"] is False
    assert envelope["error"].startswith("Unexpected ")


def test_unknown_tools_are_still_protocol_errors() -> None:
    with pytest.raises(ToolError, match="Unknown tool"):
        asyncio.run(cli.mcp.call_tool("no_such_tool", {}))


def test_every_tool_has_a_description_and_documented_parameters() -> None:
    tools = asyncio.run(cli.mcp.list_tools())
    assert len(tools) == 17
    for tool in tools:
        assert tool.description and tool.description.strip(), tool.name
        props = tool.inputSchema.get("properties", {})
        assert "ctx" not in props, tool.name
        for pname, schema in props.items():
            assert schema.get("description"), (tool.name, pname)
    by_name = {t.name: t for t in tools}
    slave = by_name["read_register"].inputSchema["properties"]["slave_id"]
    assert {"type": "null"} in slave["anyOf"]
    address = by_name["read_register"].inputSchema["properties"]["address"]
    assert (address["minimum"], address["maximum"]) == (0, 65535)


def test_prompt_is_listed() -> None:
    prompts = asyncio.run(cli.mcp.list_prompts())
    assert [p.name for p in prompts] == ["analyze_register"]


# --- failures inside the tools -----------------------------------------------------------


def test_device_exceptions_are_named(client, ctx) -> None:
    result = run(cli.read_input_registers(98, 5, ctx))  # the fake device has 100 registers
    assert result["success"] is False
    assert result["error"] == "Modbus exception 2 (Illegal Data Address) for function 0x04"
    assert result["meta"]["exception_code"] == 2


def test_single_register_device_exception(client, ctx) -> None:
    result = run(cli.read_register(150, ctx))
    assert result["error"] == "Modbus exception 2 (Illegal Data Address) for function 0x03"


def test_reads_past_the_address_space_are_refused_before_sending(client, ctx) -> None:
    result = run(cli.read_multiple_holding_registers(65535, 2, ctx))
    assert result["success"] is False and "runs past" in result["error"]
    assert client.calls == []


def test_short_response_is_not_reported_as_success(client, ctx) -> None:
    async def short(address, *, count=1, device_id=1):
        return FakeResponse(registers=[1], function_code=4)

    client.read_input_registers = short
    result = run(cli.read_input_registers(0, 4, ctx))
    assert result["success"] is False
    assert result["error"] == "the device's reply does not match the request"
    assert result["meta"]["reply_mismatch"] == {"registers": {"expected": 4, "got": 1}}


def test_chunked_reads_split_at_protocol_limits(client, ctx) -> None:
    client.SIZE = 300
    client.ir = list(range(300))
    result = run(cli.read_input_registers(0, 300, ctx))
    assert result["success"], result
    assert result["data"]["registers"] == list(range(300))
    assert [c["count"] for c in result["meta"]["chunks"]] == [125, 125, 50]


def test_device_information_is_decoded_to_text(client, ctx) -> None:
    result = run(cli.read_device_information(ctx))
    assert result["success"]
    assert result["data"] == {"0": "Fake Vendor", "1": "FAKE-1", "2": "1.0"}


TAGS = {
    "Pump": {"table": "coil", "address": 0},
    "Lamps": {"table": "coil", "address": 1, "count": 3},
    "Level": {"table": "input", "address": 3},
    "Pair": {"table": "holding", "address": 10, "count": 2},
}


@pytest.mark.parametrize(
    ("tag", "value", "fragment"),
    [
        ("Pump", "false", "takes true or false"),
        ("Pump", 2, "takes true or false"),
        ("Pump", 1, "takes true or false"),
        ("Lamps", True, "write_coils_bulk"),
        ("Level", 5, "read-only"),
        ("Pair", 5, "holds 2 value(s); got 1"),
        ("Pair", [1, 2, 3], "holds 2 value(s); got 3"),
        ("Pair", [1, 1.5], "whole number"),
        ("Nope", 1, "Unknown tag"),
    ],
)
def test_write_tag_rejects_values_it_cannot_write_exactly(client, tag, value, fragment) -> None:
    ctx = make_ctx(client, TAGS, writes_enabled=True)
    result = run(cli.write_tag(tag, value, ctx))
    assert result["success"] is False
    assert fragment in result["error"]
    assert result["meta"]["outcome"] == "not_sent" and result["meta"]["request_sent"] is False
    assert client.calls == []


def test_schemas_use_standard_json_schema_keywords() -> None:
    def walk(node):
        if isinstance(node, dict):
            assert not {"ge", "le", "gt", "lt"} & set(node), node
            for value in node.values():
                walk(value)
        elif isinstance(node, list):
            for value in node:
                walk(value)

    for tool in asyncio.run(cli.mcp.list_tools()):
        walk(tool.inputSchema)


def check_annotations(tools) -> None:
    """Write tools are destructive and never idempotent; every other tool is read-only."""
    names = {tool.name for tool in tools}
    assert cli.WRITE_TOOLS <= names
    for tool in tools:
        hints = tool.annotations
        assert hints is not None, tool.name
        if tool.name in cli.WRITE_TOOLS:
            assert hints.readOnlyHint is False, tool.name
            assert hints.destructiveHint is True, tool.name
            assert hints.idempotentHint is False, tool.name
        else:
            assert hints.readOnlyHint is True, tool.name


def test_tool_annotations_do_not_invite_retries() -> None:
    check_annotations(asyncio.run(cli.mcp.list_tools()))


def test_an_unexpected_error_before_anything_was_sent_is_not_sent(client, monkeypatch) -> None:
    tags = {"Speed": {"table": "holding", "address": 10, "dtype": "float32"}}
    app = make_ctx(client, tags, writes_enabled=True).request_context.lifespan_context
    monkeypatch.setattr(cli, "_app", lambda ctx: app)

    def explode(*args, **kwargs):
        raise RuntimeError("encoder exploded")

    monkeypatch.setattr(cli.codec, "encode", explode)
    envelope = call("write_tag", {"name": "Speed", "value": 1.5})
    assert envelope["success"] is False
    assert envelope["error"] == "Unexpected RuntimeError: encoder exploded"
    assert envelope["meta"]["outcome"] == "not_sent" and envelope["meta"]["request_sent"] is False
    assert client.calls == []
