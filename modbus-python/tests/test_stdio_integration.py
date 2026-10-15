"""End to end: the real `modbus-mcp` server over stdio, driven by the MCP client SDK,
talking Modbus TCP to the mock device."""

from __future__ import annotations

import contextlib
import json
import os
import subprocess
import sys
import time
from importlib.metadata import version
from pathlib import Path
from types import SimpleNamespace

import anyio
import pytest
from conftest import free_port, running_mock, stop_process
from mcp import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client
from test_validation import check_annotations

from modbus_mcp.cli import WRITES_DISABLED

pytestmark = [pytest.mark.integration, pytest.mark.anyio]

SERVER_VARS = ("MODBUS_", "REGISTER_MAP_FILE")


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.fixture(scope="module")
def mock(tmp_path_factory):
    with running_mock(tmp_path_factory.mktemp("stdio-mock")) as device:
        yield device


def server_env(mock, **overrides: str) -> dict[str, str]:
    """This environment minus any MODBUS_* settings, pointed at the mock."""
    env = {k: v for k, v in os.environ.items() if not k.startswith(SERVER_VARS)}
    env.update(MODBUS_HOST=mock.host, MODBUS_PORT=str(mock.port), MODBUS_RETRY_BACKOFF_BASE="0")
    env.update(overrides)
    return env


class Server:
    def __init__(self, session: ClientSession, init, logs: list, stderr: Path) -> None:
        self.session, self.init, self.logs, self.stderr = session, init, logs, stderr

    async def call(self, tool: str, **arguments) -> dict:
        result = await self.session.call_tool(tool, arguments)
        assert result.isError is (json.loads(result.content[0].text).get("success") is False), (
            result
        )  # failures are envelopes, not MCP errors
        (content,) = result.content
        envelope = json.loads(content.text)
        if getattr(result, "structuredContent", None) is not None:
            assert result.structuredContent == envelope
        assert set(envelope) == {"success", "data", "error", "meta"}
        return envelope


@contextlib.asynccontextmanager
async def server(mock, tmp_path: Path, extra_args: tuple[str, ...] = (), **env: str):
    params = StdioServerParameters(
        command=sys.executable,
        args=["-m", "modbus_mcp", *extra_args],
        env=server_env(mock, **env),
    )
    logs: list = []

    async def on_log(params) -> None:
        logs.append((params.level, params.data))

    stderr_path = tmp_path / "server-stderr.log"
    with open(stderr_path, "w") as errlog:
        async with stdio_client(params, errlog=errlog) as (read, write):
            async with ClientSession(read, write, logging_callback=on_log) as session:
                with anyio.fail_after(60):
                    init = await session.initialize()
                yield Server(session, init, logs, stderr_path)


async def test_initialize_reports_this_server(mock, tmp_path) -> None:
    async with server(mock, tmp_path) as srv:
        info = srv.init.serverInfo
        assert info.name == "Modbus MCP Server"
        assert info.version == version("industriagents-modbus-mcp")
        assert srv.init.capabilities.tools is not None
        tools = (await srv.session.list_tools()).tools
        assert len(tools) == 6
        assert all(t.description and t.description.strip() for t in tools)
        check_annotations(tools)  # as the client receives them
        prompts = (await srv.session.list_prompts()).prompts
        assert [p.name for p in prompts] == ["analyze_register"]


async def test_reads_against_the_mock(mock, tmp_path) -> None:
    async with server(mock, tmp_path) as srv:
        assert (await srv.call("read_register", address=0))["data"] == {"value": 50}
        holding = await srv.call("read_multiple_holding_registers", address=0, count=10)
        assert holding["data"]["registers"][:6] == [50, 0, 0, 0, 0, 0]
        inputs = await srv.call("read_input_registers", address=0, count=9)
        assert inputs["success"] and len(inputs["data"]["registers"]) == 9
        assert 200 <= inputs["data"]["registers"][0] <= 800  # temperature x10
        coils = await srv.call("read_coils", address=0, count=2)
        assert coils["success"] and len(coils["data"]["values"]) == 2
        discrete = await srv.call("read_discrete_inputs", address=0, count=2)
        assert discrete["success"] and len(discrete["data"]["values"]) == 2
        ident = await srv.call("read_device_information")
        assert ident["data"]["0"] == "Mock ICS" and ident["data"]["1"] == "MODBUS-MOCK"
        typed = await srv.call("read_input_typed", address=0, dtype="uint16", scale=0.1)
        assert 20.0 <= typed["data"]["values"][0] <= 80.0
        ping = await srv.call("ping")
        assert ping["success"] and ping["data"]["device_reachable"] is True
        assert ping["data"]["writes_enabled"] is False
        past = await srv.call("read_register", address=100)
        assert past["success"] is False
        assert past["error"].startswith("Modbus exception 2 (Illegal Data Address)")
        # ctx.info()/ctx.error() are awaited, so the client receives log notifications.
        assert any(level == "info" and "Read register 0" in str(data) for level, data in srv.logs)
    assert "never awaited" not in srv.stderr.read_text()


async def test_writes_are_refused_by_default(mock, tmp_path) -> None:
    async with server(mock, tmp_path) as srv:
        before = await srv.call("read_register", address=20)
        refused = await srv.call("write_register", address=20, value=1234)
        assert refused == {
            "success": False,
            "data": None,
            "error": WRITES_DISABLED,
            "meta": {
                "address": 20,
                "value": 1234,
                "slave_id": 1,
                "outcome": "not_sent",
                "request_sent": False,
            },
        }
        after = await srv.call("read_register", address=20)
        assert after["data"] == before["data"]


async def test_an_env_file_given_on_the_command_line_can_enable_writes(mock, tmp_path) -> None:
    env_file = tmp_path / "plant.env"
    env_file.write_text("MODBUS_WRITES_ENABLED=true\n")
    async with server(mock, tmp_path, extra_args=("--env-file", str(env_file))) as srv:
        assert (await srv.call("ping"))["data"]["writes_enabled"] is True
        written = await srv.call("write_register", address=25, value=77)
        assert written["success"] and written["meta"]["outcome"] == "written"


async def test_bad_arguments_come_back_as_envelopes(mock, tmp_path) -> None:
    async with server(mock, tmp_path) as srv:
        bad = await srv.call("read_register", address=-1)
        assert bad["success"] is False and bad["error"].startswith("Invalid arguments: address")
        bad = await srv.call("read_holding_typed", address=0, dtype="float16")
        assert bad["success"] is False and "dtype" in bad["error"]


async def test_writes_and_typed_values_when_enabled(mock, tmp_path) -> None:
    tags = tmp_path / "tags.json"
    tags.write_text(
        json.dumps(
            {
                "Setpoint": {"table": "holding", "address": 30, "dtype": "float32"},
                "Counter": {
                    "table": "holding",
                    "address": 32,
                    "dtype": "int32",
                    "byteorder": "little",
                    "wordorder": "little",
                },
                "Scaled": {"table": "holding", "address": 34, "dtype": "int16", "scale": 0.1},
                "Lamp": {"table": "coil", "address": 10},
            }
        )
    )
    async with server(
        mock, tmp_path, MODBUS_WRITES_ENABLED="true", REGISTER_MAP_FILE=str(tags)
    ) as srv:
        written = await srv.call("write_register", address=20, value=1234)
        assert written["success"]
        assert written["meta"]["outcome"] == "written" and written["meta"]["request_sent"]
        assert (await srv.call("read_register", address=20))["data"] == {"value": 1234}
        assert (await srv.call("write_registers", address=21, values=[1, 2, 3]))["success"]
        regs = await srv.call("read_multiple_holding_registers", address=20, count=4)
        assert regs["data"]["registers"] == [1234, 1, 2, 3]
        assert (await srv.call("mask_write_register", address=20, and_mask=0xFF00, or_mask=0x0F))[
            "success"
        ]
        assert (await srv.call("read_register", address=20))["data"] == {
            "value": (1234 & 0xFF00) | 0x0F
        }
        assert (await srv.call("write_coil", address=10, value=True))["success"]
        assert (await srv.call("write_coils_bulk", address=11, values=[True, False, True]))[
            "success"
        ]
        coils = await srv.call("read_coils", address=10, count=4)
        assert coils["data"]["values"] == [True, True, False, True]

        # Typed values, the bug that made every one of these fail with "Big"/"Little"
        assert (await srv.call("write_tag", name="Setpoint", value=42.5))["success"]
        assert (await srv.call("read_tag", name="Setpoint"))["data"]["values"] == [42.5]
        typed = await srv.call(
            "read_holding_typed", address=30, dtype="float32", byteorder="big", wordorder="big"
        )
        assert typed["data"]["values"] == [42.5]
        assert (await srv.call("write_tag", name="Counter", value=-123456))["success"]
        assert (await srv.call("read_tag", name="Counter"))["data"]["values"] == [-123456]
        swapped = await srv.call(
            "read_holding_typed", address=32, dtype="int32", byteorder="little", wordorder="little"
        )
        assert swapped["data"]["values"] == [-123456]
        assert (await srv.call("write_tag", name="Scaled", value=-12.3))["success"]
        assert (await srv.call("read_register", address=34))["data"] == {"value": 65536 - 123}
        assert (await srv.call("read_tag", name="Scaled"))["data"]["values"] == [
            pytest.approx(-12.3)
        ]
        assert (await srv.call("write_tag", name="Lamp", value=False))["success"]
        assert (await srv.call("read_tag", name="Lamp"))["data"]["values"] == [False]
        listed = await srv.call("list_tags")
        assert [t["name"] for t in listed["data"]["tags"]] == [
            "Counter",
            "Lamp",
            "Scaled",
            "Setpoint",
        ]

        refused = await srv.call("write_register", address=100, value=1)
        assert refused["success"] is False and "Illegal Data Address" in refused["error"]


async def test_server_reconnects_after_the_device_restarts(tmp_path) -> None:
    with running_mock(tmp_path) as first:
        async with server(first, tmp_path, MODBUS_MAX_RETRIES="1") as srv:
            assert (await srv.call("read_register", address=0))["success"]

            stop_process(first.process)
            down = await srv.call("read_register", address=0)
            assert down["success"] is False
            ping = await srv.call("ping")
            assert ping["success"] is False and ping["data"]["device_reachable"] is False

            # The device comes back on the same port; the server reconnects by itself.
            with running_mock(tmp_path, port=first.port):
                back = await srv.call("read_register", address=0)
                assert back["success"], back
                assert back["data"] == {"value": 50}
                assert any("reconnected" in str(data) for _, data in srv.logs)


async def test_initialize_does_not_wait_for_an_unreachable_host(tmp_path) -> None:
    """192.0.2.1 (TEST-NET-1) never answers: pymodbus would wait 3 s to connect."""
    target = SimpleNamespace(host="192.0.2.1", port=502)
    started = time.monotonic()
    async with server(target, tmp_path) as srv:
        elapsed = time.monotonic() - started
        assert len((await srv.session.list_tools()).tools) == 6
    assert elapsed < 2.5, elapsed


async def test_starts_without_a_device_and_connects_when_it_appears(tmp_path) -> None:
    port = free_port()  # nothing listens here yet
    target = SimpleNamespace(host="127.0.0.1", port=port)
    async with server(target, tmp_path, MODBUS_WRITES_ENABLED="true") as srv:
        assert srv.init.serverInfo.name == "Modbus MCP Server"
        assert len((await srv.session.list_tools()).tools) == 6

        unreachable = f"cannot reach device at 127.0.0.1:{port}"
        down = await srv.call("read_register", address=0)
        assert down["success"] is False and down["error"] == unreachable
        write = await srv.call("write_register", address=1, value=5)
        assert write["success"] is False and write["error"] == unreachable  # nothing was sent
        ping = await srv.call("ping")
        assert ping["success"] is False and ping["error"] == unreachable
        assert ping["data"]["connected"] is False and ping["data"]["device_reachable"] is False

        with running_mock(tmp_path, port=port):
            up = await srv.call("read_register", address=0)
            assert up["success"], up
            assert up["data"] == {"value": 50}
            assert (await srv.call("ping"))["data"]["device_reachable"] is True
    assert "Cannot reach Modbus tcp device" in srv.stderr.read_text()


# --- stdout carries JSON-RPC and nothing else -------------------------------------------

# One call per tool, with arguments that work against the mock (writes go to
# unused addresses 60-63). The tests check this covers every tool in tools/list.
EVERY_TOOL = {
    "read_register": {"address": 0},
    "write_register": {"address": 60, "value": 9},
    "read_coils": {"address": 0, "count": 2},
    "write_coil": {"address": 60, "value": True},
    "read_input_registers": {"address": 0, "count": 4},
    "read_multiple_holding_registers": {"address": 0, "count": 3},
    "read_discrete_inputs": {"address": 0, "count": 2},
    "write_registers": {"address": 61, "values": [1, 2]},
    "write_coils_bulk": {"address": 61, "values": [True, False]},
    "mask_write_register": {"address": 62, "and_mask": 65532, "or_mask": 3},
    "read_device_information": {},
    "read_holding_typed": {"address": 63, "dtype": "float32"},
    "read_input_typed": {"address": 0, "dtype": "uint16", "scale": 0.1},
    "list_tags": {},
    "read_tag": {"name": "Temperature"},
    "write_tag": {"name": "Setpoint", "value": 1.5},
    "ping": {},
}
RAW_TAGS = {
    "Temperature": {"table": "input", "address": 0, "dtype": "uint16", "scale": 0.1},
    "Setpoint": {"table": "holding", "address": 70, "dtype": "float32"},
}


def raw_session(env: dict[str, str]) -> tuple[dict[int, dict], list[str]]:
    """Speak JSON-RPC to `python -m modbus_mcp` by hand: initialize, list the tools and
    prompts, call every tool once (plus one invalid call), get the prompt.

    The server runs with PYTHONUNBUFFERED=1: with a block-buffered stdout, a stray
    print() would sit in the buffer and be lost when the process is stopped, and the
    test could not see it. Returns the responses by id and every stdout line.
    """
    env = {**env, "PYTHONUNBUFFERED": "1"}
    proc = subprocess.Popen(
        [sys.executable, "-m", "modbus_mcp"],
        env=env,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    lines: list[str] = []
    responses: dict[int, dict] = {}
    next_id = iter(range(1, 1000))

    def request(method: str, params: dict | None = None) -> dict:
        message: dict = {"jsonrpc": "2.0", "id": next(next_id), "method": method}
        if params is not None:
            message["params"] = params
        proc.stdin.write(json.dumps(message) + "\n")
        proc.stdin.flush()
        while message["id"] not in responses:  # log notifications may come first
            line = proc.stdout.readline()
            assert line, proc.stderr.read()
            lines.append(line)
            reply = json.loads(line)  # a stray print() fails here
            assert reply["jsonrpc"] == "2.0", reply
            if "id" in reply:
                responses[reply["id"]] = reply
        return responses[message["id"]]

    try:
        init = request(
            "initialize",
            {
                "protocolVersion": "2024-11-05",
                "capabilities": {},
                "clientInfo": {"name": "pytest", "version": "0"},
            },
        )
        assert init["result"]["serverInfo"]["name"] == "Modbus MCP Server"
        proc.stdin.write(json.dumps({"jsonrpc": "2.0", "method": "notifications/initialized"}))
        proc.stdin.write("\n")
        proc.stdin.flush()
        listed = {t["name"] for t in request("tools/list")["result"]["tools"]}
        assert listed == {
            "ping",
            "mask_write_register",
            "read_values",
            "write_values",
            "read_device_information",
            "list_tags",
        }, "EVERY_TOOL must call every tool"
        for name, arguments in EVERY_TOOL.items():
            request("tools/call", {"name": name, "arguments": arguments})
        request("tools/call", {"name": "read_register", "arguments": {"address": "x"}})
        assert [p["name"] for p in request("prompts/list")["result"]["prompts"]] == [
            "analyze_register"
        ]
        request("prompts/get", {"name": "analyze_register", "arguments": {"value": "42"}})
        # MCP shutdown: close stdin, wait, then SIGTERM (mcp 1.6's stdio server does
        # not exit on EOF by itself; 1.10+ does).
        proc.stdin.close()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.terminate()
            proc.wait(timeout=10)
        lines.extend(proc.stdout.read().splitlines(keepends=True))
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait()
    for line in lines:
        assert json.loads(line)["jsonrpc"] == "2.0", line
    return responses, lines


def envelopes(responses: dict[int, dict]) -> dict[str, dict]:
    """The envelope of each tools/call, by tool name (ids 3.. follow EVERY_TOOL order)."""
    names = list(EVERY_TOOL) + ["invalid read_register"]
    return {
        name: json.loads(responses[3 + i]["result"]["content"][0]["text"])
        for i, name in enumerate(names)
    }


def test_stdout_carries_only_json_rpc(mock, tmp_path) -> None:
    tags = tmp_path / "tags.json"
    tags.write_text(json.dumps(RAW_TAGS))
    env = server_env(mock, MODBUS_WRITES_ENABLED="true", REGISTER_MAP_FILE=str(tags))
    responses, lines = raw_session(env)
    assert len(lines) >= len(EVERY_TOOL) + 5
    assert responses[1]["result"]["serverInfo"]["version"] == version("industriagents-modbus-mcp")
    results = envelopes(responses)
    failed = {k: v["error"] for k, v in results.items() if not v["success"]}
    assert set(failed) == {"invalid read_register"}, failed
    assert results["read_register"]["data"] == {"value": 50}
    assert "Invalid arguments" in failed["invalid read_register"]


def test_stdout_is_clean_without_a_device(tmp_path) -> None:
    port = free_port()  # nothing listens here
    env = {k: v for k, v in os.environ.items() if not k.startswith(SERVER_VARS)}
    env.update(MODBUS_HOST="127.0.0.1", MODBUS_PORT=str(port), MODBUS_RETRY_BACKOFF_BASE="0")
    tags = tmp_path / "tags.json"
    tags.write_text(json.dumps(RAW_TAGS))
    env.update(MODBUS_WRITES_ENABLED="true", REGISTER_MAP_FILE=str(tags))
    responses, _ = raw_session(env)
    results = envelopes(responses)
    unreachable = f"cannot reach device at 127.0.0.1:{port}"
    assert results["list_tags"]["success"] is True  # needs no device
    for name in set(EVERY_TOOL) - {"list_tags"}:
        assert results[name]["success"] is False, name
        assert results[name]["error"] == unreachable, (name, results[name])
