import argparse
import os
import re
import sys
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

import anyio
import anyio.lowlevel
from dotenv import dotenv_values
from mcp.server.fastmcp import Context, FastMCP

from . import __version__
from .adapters.transport import _RX_KEEP as _RX_KEEP
from .adapters.transport import NotConnected as NotConnected
from .adapters.transport import RequestNotSent as RequestNotSent
from .adapters.transport import SendGate, _make_client
from .adapters.transport import _desync as _desync
from .adapters.transport import _frames_now as _frames_now
from .adapters.transport import _reconnect as _reconnect
from .adapters.transport import _retry_call as _retry_call
from .adapters.transport import _sent_since as _sent_since
from .adapters.transport import _unit as _unit
from .adapters.transport import _unit_kwarg_for as _unit_kwarg_for
from .adapters.transport import _with_timeout as _with_timeout
from .application.diagnostics import list_tags as list_tags
from .application.diagnostics import ping as ping
from .application.diagnostics import read_device_information as read_device_information
from .application.execution import Execution
from .application.read import _chunked_read as _chunked_read
from .application.read import _read_typed as _read_typed
from .application.read import read_coils as read_coils
from .application.read import read_discrete_inputs as read_discrete_inputs
from .application.read import read_holding_typed as read_holding_typed
from .application.read import read_input_registers as read_input_registers
from .application.read import read_input_typed as read_input_typed
from .application.read import read_multiple_holding_registers as read_multiple_holding_registers
from .application.read import read_register as read_register
from .application.read import read_tag as read_tag
from .application.request import _request as _request
from .application.write import mask_write_register as mask_write_register
from .application.write import write_coil as write_coil
from .application.write import write_coils_bulk as write_coils_bulk
from .application.write import write_register as write_register
from .application.write import write_registers as write_registers
from .application.write import write_tag as write_tag
from .config import ConfigError, Settings, Tag, load_tag_map
from .domain.model import _EXCEPTION_NAMES as _EXCEPTION_NAMES
from .domain.model import ADDRESS_SPACE as ADDRESS_SPACE
from .domain.model import MAX_READ_BITS as MAX_READ_BITS
from .domain.model import MAX_READ_REGISTERS as MAX_READ_REGISTERS
from .domain.model import MAX_WRITE_COILS as MAX_WRITE_COILS
from .domain.model import MAX_WRITE_REGISTERS as MAX_WRITE_REGISTERS
from .domain.model import MAY_HAVE_BEEN_APPLIED as MAY_HAVE_BEEN_APPLIED
from .domain.model import READ_REPLY_MISMATCH as READ_REPLY_MISMATCH
from .domain.model import REFUSED as REFUSED
from .domain.model import STARTUP_CONNECT_TIMEOUT, log
from .domain.model import WRITE_NO_REPLY as WRITE_NO_REPLY
from .domain.model import WRITE_REPLY_MISMATCH as WRITE_REPLY_MISMATCH
from .domain.model import WRITE_TOOLS as WRITE_TOOLS
from .domain.model import WRITES_DISABLED as WRITES_DISABLED
from .domain.model import Address as Address
from .domain.model import BitCount as BitCount
from .domain.model import ByteOrder as ByteOrder
from .domain.model import DType as DType
from .domain.model import Mask as Mask
from .domain.model import Offset as Offset
from .domain.model import RegisterCount as RegisterCount
from .domain.model import RegisterValue as RegisterValue
from .domain.model import Scale as Scale
from .domain.model import SlaveId as SlaveId
from .domain.model import StrictBool as StrictBool
from .domain.model import TypedCount as TypedCount
from .domain.model import WordOrder as WordOrder
from .domain.model import _apply_scale as _apply_scale
from .domain.model import _approx as _approx
from .domain.model import _decimal as _decimal
from .domain.model import _device_error as _device_error
from .domain.model import _echo_mismatch as _echo_mismatch
from .domain.model import _int as _int
from .domain.model import _is_error as _is_error
from .domain.model import _json_number as _json_number
from .domain.model import _make_result as _make_result
from .domain.model import _not_sent as _not_sent
from .domain.model import _slave as _slave
from .domain.model import _span_error as _span_error
from .domain.model import _unknown_tag as _unknown_tag
from .domain.model import _unscale as _unscale
from .domain.model import reply_check as reply_check
from .domain.policy import Policy
from .infrastructure.control_context import _CALL as _CALL
from .infrastructure.control_context import DEADLINE_BACKSTOP as DEADLINE_BACKSTOP
from .infrastructure.control_context import _abandoned as _abandoned
from .infrastructure.control_context import _CallState as _CallState
from .infrastructure.control_context import _deadline_error as _deadline_error
from .infrastructure.control_context import _limit as _limit
from .infrastructure.control_context import _remaining as _remaining
from .infrastructure.control_context import _why_not_send as _why_not_send
from .infrastructure.control_context import run_with_deadline as run_with_deadline
from .protocol.hints import DEVICE_WRITE as DEVICE_WRITE
from .protocol.hints import READ_ONLY as READ_ONLY
from .protocol.server import _UNDELIVERABLE as _UNDELIVERABLE
from .protocol.server import ModbusMCP
from .protocol.server import _describe_validation_error as _describe_validation_error
from .protocol.tools import register_tools
from .state import AppContext, OperationContext

_config: tuple[Settings, dict[str, Tag]] | None = None
GATE_VARIABLES = ("MODBUS_WRITES_ENABLED",)
OWN_EXTRAS = ("REGISTER_MAP_FILE",)
PACKAGE_DIR = Path(__file__).resolve().parent
_EXPORT_PREFIX = re.compile("^\\s*export\\s+", re.IGNORECASE)
_PROJECT_NAME = re.compile('^name\\s*=\\s*"(?:industriagents-)?modbus-mcp"\\s*$', re.MULTILINE)


def implicit_dotenv(package_dir: Path = PACKAGE_DIR) -> Path | None:
    """The one .env file read automatically: modbus-python/.env of a source checkout.

    It is found relative to the package source (src/modbus_mcp -> ../../.env) and
    only when that directory is this project (its pyproject.toml names
    industriagents-modbus-mcp, or its legacy modbus-mcp name). Never the working
    directory or a parent directory: the working
    directory is wherever the MCP client launched the server, which may be an
    untrusted project, and settings such as the host or the tag map decide where
    a write goes. An installed package (site-packages) has no implicit .env; use
    the environment or --env-file.
    """
    if package_dir.parent.name != "src":
        return None
    project = package_dir.parent.parent
    try:
        if not _PROJECT_NAME.search((project / "pyproject.toml").read_text(encoding="utf-8")):
            return None
    except OSError:
        return None
    candidate = project / ".env"
    return candidate if candidate.is_file() else None


def _env_key(key: str) -> str:
    """Normalise a .env key: no `export` prefix or quotes, stripped, upper case.

    Windows treats environment variable names case-insensitively (and upper-cases
    them), so modbus_writes_enabled in a .env must count as MODBUS_WRITES_ENABLED.
    """
    return _EXPORT_PREFIX.sub("", key).strip().strip("'\"").strip().upper()


def _read_env_file(path: str | Path) -> dict[str, str]:
    """A .env file's settings with normalised keys (python-dotenv parses quotes and export)."""
    return {_env_key(k): v for k, v in dotenv_values(path).items() if v is not None}


def _gates_in(path: str | Path) -> set[str]:
    """Gate variables a .env file tries to set, including on lines python-dotenv cannot parse."""
    gates = set()
    with open(path, encoding="utf-8", errors="replace") as fh:
        for line in fh:
            key, sep, _ = line.partition("=")
            if sep and _env_key(key) in GATE_VARIABLES:
                gates.add(_env_key(key))
    return gates


def _warn(message: str) -> None:
    sys.stderr.write(f"modbus-mcp: warning: {message}\n")


def read_environment(env_file: str | None = None) -> dict[str, str]:
    """The variables the settings are read from, without changing os.environ.

    Highest precedence first: the process environment; `env_file` (--env-file),
    honoured in full; the implicit .env (implicit_dotenv()), which may only set
    this server's own non-gate settings (MODBUS_* except GATE_VARIABLES, and
    OWN_EXTRAS). Anything else in it, such as MODBUS_WRITES_ENABLED, PATH or
    PYTHONPATH, is ignored with a warning on stderr naming it. Keys from both
    files are compared case-insensitively (see _env_key), as Windows does.
    """
    env = dict(os.environ)
    if env_file is not None:
        if not os.path.isfile(env_file):
            raise ConfigError(f"--env-file {env_file!r} does not exist")
        for key, value in _read_env_file(env_file).items():
            env.setdefault(key, value)
    found = implicit_dotenv()
    if found is None or (env_file and os.path.realpath(found) == os.path.realpath(env_file)):
        return env
    values = _read_env_file(found)
    for gate in sorted((set(values) | _gates_in(found)) & set(GATE_VARIABLES)):
        _warn(
            "ignoring "
            f"{gate}"
            " from "
            f"{found}"
            ": it is only read from the process environment "
            "or a file given with --env-file"
        )
    for key, value in values.items():
        if key in GATE_VARIABLES:
            continue
        if not (key.startswith("MODBUS_") or key in OWN_EXTRAS):
            _warn(
                "ignoring "
                f"{key}"
                " from "
                f"{found}"
                ": the automatically loaded .env may only set "
                "modbus-mcp's own settings (MODBUS_*, "
                f"{', '.join(OWN_EXTRAS)}"
                ")"
            )
            continue
        env.setdefault(key, value)
    return env


def load_config(env_file: str | None = None) -> tuple[Settings, dict[str, Tag]]:
    """Read the settings (see read_environment) and the tag map."""
    settings = Settings.from_env(read_environment(env_file))
    return (settings, load_tag_map(settings.register_map_file))


def _app(ctx: Context) -> AppContext:
    return ctx.request_context.lifespan_context


@asynccontextmanager
async def app_lifespan(server: FastMCP) -> AsyncIterator[AppContext]:
    """Create the Modbus client and try to connect; an unreachable device is not fatal.

    The server starts either way, so initialize and tools/list work. Until the
    device answers, the device tools return success: false "cannot reach device
    at ..."; every call tries to connect again (see _retry_call/_reconnect).
    """
    settings, tags = server.configuration or (_config if _config is not None else load_config())
    gate = SendGate(lambda: getattr(server, "client_gone", False))
    client = _make_client(settings, gate)
    gate.client = client
    try:
        with anyio.move_on_after(STARTUP_CONNECT_TIMEOUT):
            await client.connect()
    except Exception as exc:
        log.warning("Connecting to %s failed: %s: %s", settings.target, type(exc).__name__, exc)
    if getattr(client, "connected", False):
        log.info("Connected to Modbus %s device at %s", settings.modbus_type, settings.target)
    else:
        log.warning(
            "Cannot reach Modbus %s device at %s yet; tool calls will keep trying",
            settings.modbus_type,
            settings.target,
        )
    try:
        yield AppContext(
            modbus_client=client,
            settings=settings,
            tags=tags,
            frames_sent=lambda: gate.sent,
            last_reply=lambda: bytes(gate.rx),
            closing=lambda: getattr(server, "client_gone", False),
        )
    finally:
        client.close()


def main(argv: list[str] | None = None) -> None:
    """Entry point of `modbus-mcp`: read the configuration, then serve MCP over stdio."""
    parser = argparse.ArgumentParser(
        prog="modbus-mcp",
        description=(
            "MCP server for Modbus TCP/UDP/serial devices, "
            "over stdio. Configured with MODBUS_* environment"
            " variables."
        ),
    )
    parser.add_argument(
        "--env-file",
        metavar="PATH",
        help=(
            "read settings from this file (after the process "
            "environment). Unlike a .env found automatically,"
            " it may set MODBUS_WRITES_ENABLED."
        ),
    )
    parser.add_argument("--version", action="version", version=f"modbus-mcp {__version__}")
    args = parser.parse_args(argv)
    global _config
    try:
        environment = read_environment(args.env_file)
        settings = Settings.from_env(environment)
        _config = (settings, load_tag_map(settings.register_map_file))
        # Resolve policy alongside connection settings, after --env-file. The
        # compatibility module instance must not capture import-time settings.
        mcp.contract_boundary.execution = Execution.from_environment(
            "modbus", environment=environment
        )
    except (ValueError, OSError) as exc:
        sys.stderr.write(f"modbus-mcp: configuration error: {exc}\n")
        raise SystemExit(2) from None
    mcp.run()


def context_for(ctx):
    return OperationContext(_app(ctx), ctx.info, ctx.error)


def _make_server(configuration=None, policy=None):
    server = ModbusMCP(
        name="Modbus MCP Server",
        instructions=(
            "Tools for one Modbus device. Every tool returns "
            "{success, data, error, meta}; check success "
            "before using data. Addresses are zero-based "
            "(holding register 40001 is address 0). Write "
            "tools only work when the server was started with"
            " MODBUS_WRITES_ENABLED=true. If the device "
            "cannot be reached, tools return success=false "
            "with 'cannot reach device' and try again on the "
            "next call."
        ),
        dependencies=["pymodbus"],
        lifespan=app_lifespan,
    )
    server.configuration = configuration
    server._mcp_server.version = __version__
    register_tools(server, context_for, policy)
    return server


def create_server(configuration=None, policy=None):
    """Build an independent instance with a configuration snapshot."""
    return _make_server(configuration if configuration is not None else load_config(), policy)


mcp = _make_server(policy=Policy())


def _legacy_context_call(fn):
    import functools
    import inspect

    signature = inspect.signature(fn)

    @functools.wraps(fn)
    async def adapted(*args, **kwargs):
        bound = signature.bind(*args, **kwargs)
        ctx = bound.arguments.get("ctx")
        if ctx is not None and (not isinstance(ctx, OperationContext)):
            bound.arguments["ctx"] = context_for(ctx)
        return await fn(*bound.args, **bound.kwargs)

    return adapted


for _legacy_name in (
    "list_tags",
    "ping",
    "read_device_information",
    "_chunked_read",
    "_read_typed",
    "read_coils",
    "read_discrete_inputs",
    "read_holding_typed",
    "read_input_registers",
    "read_input_typed",
    "read_multiple_holding_registers",
    "read_register",
    "read_tag",
    "mask_write_register",
    "write_coil",
    "write_coils_bulk",
    "write_register",
    "write_registers",
    "write_tag",
    "_request",
):
    globals()[_legacy_name] = _legacy_context_call(globals()[_legacy_name])
