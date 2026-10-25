"""Application modules depend on injected invocation, not a native SDK/adapter."""

import ast
import asyncio
from pathlib import Path

from conftest import make_ctx

from modbus_mcp import cli
from modbus_mcp.application import write


def test_application_has_no_native_adapter_or_sdk_imports():
    directory = Path(__file__).resolve().parents[1] / "src/modbus_mcp/application"
    for path in directory.glob("*.py"):
        for node in ast.walk(ast.parse(path.read_text())):
            if isinstance(node, ast.ImportFrom):
                assert "adapters" not in (node.module or "").split("."), path
                assert not (node.module or "").startswith(("pymodbus", "mcp")), path


def test_write_uses_injected_transport_and_does_not_log_payload(client):
    ctx = make_ctx(client, writes_enabled=True)
    operation_ctx = cli.context_for(ctx)
    native = operation_ctx.transport
    calls = []

    class Transport:
        def unit(self, driver, unit):
            calls.append(("unit", unit))
            return native.unit(driver, unit)

        async def request(self, *args, **kwargs):
            calls.append(("request", kwargs["write"]))
            return await native.request(*args, **kwargs)

    operation_ctx.transport = Transport()
    result = asyncio.run(write.write_register(2, 42137, operation_ctx))
    assert result["success"] is True
    assert calls == [("request", True), ("unit", 1)]
    assert len(client.sent_writes) == 1
    assert "42137" not in str(ctx.logs)


def test_reply_mismatch_logs_fields_without_raw_register_payload(client):
    from conftest import FakeResponse

    async def wrong_echo(**kwargs):
        return FakeResponse(function_code=6, address=2, registers=[42138])

    client.write_register = wrong_echo
    ctx = make_ctx(client, writes_enabled=True, max_retries=0)
    result = asyncio.run(cli.write_register(2, 42137, ctx))
    assert result["meta"]["outcome"] == "unknown"
    assert "registers" in str(ctx.logs)
    assert "42137" not in str(ctx.logs) and "42138" not in str(ctx.logs)
