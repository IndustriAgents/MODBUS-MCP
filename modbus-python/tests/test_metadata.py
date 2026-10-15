"""Package metadata and what the server says about itself in `initialize`."""

from __future__ import annotations

import os
import subprocess
import sys
from importlib.metadata import metadata, version
from pathlib import Path

try:
    import tomllib
except ModuleNotFoundError:  # Python 3.10
    import tomli as tomllib

import modbus_mcp
from modbus_mcp import cli

PYPROJECT = Path(__file__).resolve().parents[1] / "pyproject.toml"


def test_description_has_no_stray_characters() -> None:
    description = tomllib.loads(PYPROJECT.read_text())["project"]["description"]
    assert description.endswith("industrial IoT systems.")
    assert metadata("industriagents-modbus-mcp")["Summary"] == description


def test_server_info_reports_the_package_version_not_the_sdk_version() -> None:
    options = cli.mcp._mcp_server.create_initialization_options()
    assert options.server_name == "Modbus MCP Server"
    assert options.server_version == version("industriagents-modbus-mcp") == modbus_mcp.__version__
    assert options.server_version != version("mcp")


def test_python_dash_m_runs_main() -> None:
    proc = subprocess.run(
        [sys.executable, "-m", "modbus_mcp"],
        env={**os.environ, "MODBUS_TYPE": "bogus"},
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert proc.returncode == 2 and "MODBUS_TYPE" in proc.stderr and proc.stdout == ""
