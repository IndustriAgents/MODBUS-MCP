"""Backend support and native gates are separate from device authorization."""

import json
from pathlib import Path

from jsonschema import Draft202012Validator

from modbus_mcp.domain.capabilities import capability_report


def test_report_matches_advertised_status_schema_and_all_canonical_operations():
    contract = json.loads((Path(__file__).resolve().parents[2] / "contract/tools.json").read_text())
    status = next(t for t in contract["tools"] if t["name"] == "get_connection_status")
    for enabled in (False, True):
        capabilities = capability_report(writes_enabled=enabled, system_commands_enabled=enabled)
        Draft202012Validator(status["outputSchema"]).validate(
            {
                "success": True,
                "data": {"capabilities": capabilities},
                "error": None,
                "meta": {},
            }
        )
        assert set(capabilities["operations"]) == {t["name"] for t in contract["tools"]}
        assert capabilities["protocol"] == contract["protocol"]
        assert capabilities["device_verified"] is False
        assert "MCP policy" in capabilities["authorization"]
        for tool in contract["tools"]:
            operation = capabilities["operations"][tool["name"]]
            assert operation["supported"] is True
            if not tool["annotations"].get("readOnlyHint", False):
                assert operation["enabled"] is enabled


def test_capability_reports_do_not_share_mutable_state():
    first = capability_report(writes_enabled=False)
    first["operations"].clear()
    assert capability_report(writes_enabled=False)["operations"]


def test_local_status_does_not_probe_device(client, ctx):
    import asyncio

    from modbus_mcp import cli
    from modbus_mcp.application.diagnostics import get_connection_status

    result = asyncio.run(get_connection_status(cli.context_for(ctx)))
    assert result["success"] is True
    assert result["data"]["capabilities"]["backend"] == "pymodbus"
    assert client.calls == []
