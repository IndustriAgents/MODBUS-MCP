"""Configuration is read at startup, after .env is loaded, and validated."""

from __future__ import annotations

import json
import os
import subprocess
import sys

import pytest

from modbus_mcp import cli
from modbus_mcp.config import ConfigError, Settings, load_tag_map, parse_tag_map

MODBUS_VARS = [
    "MODBUS_TYPE",
    "MODBUS_HOST",
    "MODBUS_PORT",
    "MODBUS_SERIAL_PORT",
    "MODBUS_BAUDRATE",
    "MODBUS_PARITY",
    "MODBUS_STOPBITS",
    "MODBUS_BYTESIZE",
    "MODBUS_TIMEOUT",
    "MODBUS_DEFAULT_SLAVE_ID",
    "MODBUS_MAX_RETRIES",
    "MODBUS_RETRY_BACKOFF_BASE",
    "MODBUS_TOOL_TIMEOUT",
    "MODBUS_WRITES_ENABLED",
    "REGISTER_MAP_FILE",
]


def clean_env(**overrides: str) -> dict[str, str]:
    env = {k: v for k, v in os.environ.items() if k not in MODBUS_VARS}
    env.update(overrides)
    return env


def test_defaults() -> None:
    s = Settings.from_env({})
    assert s.modbus_type == "tcp"
    assert (s.host, s.port) == ("127.0.0.1", 502)
    assert s.default_slave_id == 1
    assert (s.max_retries, s.retry_backoff_base, s.tool_timeout) == (2, 0.2, None)
    assert s.register_map_file is None


def test_writes_are_disabled_by_default() -> None:
    assert Settings.from_env({}).writes_enabled is False
    assert Settings().writes_enabled is False


@pytest.mark.parametrize(
    ("raw", "expected"),
    [("true", True), ("1", True), ("YES", True), ("on", True), ("false", False), ("0", False)],
)
def test_writes_enabled_values(raw: str, expected: bool) -> None:
    assert Settings.from_env({"MODBUS_WRITES_ENABLED": raw}).writes_enabled is expected


def test_parsing() -> None:
    s = Settings.from_env(
        {
            "MODBUS_TYPE": "SERIAL",
            "MODBUS_SERIAL_PORT": "COM3",
            "MODBUS_BAUDRATE": "19200",
            "MODBUS_PARITY": "e",
            "MODBUS_STOPBITS": "2",
            "MODBUS_BYTESIZE": "7",
            "MODBUS_TIMEOUT": "0.5",
            "MODBUS_DEFAULT_SLAVE_ID": "17",
            "MODBUS_MAX_RETRIES": "0",
            "MODBUS_RETRY_BACKOFF_BASE": "0",
            "MODBUS_TOOL_TIMEOUT": "2.5",
            "REGISTER_MAP_FILE": "tags.json",
        }
    )
    assert s.modbus_type == "serial" and s.serial_port == "COM3" and s.target == "COM3"
    assert (s.baudrate, s.parity, s.stopbits, s.bytesize) == (19200, "E", 2, 7)
    assert (s.serial_timeout, s.default_slave_id, s.max_retries) == (0.5, 17, 0)
    assert (s.retry_backoff_base, s.tool_timeout) == (0.0, 2.5)
    assert s.register_map_file == "tags.json"


def test_empty_values_mean_default_and_zero_timeout_means_none() -> None:
    s = Settings.from_env({"MODBUS_PORT": "", "MODBUS_TOOL_TIMEOUT": "0", "MODBUS_HOST": " "})
    assert (s.port, s.tool_timeout, s.host) == (502, None, "127.0.0.1")


@pytest.mark.parametrize(
    ("name", "value", "message"),
    [
        ("MODBUS_TYPE", "rtu", "MODBUS_TYPE must be one of tcp, udp, serial"),
        ("MODBUS_PORT", "abc", "MODBUS_PORT must be an integer"),
        ("MODBUS_PORT", "70000", "MODBUS_PORT must be between 1 and 65535"),
        ("MODBUS_DEFAULT_SLAVE_ID", "256", "between 0 and 255"),
        ("MODBUS_PARITY", "X", "MODBUS_PARITY must be one of N, E, O"),
        ("MODBUS_TIMEOUT", "0", "MODBUS_TIMEOUT must be a finite number"),
        ("MODBUS_TOOL_TIMEOUT", "-1", "MODBUS_TOOL_TIMEOUT"),
        ("MODBUS_RETRY_BACKOFF_BASE", "nan", "finite"),
        ("MODBUS_WRITES_ENABLED", "maybe", "MODBUS_WRITES_ENABLED must be true or false"),
    ],
)
def test_invalid_values_raise(name: str, value: str, message: str) -> None:
    with pytest.raises(ConfigError, match=message):
        Settings.from_env({name: value})


def test_load_config_reads_settings_after_load_dotenv(monkeypatch: pytest.MonkeyPatch) -> None:
    for var in MODBUS_VARS:
        monkeypatch.delenv(var, raising=False)
    calls: list[str] = []

    def fake_load_dotenv() -> bool:
        # What python-dotenv does with a .env file: put its values into os.environ.
        calls.append("load_dotenv")
        os.environ["MODBUS_PORT"] = "1502"
        os.environ["MODBUS_WRITES_ENABLED"] = "true"
        return True

    monkeypatch.setattr(cli, "load_dotenv", fake_load_dotenv)
    try:
        settings, tags = cli.load_config()
    finally:
        os.environ.pop("MODBUS_PORT", None)
        os.environ.pop("MODBUS_WRITES_ENABLED", None)
    assert calls == ["load_dotenv"]
    assert settings.port == 1502
    assert settings.writes_enabled is True
    assert tags == {}


def test_importing_the_server_does_not_read_the_environment() -> None:
    env = clean_env(MODBUS_PORT="not-a-port", MODBUS_TYPE="bogus", REGISTER_MAP_FILE="/nope.json")
    proc = subprocess.run(
        [
            sys.executable,
            "-c",
            "import modbus_mcp.cli as c; print(len(c.mcp._tool_manager.list_tools()))",
        ],
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.strip() == "17"


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        ({"MODBUS_TYPE": "bogus"}, "MODBUS_TYPE must be one of"),
        ({"REGISTER_MAP_FILE": "/definitely/not/here.json"}, "does not exist"),
    ],
)
def test_main_rejects_bad_configuration_on_stderr_only(overrides: dict, message: str) -> None:
    proc = subprocess.run(
        [sys.executable, "-c", "from modbus_mcp.cli import main; main()"],
        env=clean_env(**overrides),
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert proc.returncode == 2
    assert proc.stdout == ""  # stdout belongs to JSON-RPC
    assert "configuration error" in proc.stderr and message in proc.stderr


# --- tag map -----------------------------------------------------------------


def test_tag_map_parsing_normalises_and_fills_defaults() -> None:
    tags = parse_tag_map(
        {
            "Speed": {"table": "HR", "address": 2, "dtype": "float32", "byteorder": "LITTLE"},
            "Pump": {"table": "coils", "address": 0},
            "Level": {"table": "input", "address": 3, "dtype": "uint16", "scale": 0.1, "unit": "%"},
        }
    )
    assert tags["Speed"].table == "holding" and tags["Speed"].byteorder == "little"
    assert tags["Speed"].span == 2
    assert tags["Pump"].table == "coil" and tags["Pump"].writable
    assert not tags["Level"].writable and tags["Level"].scale == 0.1
    assert tags["Level"].as_dict()["unit"] == "%"


@pytest.mark.parametrize(
    ("spec", "message"),
    [
        ({"table": "holding"}, "address is required"),
        ({"table": "magic", "address": 0}, "unknown table"),
        ({"address": -1}, "between 0 and 65535"),
        ({"address": "5"}, "must be an integer"),
        ({"address": 0, "dtype": "float16"}, "Unsupported dtype"),
        ({"address": 0, "dtype": "int32", "wordorder": "middle"}, "wordorder"),
        ({"table": "coil", "address": 0, "dtype": "uint16"}, "only apply to holding/input"),
        ({"address": 0, "scale": 2}, "scale and offset need a dtype"),
        ({"address": 0, "dtype": "int16", "scale": 0}, "must not be 0"),
        ({"address": 65535, "dtype": "float32"}, "runs past 65535"),
        ({"address": 0, "adress": 1}, "unknown key"),
        ({"address": 0, "slave_id": 300}, "between 0 and 255"),
        ("holding", "expected an object"),
    ],
)
def test_invalid_tags_are_rejected(spec: object, message: str) -> None:
    with pytest.raises(ConfigError, match=message):
        parse_tag_map({"T": spec})


def test_load_tag_map_errors(tmp_path) -> None:
    assert load_tag_map(None) == {}
    with pytest.raises(ConfigError, match="does not exist"):
        load_tag_map(str(tmp_path / "missing.json"))
    bad = tmp_path / "bad.json"
    bad.write_text("{not json")
    with pytest.raises(ConfigError, match="not valid JSON"):
        load_tag_map(str(bad))
    top = tmp_path / "list.json"
    top.write_text("[]")
    with pytest.raises(ConfigError, match="must be a JSON object"):
        load_tag_map(str(top))
    good = tmp_path / "good.json"
    good.write_text(json.dumps({"Valve": {"table": "holding", "address": 0}}))
    assert load_tag_map(str(good))["Valve"].address == 0
