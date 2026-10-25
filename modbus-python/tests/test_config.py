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
    "MODBUS_POLICY_FILE",
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


@pytest.fixture
def no_modbus_env(monkeypatch: pytest.MonkeyPatch) -> pytest.MonkeyPatch:
    for var in MODBUS_VARS:
        monkeypatch.delenv(var, raising=False)
    return monkeypatch


def found_dotenv(monkeypatch: pytest.MonkeyPatch, path) -> None:
    """Make `path` the .env the server loads automatically (implicit_dotenv())."""
    monkeypatch.setattr(cli, "implicit_dotenv", lambda *a, **k: path)


def test_an_automatically_found_env_sets_connection_settings(no_modbus_env, tmp_path) -> None:
    dotenv = tmp_path / ".env"
    dotenv.write_text("MODBUS_HOST=10.0.0.5\nMODBUS_PORT=1502\n")
    found_dotenv(no_modbus_env, dotenv)
    settings, tags = cli.load_config()
    assert (settings.host, settings.port) == ("10.0.0.5", 1502)
    assert tags == {}
    assert "MODBUS_PORT" not in os.environ  # read, not exported


def test_an_automatically_found_env_cannot_enable_writes(no_modbus_env, tmp_path, capsys) -> None:
    dotenv = tmp_path / ".env"
    dotenv.write_text("MODBUS_PORT=1502\nMODBUS_WRITES_ENABLED=true\n")
    found_dotenv(no_modbus_env, dotenv)
    settings, _ = cli.load_config()
    assert settings.writes_enabled is False
    assert settings.port == 1502  # the rest of the file still applies
    err = capsys.readouterr().err
    assert "ignoring MODBUS_WRITES_ENABLED" in err and str(dotenv) in err


@pytest.mark.parametrize(
    "line",
    [
        "modbus_writes_enabled=true",
        "Modbus_Writes_Enabled=yes",
        "export MODBUS_WRITES_ENABLED=on",
        "export   modbus_writes_enabled = 'true'",
        'MODBUS_WRITES_ENABLED="true"',
        "  MODBUS_WRITES_ENABLED = 1 ",
        "EXPORT MODBUS_WRITES_ENABLED=true",  # python-dotenv cannot parse this one
    ],
)
def test_the_gate_filter_ignores_case_export_and_quotes(no_modbus_env, tmp_path, capsys, line):
    # Windows upper-cases environment names, so a lower-case key in a .env would
    # be MODBUS_WRITES_ENABLED there; the filter must catch every spelling.
    dotenv = tmp_path / ".env"
    dotenv.write_text(f"{line}\nexport modbus_port=1502\n")
    found_dotenv(no_modbus_env, dotenv)
    settings, _ = cli.load_config()
    assert settings.writes_enabled is False
    assert settings.port == 1502  # keys are normalised, so connection settings still apply
    assert "ignoring MODBUS_WRITES_ENABLED" in capsys.readouterr().err


def test_an_explicit_env_file_with_a_lower_case_key_can_enable_writes(no_modbus_env, tmp_path):
    no_modbus_env.setattr(cli, "implicit_dotenv", lambda *a, **k: None)
    env_file = tmp_path / "plant.env"
    env_file.write_text("export modbus_writes_enabled='true'\n")
    assert cli.load_config(str(env_file))[0].writes_enabled is True


FOREIGN = ["PATH", "LD_PRELOAD", "DYLD_INSERT_LIBRARIES", "PYTHONPATH", "PYTHONSTARTUP", "FOO"]


def test_the_implicit_env_sets_only_this_servers_settings(no_modbus_env, tmp_path, capsys):
    for key in FOREIGN[1:]:
        no_modbus_env.delenv(key, raising=False)
    tags = tmp_path / "tags.json"
    tags.write_text("{}")
    dotenv = tmp_path / ".env"
    dotenv.write_text(
        "".join(f"{key}=/from/dotenv\n" for key in FOREIGN)
        + f"MODBUS_PORT=1502\nregister_map_file={tags}\n"
    )
    found_dotenv(no_modbus_env, dotenv)
    env = cli.read_environment()
    assert env["MODBUS_PORT"] == "1502" and env["REGISTER_MAP_FILE"] == str(tags)
    assert env["PATH"] == os.environ["PATH"]
    for key in FOREIGN[1:]:
        assert key not in env, key
    err = capsys.readouterr().err
    for key in FOREIGN:
        assert f"ignoring {key} from {dotenv}" in err, key


def test_an_explicit_env_file_is_honoured_in_full(no_modbus_env, tmp_path, capsys):
    no_modbus_env.setattr(cli, "implicit_dotenv", lambda *a, **k: None)
    no_modbus_env.delenv("FOO", raising=False)
    env_file = tmp_path / "plant.env"
    env_file.write_text("FOO=bar\nMODBUS_PORT=1502\n")
    env = cli.read_environment(str(env_file))
    assert env["FOO"] == "bar" and env["MODBUS_PORT"] == "1502"
    assert capsys.readouterr().err == ""


def _checkout(root, *, name="modbus-mcp", installed=False):
    """A fake package layout under `root`; returns the package directory."""
    if installed:
        package = root / "site-packages" / "modbus_mcp"
        package.mkdir(parents=True)
        return package
    project = root / "modbus-python"
    package = project / "src" / "modbus_mcp"
    package.mkdir(parents=True)
    (project / "pyproject.toml").write_text(f'[project]\nname = "{name}"\nversion = "0"\n')
    return package


@pytest.mark.parametrize("name", ["modbus-mcp", "industriagents-modbus-mcp"])
def test_the_implicit_env_is_only_the_projects_own(tmp_path, name) -> None:
    package = _checkout(tmp_path, name=name)
    (tmp_path / ".env").write_text("MODBUS_PORT=1\n")  # a parent of the project
    assert cli.implicit_dotenv(package) is None
    own = tmp_path / "modbus-python" / ".env"
    own.write_text("MODBUS_PORT=2\n")
    assert cli.implicit_dotenv(package) == own


def test_no_implicit_env_for_an_installed_package_or_another_project(tmp_path) -> None:
    installed = _checkout(tmp_path / "a", installed=True)
    (installed.parent / ".env").write_text("MODBUS_PORT=1\n")
    (installed.parent.parent / ".env").write_text("MODBUS_PORT=1\n")
    assert cli.implicit_dotenv(installed) is None
    other = _checkout(tmp_path / "b", name="someone-else")
    (tmp_path / "b" / "modbus-python" / ".env").write_text("MODBUS_PORT=1\n")
    assert cli.implicit_dotenv(other) is None
    # Installed (not under src/), even next to a pyproject.toml that names us.
    lookalike = tmp_path / "c" / "lib" / "site-packages" / "modbus_mcp"
    lookalike.mkdir(parents=True)
    (lookalike.parent.parent / "pyproject.toml").write_text(
        '[project]\nname = "industriagents-modbus-mcp"\n'
    )
    (lookalike.parent.parent / ".env").write_text("MODBUS_PORT=1\n")
    assert cli.implicit_dotenv(lookalike) is None


@pytest.mark.parametrize("where", ["cwd", "parent of cwd"])
def test_a_env_in_the_working_directory_or_a_parent_is_not_read(tmp_path, where) -> None:
    # The real lookup, run from a directory that has a .env (or whose parent has one).
    root = tmp_path / "untrusted-project"
    cwd = root if where == "cwd" else root / "sub"
    cwd.mkdir(parents=True)
    (root / ".env").write_text(
        "MODBUS_HOST=from-untrusted-dotenv\nMODBUS_WRITES_ENABLED=true\nREGISTER_MAP_FILE=/x\n"
    )
    code = (
        "from modbus_mcp import cli; env = cli.read_environment(); "
        "print(env.get('MODBUS_HOST'), env.get('MODBUS_WRITES_ENABLED'), "
        "env.get('REGISTER_MAP_FILE'), cli.implicit_dotenv())"
    )
    proc = subprocess.run(
        [sys.executable, "-c", code],
        cwd=cwd,
        env=clean_env(),
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert proc.returncode == 0, proc.stderr
    assert "from-untrusted-dotenv" not in proc.stdout and "True" not in proc.stdout
    host, writes, tag_map, found = proc.stdout.split()
    assert host != "from-untrusted-dotenv" and tag_map != "/x" and writes == "None"
    assert found == "None" or not found.startswith(str(root)), found
    assert "untrusted-project" not in proc.stderr


def test_an_explicit_env_file_can_enable_writes(no_modbus_env, tmp_path, capsys) -> None:
    no_modbus_env.setattr(cli, "implicit_dotenv", lambda *a, **k: None)  # no .env
    env_file = tmp_path / "plant.env"
    env_file.write_text("MODBUS_WRITES_ENABLED=true\nMODBUS_PORT=1503\n")
    settings, _ = cli.load_config(str(env_file))
    assert settings.writes_enabled is True and settings.port == 1503
    assert capsys.readouterr().err == ""


def test_the_process_environment_can_enable_writes(no_modbus_env, tmp_path) -> None:
    dotenv = tmp_path / ".env"
    dotenv.write_text("MODBUS_WRITES_ENABLED=false\n")
    found_dotenv(no_modbus_env, dotenv)
    no_modbus_env.setenv("MODBUS_WRITES_ENABLED", "true")
    assert cli.load_config()[0].writes_enabled is True


def test_precedence_environment_then_env_file_then_found_env(no_modbus_env, tmp_path) -> None:
    dotenv = tmp_path / ".env"
    dotenv.write_text("MODBUS_HOST=found\nMODBUS_PORT=1111\nMODBUS_DEFAULT_SLAVE_ID=3\n")
    found_dotenv(no_modbus_env, dotenv)
    env_file = tmp_path / "explicit.env"
    env_file.write_text("MODBUS_PORT=2222\nMODBUS_DEFAULT_SLAVE_ID=4\n")
    no_modbus_env.setenv("MODBUS_DEFAULT_SLAVE_ID", "5")
    settings, _ = cli.load_config(str(env_file))
    assert (settings.host, settings.port, settings.default_slave_id) == ("found", 2222, 5)


def test_a_missing_env_file_is_a_configuration_error(no_modbus_env, tmp_path) -> None:
    with pytest.raises(ConfigError, match="--env-file"):
        cli.load_config(str(tmp_path / "missing.env"))


def test_main_reads_the_env_file_argument(tmp_path) -> None:
    env_file = tmp_path / "bad.env"
    env_file.write_text("MODBUS_TYPE=bogus\n")
    proc = subprocess.run(
        [sys.executable, "-m", "modbus_mcp", "--env-file", str(env_file)],
        env=clean_env(),
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert proc.returncode == 2
    assert "MODBUS_TYPE must be one of" in proc.stderr and proc.stdout == ""


def test_importing_the_server_does_not_read_the_environment() -> None:
    env = clean_env(
        MODBUS_PORT="not-a-port",
        MODBUS_TYPE="bogus",
        REGISTER_MAP_FILE="/nope.json",
        MODBUS_POLICY_FILE="/missing-policy.json",
    )
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
    assert proc.stdout.strip() == "19"


def test_main_resolves_policy_from_explicit_env_file(no_modbus_env, tmp_path):
    policy = tmp_path / "policy.json"
    policy.write_text('{"read_only": true, "max_in_flight": 2}')
    environment = tmp_path / "settings.env"
    environment.write_text(f"MODBUS_POLICY_FILE={policy}\n")
    observed = []
    no_modbus_env.setattr(cli, "_config", None)
    no_modbus_env.setattr(
        cli.mcp.contract_boundary, "execution", cli.mcp.contract_boundary.execution
    )
    no_modbus_env.setattr(
        cli.mcp, "run", lambda: observed.append(cli.mcp.contract_boundary.execution.policy)
    )
    cli.main(["--env-file", str(environment)])
    assert observed[0].read_only is True
    assert observed[0].max_in_flight == 2


def test_main_rejects_invalid_policy_on_stderr_only(tmp_path):
    policy = tmp_path / "policy.json"
    policy.write_text('{"read_only": "false"}')
    proc = subprocess.run(
        [sys.executable, "-m", "modbus_mcp"],
        env=clean_env(MODBUS_POLICY_FILE=str(policy)),
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert proc.returncode == 2
    assert proc.stdout == ""
    assert "configuration error" in proc.stderr and "read_only must be a boolean" in proc.stderr


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
