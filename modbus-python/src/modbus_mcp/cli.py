"""Modbus MCP server: Modbus TCP/UDP/serial devices as MCP tools over stdio.

stdout carries the MCP JSON-RPC stream, so nothing in this module may print;
diagnostics go to stderr (logging) or to the client as MCP log messages.
"""

# No `from __future__ import annotations` here: FastMCP 1.6 finds the Context
# parameter by comparing the annotation object itself (`is Context`).
import argparse
import asyncio
import functools
import inspect
import json
import logging
import math
import os
import sys
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import Annotated, Any, Literal

from dotenv import dotenv_values, find_dotenv
from mcp.server.fastmcp import Context, FastMCP
from mcp.server.fastmcp.exceptions import ToolError
from mcp.server.fastmcp.prompts import base
from mcp.types import TextContent
from pydantic import BeforeValidator, Field, ValidationError
from pymodbus.client import AsyncModbusSerialClient, AsyncModbusTcpClient, AsyncModbusUdpClient
from pymodbus.exceptions import ConnectionException, ModbusException

from . import __version__, codec
from .config import ConfigError, Settings, Tag, load_tag_map

log = logging.getLogger("modbus_mcp")  # stderr, via FastMCP's logging setup

WRITES_DISABLED = (
    "Writes are disabled by configuration (set MODBUS_WRITES_ENABLED=true to allow them)"
)
# A write request went out but no reply came back. It is never sent again.
WRITE_NO_REPLY = "no reply; the write may have been applied; check before retrying"
WRITE_TOOLS = frozenset(
    {
        "write_register",
        "write_registers",
        "write_coil",
        "write_coils_bulk",
        "mask_write_register",
        "write_tag",
    }
)
# meta.outcome of a write: "written" (the device acknowledged it), "rejected"
# (the device answered with a Modbus exception), "not_sent" (refused before
# anything was sent), "unknown" (sent, no reply: it may have been applied).

# Modbus protocol limits per request (Modbus Application Protocol v1.1b3).
MAX_READ_REGISTERS = 125
MAX_READ_BITS = 2000
MAX_WRITE_REGISTERS = 123
MAX_WRITE_COILS = 1968
ADDRESS_SPACE = 65536

_EXCEPTION_NAMES = {
    1: "Illegal Function",
    2: "Illegal Data Address",
    3: "Illegal Data Value",
    4: "Server Device Failure",
    5: "Acknowledge",
    6: "Server Device Busy",
    8: "Memory Parity Error",
    10: "Gateway Path Unavailable",
    11: "Gateway Target Device Failed to Respond",
}


# -----------------------------
# Argument types (they become the tools' JSON Schema)
# -----------------------------


def _reject_bool(value: Any) -> Any:
    # pydantic's lax mode turns true/false into 1/0 for an int; for a register
    # address or value that would quietly read or write the wrong thing.
    if isinstance(value, bool):
        raise ValueError("expected an integer, not a boolean")
    return value


def _int(lo: int, hi: int, description: str) -> Any:
    # Field before the validator, so the bounds stay standard JSON Schema (minimum/maximum).
    return Annotated[
        int, Field(ge=lo, le=hi, description=description), BeforeValidator(_reject_bool)
    ]


Address = _int(0, 65535, "Zero-based Modbus address (0-65535).")
RegisterValue = _int(0, 65535, "Unsigned 16-bit register value (0-65535).")
Mask = _int(0, 65535, "16-bit mask (0-65535).")
RegisterCount = _int(1, ADDRESS_SPACE, "Number of registers to read (at least 1).")
BitCount = _int(1, ADDRESS_SPACE, "Number of bits to read (at least 1).")
TypedCount = _int(1, ADDRESS_SPACE, "Number of typed values to decode (at least 1).")
SlaveId = Annotated[
    Annotated[int, Field(ge=0, le=255), BeforeValidator(_reject_bool)] | None,
    Field(description="Modbus unit/slave ID (0-255); omit to use MODBUS_DEFAULT_SLAVE_ID."),
]
DType = Annotated[
    Literal["int16", "uint16", "int32", "uint32", "int64", "uint64", "float32", "float64"],
    Field(description="Value type; 16-bit types use 1 register, 32-bit 2, 64-bit 4."),
]
ByteOrder = Annotated[
    Literal["big", "little"],
    Field(description="Byte order inside each register ('big' is the Modbus default)."),
]
WordOrder = Annotated[
    Literal["big", "little"],
    Field(description="Register order for values wider than 16 bits ('big' = high word first)."),
]
Scale = Annotated[
    float, Field(allow_inf_nan=False, description="Multiply each decoded value by this.")
]
Offset = Annotated[
    float, Field(allow_inf_nan=False, description="Then add this to each decoded value.")
]


@dataclass
class AppContext:
    """Per-server state, created by the lifespan once the device is connected."""

    modbus_client: Any
    settings: Settings = field(default_factory=Settings)
    tags: dict[str, Tag] = field(default_factory=dict)
    # Tool calls can run concurrently; only one of them should reconnect.
    reconnect_lock: asyncio.Lock = field(default_factory=asyncio.Lock)


# -----------------------------
# Configuration
# -----------------------------

# Set by main(); the lifespan falls back to load_config() when the server is
# started some other way (e.g. `mcp dev`).
_config: tuple[Settings, dict[str, Tag]] | None = None


# Settings that let the server change the device. They are honoured only from
# the real process environment or a file passed explicitly with --env-file,
# never from a .env file found automatically: when an MCP client launches the
# server, that search can reach the client's own project, which may not be
# trusted, and a .env there must not be able to switch writes on.
GATE_VARIABLES = ("MODBUS_WRITES_ENABLED",)


def load_config(env_file: str | None = None) -> tuple[Settings, dict[str, Tag]]:
    """Read the settings and the tag map, without changing os.environ.

    Sources, highest precedence first: the process environment, then `env_file`
    (--env-file), then a `.env` found automatically by searching up from this
    package's directory (python-dotenv's default search, as before). The
    automatically found file may set connection settings but no GATE_VARIABLES:
    those are ignored, with a warning on stderr naming them.
    """
    env = dict(os.environ)
    if env_file is not None:
        if not os.path.isfile(env_file):
            raise ConfigError(f"--env-file {env_file!r} does not exist")
        for key, value in dotenv_values(env_file).items():
            if value is not None:
                env.setdefault(key, value)
    found = find_dotenv()
    if found and not (env_file and os.path.realpath(found) == os.path.realpath(env_file)):
        for key, value in dotenv_values(found).items():
            if value is None:
                continue
            if key in GATE_VARIABLES:
                sys.stderr.write(
                    f"modbus-mcp: warning: ignoring {key} from {found}: it is only read from"
                    " the process environment or a file given with --env-file\n"
                )
                continue
            env.setdefault(key, value)
    settings = Settings.from_env(env)
    return settings, load_tag_map(settings.register_map_file)


# -----------------------------
# Helpers
# -----------------------------


def _make_result(
    success: bool,
    data: Any = None,
    error: str | None = None,
    meta: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "success": success,
        "data": data,
        "error": error,
        "meta": meta or {},
    }


def _not_sent(error: str, meta: dict[str, Any]) -> dict[str, Any]:
    """A write refused before anything was sent to the device."""
    return _make_result(
        False, error=error, meta={**meta, "outcome": "not_sent", "request_sent": False}
    )


def _app(ctx: Context) -> AppContext:
    return ctx.request_context.lifespan_context


def _slave(app: AppContext, slave_id: int | None) -> int:
    return app.settings.default_slave_id if slave_id is None else slave_id


def _is_error(result: Any) -> bool:
    return bool(hasattr(result, "isError") and result.isError())


def _device_error(result: Any) -> tuple[str, dict[str, Any]]:
    """Describe a Modbus exception response, e.g. 'Modbus exception 2 (Illegal Data Address)'."""
    code = getattr(result, "exception_code", None)
    if not isinstance(code, int) or not code:
        return f"Device error: {result}", {}
    message = f"Modbus exception {code}"
    if code in _EXCEPTION_NAMES:
        message += f" ({_EXCEPTION_NAMES[code]})"
    function_code = getattr(result, "function_code", None)
    if isinstance(function_code, int):
        message += f" for function 0x{function_code & 0x7F:02X}"
    return message, {"exception_code": code}


def _span_error(address: int, span: int) -> str | None:
    if address + span > ADDRESS_SPACE:
        return f"address {address} + {span} runs past the last Modbus address (65535)"
    return None


async def _with_timeout(coro: Awaitable[Any], timeout: float | None) -> Any:
    if timeout is None or timeout <= 0:
        return await coro
    return await asyncio.wait_for(coro, timeout=timeout)


async def _reconnect(ctx: Context, app: AppContext, op: str) -> bool:
    """Re-open a dropped connection; True if the client is connected afterwards.

    pymodbus raises ConnectionException while the connection is down and keeps
    retrying in the background with a back-off of up to 5 minutes. close() stops
    that loop, so it cannot race the connect() here, and connect() tries now.
    """
    client = app.modbus_client
    async with app.reconnect_lock:
        if getattr(client, "connected", False):
            return True  # another call reconnected while this one waited
        await ctx.info(f"{op}: not connected, reconnecting to {app.settings.target}")
        try:
            client.close()
            await client.connect()
        except Exception as exc:
            await ctx.error(f"{op}: reconnect failed: {type(exc).__name__}: {exc}")
            return False
        if not getattr(client, "connected", False):
            await ctx.error(f"{op}: reconnect to {app.settings.target} failed")
            return False
        await ctx.info(f"{op}: reconnected to {app.settings.target}")
        return True


async def _retry_call(
    ctx: Context,
    app: AppContext,
    op: str,
    call_factory: Callable[[], Awaitable[Any]],
    max_retries: int | None = None,
    *,
    write: bool = False,
    details: dict[str, Any] | None = None,
) -> tuple[Any | None, str | None, float, int]:
    """Execute call with retries and timeout. Returns (result, error, duration, attempts).

    When the connection is down (pymodbus raises ConnectionException, a
    ModbusException, rather than ConnectionError/OSError), reconnect before the
    first attempt and before each retry, so a dropped TCP session or a restarted
    device does not need a server restart.

    A read that fails is retried up to `max_retries` times. A write is sent at
    most once (`write=True`): only attempts that sent nothing - the connection
    could not be opened, or pymodbus raised ConnectionException, which it does
    before sending - are retried. Once a write has gone out, a missing or late
    reply returns WRITE_NO_REPLY; sending it again could apply it twice. (The
    pymodbus client is created with retries=0 for the same reason; see
    _make_client.) The underlying error, if any, goes to `details["cause"]`.
    """
    settings = app.settings
    client = app.modbus_client
    if max_retries is None:
        max_retries = settings.max_retries
    details = {} if details is None else details
    attempt = 0
    start = time.perf_counter()
    last_err: str | None = None

    def elapsed_ms() -> float:
        return (time.perf_counter() - start) * 1000.0

    while attempt <= max_retries:
        attempt += 1
        if attempt > 1:
            await asyncio.sleep(settings.retry_backoff_base * (2 ** (attempt - 2)))
        if not getattr(client, "connected", True) and not await _reconnect(ctx, app, op):
            last_err = f"cannot reach device at {settings.target}"
            continue
        try:
            result = await _with_timeout(call_factory(), settings.tool_timeout)
            return result, None, elapsed_ms(), attempt
        except ConnectionException as e:
            # Raised before anything is sent, so retrying cannot repeat a write.
            last_err = f"cannot reach device at {settings.target}"
            details["cause"] = f"{type(e).__name__}: {e}"
            await ctx.error(f"{op} failed on attempt {attempt}: {details['cause']}")
        except (ModbusException, asyncio.TimeoutError, ConnectionError, OSError) as e:
            cause = f"{type(e).__name__}: {e}"
            await ctx.error(f"{op} failed on attempt {attempt}: {cause}")
            if write:
                details["cause"] = cause
                return None, WRITE_NO_REPLY, elapsed_ms(), attempt
            last_err = cause
        except Exception as e:  # unexpected
            last_err = f"Unexpected {type(e).__name__}: {e}"
            await ctx.error(f"{op} unexpected error on attempt {attempt}: {last_err}")
            break
    return None, last_err, elapsed_ms(), attempt


async def _request(
    ctx: Context,
    app: AppContext,
    op: str,
    call: Callable[[], Awaitable[Any]],
    meta: dict,
    *,
    write: bool = False,
) -> tuple[Any, dict[str, Any] | None]:
    """One request with retries. Returns (response, None) or (None, failure envelope).

    Adds duration_ms and attempts (and the cause of a failure) to `meta` in place.
    `write=True` sends the request at most once (see _retry_call).
    """
    details: dict[str, Any] = {}
    result, err, duration_ms, attempts = await _retry_call(
        ctx, app, op, call, write=write, details=details
    )
    meta.update(duration_ms=round(duration_ms, 3), attempts=attempts)
    if err is not None:
        meta.update(details)
        if write:
            if err == WRITE_NO_REPLY:
                meta.update(outcome="unknown", request_sent=True)
            elif err.startswith("cannot reach device"):
                meta.update(outcome="not_sent", request_sent=False)
            else:  # an unexpected error: whether the request went out is not known
                meta.update(outcome="unknown", request_sent=None)
                err += "; the write may have been applied; check before retrying"
        return None, _make_result(False, error=err, meta=meta)
    if _is_error(result):
        message, extra = _device_error(result)
        meta.update(extra)
        if write:
            meta.update(outcome="rejected", request_sent=True)
        return None, _make_result(False, error=message, meta=meta)
    if write:
        meta.update(outcome="written", request_sent=True)
    return result, None


async def _chunked_read(
    ctx: Context,
    app: AppContext,
    label: str,
    read_func: Callable[[int, int], Awaitable[Any]],
    start_address: int,
    total_count: int,
    per_request_limit: int,
    attr: str,
) -> tuple[list[Any] | None, str | None, dict[str, Any]]:
    """Read `total_count` registers or bits in requests of at most `per_request_limit`.

    - read_func: async function(start, size) -> pymodbus response
    - attr: response attribute to extract ("registers" or "bits")

    On failure the values read so far are returned in meta["partial"].
    """
    if total_count <= 0:
        return None, "Count must be positive", {}
    span_error = _span_error(start_address, total_count)
    if span_error:
        return None, span_error, {}

    values: list[Any] = []
    chunks: list[dict[str, Any]] = []
    remaining = total_count
    current = start_address
    while remaining > 0:
        size = min(remaining, per_request_limit)
        op = f"{label}[{current}:{current + size - 1}]"
        details: dict[str, Any] = {}
        result, err, duration_ms, attempts = await _retry_call(
            ctx, app, op, functools.partial(read_func, current, size), details=details
        )
        if err is not None:
            return None, err, {"partial": values, "chunks": chunks, **details}
        if _is_error(result):
            message, extra = _device_error(result)
            return None, message, {"partial": values, "chunks": chunks, **extra}
        chunk_vals = getattr(result, attr, None)
        if chunk_vals is None or len(chunk_vals) < size:
            return None, "Malformed response from device", {"partial": values, "chunks": chunks}
        # trim to the requested size: bit responses are padded to a multiple of 8
        values.extend(list(chunk_vals)[:size])
        chunks.append(
            {
                "address": current,
                "count": size,
                "duration_ms": round(duration_ms, 3),
                "attempts": attempts,
            }
        )
        current += size
        remaining -= size
    return values, None, {"chunks": chunks}


# pymodbus renamed the unit-id keyword from `slave` (up to 3.9) to `device_id`
# (3.10 and later); everything else this server calls is the same in both.
@functools.cache
def _unit_kwarg_for(client_type: type) -> str:
    try:
        params = inspect.signature(client_type.read_holding_registers).parameters
    except (AttributeError, TypeError, ValueError):
        return "device_id"
    return "slave" if "slave" in params and "device_id" not in params else "device_id"


def _unit(client: Any, slave_id: int) -> dict[str, int]:
    """Keyword argument that addresses `slave_id` on this pymodbus version."""
    return {_unit_kwarg_for(type(client)): slave_id}


def _json_number(value: Any) -> Any:
    """JSON has no NaN or infinity: spell a non-finite float out rather than emit invalid JSON."""
    if isinstance(value, float) and not math.isfinite(value):
        return "NaN" if math.isnan(value) else ("Infinity" if value > 0 else "-Infinity")
    return value


def _apply_scale(values: list[Any], scale: float, offset: float) -> list[Any]:
    """Apply `value * scale + offset`, leaving raw values untouched when that is the identity.

    Converting unscaled integers to float would silently lose precision for 64-bit values.
    """
    if scale != 1 or offset != 0:
        values = [v * float(scale) + float(offset) for v in values]
    return [_json_number(v) for v in values]


def _make_client(settings: Settings) -> Any:
    # retries=0: on a timeout, pymodbus (3.9-3.15, default retries=3) sends the
    # same request again, which would repeat a write. Reads are retried by
    # _retry_call instead (MODBUS_MAX_RETRIES); writes are never re-sent.
    if settings.modbus_type == "tcp":
        return AsyncModbusTcpClient(host=settings.host, port=settings.port, retries=0)
    if settings.modbus_type == "udp":
        return AsyncModbusUdpClient(host=settings.host, port=settings.port, retries=0)
    return AsyncModbusSerialClient(
        port=settings.serial_port,
        baudrate=settings.baudrate,
        parity=settings.parity,
        stopbits=settings.stopbits,
        bytesize=settings.bytesize,
        timeout=settings.serial_timeout,
        retries=0,
    )


@asynccontextmanager
async def app_lifespan(server: FastMCP) -> AsyncIterator[AppContext]:
    """Create the Modbus client and try to connect; an unreachable device is not fatal.

    The server starts either way, so initialize and tools/list work. Until the
    device answers, the device tools return success: false "cannot reach device
    at ..."; every call tries to connect again (see _retry_call/_reconnect).
    """
    settings, tags = _config if _config is not None else load_config()
    client = _make_client(settings)
    try:
        await client.connect()
    except Exception as exc:  # e.g. a serial port that cannot be opened
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
        yield AppContext(modbus_client=client, settings=settings, tags=tags)
    finally:
        client.close()


def _describe_validation_error(exc: ValidationError) -> str:
    parts = []
    for err in exc.errors():
        where = ".".join(str(p) for p in err.get("loc", ())) or "arguments"
        part = f"{where}: {err.get('msg', 'invalid')}"
        if err.get("type") != "missing" and "input" in err:
            part += f" (got {err['input']!r})"
        parts.append(part)
    return "; ".join(parts)


class ModbusMCP(FastMCP):
    """FastMCP that returns the {success, data, error, meta} envelope for every tool failure.

    FastMCP turns invalid arguments (and any exception a tool raises) into a bare
    error string. Clients of IndustriConnect servers parse the envelope, so give
    them one: success=false with the reason in `error`.
    """

    async def call_tool(self, name: str, arguments: dict[str, Any]) -> Any:
        try:
            return await super().call_tool(name, arguments)
        except ToolError as exc:
            tool = self._tool_manager.get_tool(name)
            if tool is None:  # unknown tool: a protocol-level error, not a tool result
                raise
            cause = exc.__cause__
            if isinstance(cause, ValidationError):
                error = f"Invalid arguments: {_describe_validation_error(cause)}"
            elif cause is not None:
                error = f"Unexpected {type(cause).__name__}: {cause}"
            else:
                error = str(exc)
            meta: dict[str, Any] = {"tool": name}
            if name in WRITE_TOOLS:
                if isinstance(cause, ValidationError):
                    meta.update(outcome="not_sent", request_sent=False)
                else:  # raised somewhere in the tool: whether a request went out is unknown
                    meta.update(outcome="unknown", request_sent=None)
                    error += "; the write may have been applied; check before retrying"
            envelope = _make_result(False, error=error, meta=meta)
            # mcp >= 1.10 also returns structured content; let the tool's own
            # converter shape the envelope exactly like a normal result.
            convert = getattr(getattr(tool, "fn_metadata", None), "convert_result", None)
            if convert is not None:
                return convert(envelope)
            return [TextContent(type="text", text=json.dumps(envelope))]


mcp = ModbusMCP(
    name="Modbus MCP Server",
    instructions=(
        "Tools for one Modbus device. Every tool returns {success, data, error, meta}; "
        "check success before using data. Addresses are zero-based (holding register "
        "40001 is address 0). Write tools only work when the server was started with "
        "MODBUS_WRITES_ENABLED=true. If the device cannot be reached, tools return "
        "success=false with 'cannot reach device' and try again on the next call."
    ),
    dependencies=["pymodbus"],
    lifespan=app_lifespan,
)
# FastMCP has no version argument and reports the mcp SDK's version in
# serverInfo.version unless the low-level server is given one.
mcp._mcp_server.version = __version__


# -----------------------------
# Holding registers and coils
# -----------------------------


@mcp.tool()
async def read_register(address: Address, ctx: Context, slave_id: SlaveId = None) -> dict[str, Any]:
    """Read one holding register (function 3); data.value is its raw 16-bit value."""
    app = _app(ctx)
    client = app.modbus_client
    slave_id = _slave(app, slave_id)
    meta: dict[str, Any] = {"address": address, "slave_id": slave_id}

    async def _call():
        return await client.read_holding_registers(
            address=address, count=1, **_unit(client, slave_id)
        )

    result, failure = await _request(
        ctx, app, f"read_register addr={address} slave={slave_id}", _call, meta
    )
    if failure:
        return failure
    if not getattr(result, "registers", None):
        return _make_result(False, error="Malformed response from device", meta=meta)
    value = result.registers[0]
    await ctx.info(f"Read register {address} from slave {slave_id}: {value}")
    return _make_result(True, data={"value": value}, meta=meta)


@mcp.tool()
async def write_register(
    address: Address, value: RegisterValue, ctx: Context, slave_id: SlaveId = None
) -> dict[str, Any]:
    """Write one holding register (function 6).

    Only works when MODBUS_WRITES_ENABLED=true; otherwise nothing is sent and
    success is false. The write is sent at most once. meta.outcome says what
    happened: written, rejected (the device answered with an exception),
    not_sent, or unknown (no reply: it may have been applied, so check before
    retrying); meta.request_sent says whether the request went out.
    """
    app = _app(ctx)
    slave_id = _slave(app, slave_id)
    meta: dict[str, Any] = {"address": address, "value": value, "slave_id": slave_id}
    if not app.settings.writes_enabled:
        return _not_sent(WRITES_DISABLED, meta=meta)
    if not (0 <= value <= 65535):
        return _not_sent(f"Value {value} out of uint16 range (0-65535)", meta=meta)
    client = app.modbus_client

    async def _call():
        return await client.write_register(address=address, value=value, **_unit(client, slave_id))

    op = f"write_register addr={address} value={value} slave={slave_id}"
    _, failure = await _request(ctx, app, op, _call, meta, write=True)
    if failure:
        return failure
    await ctx.info(f"Wrote {value} to register {address} on slave {slave_id}")
    return _make_result(True, data={"written": value}, meta=meta)


@mcp.tool()
async def read_coils(
    address: Address, count: BitCount, ctx: Context, slave_id: SlaveId = None
) -> dict[str, Any]:
    """Read coils (function 1); data.values is a list of booleans.

    Counts above 2000 are split into several requests.
    """
    app = _app(ctx)
    client = app.modbus_client
    slave_id = _slave(app, slave_id)
    values, err, meta = await _chunked_read(
        ctx,
        app,
        "read_coils",
        lambda start, size: client.read_coils(address=start, count=size, **_unit(client, slave_id)),
        address,
        count,
        MAX_READ_BITS,
        "bits",
    )
    meta = {"address": address, "count": count, "slave_id": slave_id, **meta}
    if err is not None:
        return _make_result(False, error=err, meta=meta)
    await ctx.info(f"Read {count} coils starting at {address} from slave {slave_id}")
    return _make_result(True, data={"values": values}, meta=meta)


@mcp.tool()
async def write_coil(
    address: Address,
    value: Annotated[bool, Field(description="true = ON, false = OFF.")],
    ctx: Context,
    slave_id: SlaveId = None,
) -> dict[str, Any]:
    """Write one coil (function 5).

    Only works when MODBUS_WRITES_ENABLED=true; otherwise nothing is sent and
    success is false. The write is sent at most once. meta.outcome says what
    happened: written, rejected (the device answered with an exception),
    not_sent, or unknown (no reply: it may have been applied, so check before
    retrying); meta.request_sent says whether the request went out.
    """
    app = _app(ctx)
    slave_id = _slave(app, slave_id)
    meta: dict[str, Any] = {"address": address, "value": value, "slave_id": slave_id}
    if not app.settings.writes_enabled:
        return _not_sent(WRITES_DISABLED, meta=meta)
    client = app.modbus_client

    async def _call():
        return await client.write_coil(address=address, value=value, **_unit(client, slave_id))

    op = f"write_coil addr={address} value={value} slave={slave_id}"
    _, failure = await _request(ctx, app, op, _call, meta, write=True)
    if failure:
        return failure
    await ctx.info(f"Wrote {value} to coil {address} on slave {slave_id}")
    return _make_result(True, data={"written": value}, meta=meta)


@mcp.tool()
async def read_input_registers(
    address: Address, count: RegisterCount, ctx: Context, slave_id: SlaveId = None
) -> dict[str, Any]:
    """Read input registers (function 4); data.registers is a list of raw 16-bit values.

    Counts above 125 are split into several requests.
    """
    app = _app(ctx)
    client = app.modbus_client
    slave_id = _slave(app, slave_id)
    values, err, meta = await _chunked_read(
        ctx,
        app,
        "read_input_registers",
        lambda start, size: client.read_input_registers(
            address=start, count=size, **_unit(client, slave_id)
        ),
        address,
        count,
        MAX_READ_REGISTERS,
        "registers",
    )
    meta = {"address": address, "count": count, "slave_id": slave_id, **meta}
    if err is not None:
        return _make_result(False, error=err, meta=meta)
    await ctx.info(f"Read {count} input registers starting at {address} from slave {slave_id}")
    return _make_result(True, data={"registers": values}, meta=meta)


@mcp.tool()
async def read_multiple_holding_registers(
    address: Address, count: RegisterCount, ctx: Context, slave_id: SlaveId = None
) -> dict[str, Any]:
    """Read holding registers (function 3); data.registers is a list of raw 16-bit values.

    Counts above 125 are split into several requests.
    """
    app = _app(ctx)
    client = app.modbus_client
    slave_id = _slave(app, slave_id)
    values, err, meta = await _chunked_read(
        ctx,
        app,
        "read_holding_registers",
        lambda start, size: client.read_holding_registers(
            address=start, count=size, **_unit(client, slave_id)
        ),
        address,
        count,
        MAX_READ_REGISTERS,
        "registers",
    )
    meta = {"address": address, "count": count, "slave_id": slave_id, **meta}
    if err is not None:
        return _make_result(False, error=err, meta=meta)
    await ctx.info(f"Read {count} holding registers starting at {address} from slave {slave_id}")
    return _make_result(True, data={"registers": values}, meta=meta)


@mcp.prompt()
def analyze_register(value: str) -> list[base.Message]:
    """Prompt to analyze a Modbus register value."""
    return [
        base.UserMessage(f"I read this value from a Modbus register: {value}"),
        base.UserMessage("Can you help me understand what it means?"),
        base.AssistantMessage(
            "I'll help analyze the register value. "
            "Please provide any context about the device or system."
        ),
    ]


def main(argv: list[str] | None = None) -> None:
    """Entry point of `modbus-mcp`: read the configuration, then serve MCP over stdio."""
    parser = argparse.ArgumentParser(
        prog="modbus-mcp",
        description="MCP server for Modbus TCP/UDP/serial devices, over stdio. "
        "Configured with MODBUS_* environment variables.",
    )
    parser.add_argument(
        "--env-file",
        metavar="PATH",
        help="read settings from this file (after the process environment). Unlike a "
        ".env found automatically, it may set MODBUS_WRITES_ENABLED.",
    )
    parser.add_argument("--version", action="version", version=f"modbus-mcp {__version__}")
    args = parser.parse_args(argv)
    global _config
    try:
        _config = load_config(args.env_file)
    except ConfigError as exc:
        sys.stderr.write(f"modbus-mcp: configuration error: {exc}\n")
        raise SystemExit(2) from None
    mcp.run()


# -----------------------------
# Additional protocol coverage tools
# -----------------------------


@mcp.tool()
async def read_discrete_inputs(
    address: Address, count: BitCount, ctx: Context, slave_id: SlaveId = None
) -> dict[str, Any]:
    """Read discrete inputs (function 2); data.values is a list of booleans.

    Counts above 2000 are split into several requests.
    """
    app = _app(ctx)
    client = app.modbus_client
    slave_id = _slave(app, slave_id)
    values, err, meta = await _chunked_read(
        ctx,
        app,
        "read_discrete_inputs",
        lambda start, size: client.read_discrete_inputs(
            address=start, count=size, **_unit(client, slave_id)
        ),
        address,
        count,
        MAX_READ_BITS,
        "bits",
    )
    meta = {"address": address, "count": count, "slave_id": slave_id, **meta}
    if err is not None:
        return _make_result(False, error=err, meta=meta)
    await ctx.info(f"Read {count} discrete inputs starting at {address} from slave {slave_id}")
    return _make_result(True, data={"values": values}, meta=meta)


@mcp.tool()
async def write_registers(
    address: Address,
    values: Annotated[
        list[RegisterValue],
        Field(
            min_length=1,
            max_length=MAX_WRITE_REGISTERS,
            description="1-123 register values (0-65535 each), written from `address` up.",
        ),
    ],
    ctx: Context,
    slave_id: SlaveId = None,
) -> dict[str, Any]:
    """Write consecutive holding registers in one request (function 16).

    Only works when MODBUS_WRITES_ENABLED=true; otherwise nothing is sent and
    success is false. The write is sent at most once. meta.outcome says what
    happened: written, rejected (the device answered with an exception),
    not_sent, or unknown (no reply: it may have been applied, so check before
    retrying); meta.request_sent says whether the request went out.
    """
    app = _app(ctx)
    slave_id = _slave(app, slave_id)
    meta: dict[str, Any] = {"address": address, "count": len(values), "slave_id": slave_id}
    if not app.settings.writes_enabled:
        return _not_sent(WRITES_DISABLED, meta=meta)
    if not values or len(values) > MAX_WRITE_REGISTERS:
        return _not_sent(f"Write 1 to {MAX_WRITE_REGISTERS} registers per request", meta=meta)
    for i, v in enumerate(values):
        if not (0 <= v <= 65535):
            return _not_sent(
                f"Value {v} at index {i} out of uint16 range (0-65535)",
                meta={**meta, "index": i, "value": v},
            )
    span_error = _span_error(address, len(values))
    if span_error:
        return _not_sent(span_error, meta=meta)
    client = app.modbus_client

    async def _call():
        return await client.write_registers(
            address=address, values=values, **_unit(client, slave_id)
        )

    op = f"write_registers addr={address} n={len(values)} slave={slave_id}"
    _, failure = await _request(ctx, app, op, _call, meta, write=True)
    if failure:
        return failure
    await ctx.info(f"Wrote {len(values)} registers starting at {address} on slave {slave_id}")
    return _make_result(True, data={"written": len(values)}, meta=meta)


@mcp.tool()
async def write_coils_bulk(
    address: Address,
    values: Annotated[
        list[bool],
        Field(
            min_length=1,
            max_length=MAX_WRITE_COILS,
            description="1-1968 coil states, written from `address` up.",
        ),
    ],
    ctx: Context,
    slave_id: SlaveId = None,
) -> dict[str, Any]:
    """Write consecutive coils in one request (function 15).

    Only works when MODBUS_WRITES_ENABLED=true; otherwise nothing is sent and
    success is false. The write is sent at most once. meta.outcome says what
    happened: written, rejected (the device answered with an exception),
    not_sent, or unknown (no reply: it may have been applied, so check before
    retrying); meta.request_sent says whether the request went out.
    """
    app = _app(ctx)
    slave_id = _slave(app, slave_id)
    meta: dict[str, Any] = {"address": address, "count": len(values), "slave_id": slave_id}
    if not app.settings.writes_enabled:
        return _not_sent(WRITES_DISABLED, meta=meta)
    if not values or len(values) > MAX_WRITE_COILS:
        return _not_sent(f"Write 1 to {MAX_WRITE_COILS} coils per request", meta=meta)
    span_error = _span_error(address, len(values))
    if span_error:
        return _not_sent(span_error, meta=meta)
    client = app.modbus_client

    async def _call():
        return await client.write_coils(address=address, values=values, **_unit(client, slave_id))

    op = f"write_coils addr={address} n={len(values)} slave={slave_id}"
    _, failure = await _request(ctx, app, op, _call, meta, write=True)
    if failure:
        return failure
    await ctx.info(f"Wrote {len(values)} coils starting at {address} on slave {slave_id}")
    return _make_result(True, data={"written": len(values)}, meta=meta)


@mcp.tool()
async def mask_write_register(
    address: Address,
    and_mask: Mask,
    or_mask: Mask,
    ctx: Context,
    slave_id: SlaveId = None,
) -> dict[str, Any]:
    """Change bits of one holding register in place (function 22).

    The device sets: register = (register AND and_mask) OR (or_mask AND NOT and_mask).
    Only works when MODBUS_WRITES_ENABLED=true; otherwise nothing is sent and
    success is false. The write is sent at most once. meta.outcome says what
    happened: written, rejected (the device answered with an exception),
    not_sent, or unknown (no reply: it may have been applied, so check before
    retrying); meta.request_sent says whether the request went out.
    """
    app = _app(ctx)
    slave_id = _slave(app, slave_id)
    meta: dict[str, Any] = {
        "address": address,
        "and_mask": and_mask,
        "or_mask": or_mask,
        "slave_id": slave_id,
    }
    if not app.settings.writes_enabled:
        return _not_sent(WRITES_DISABLED, meta=meta)
    client = app.modbus_client

    async def _call():
        return await client.mask_write_register(
            address=address, and_mask=and_mask, or_mask=or_mask, **_unit(client, slave_id)
        )

    op = f"mask_write_register addr={address} and={and_mask} or={or_mask} slave={slave_id}"
    _, failure = await _request(ctx, app, op, _call, meta, write=True)
    if failure:
        return failure
    await ctx.info(f"Mask write register {address} on slave {slave_id}")
    return _make_result(
        True, data={"address": address, "and_mask": and_mask, "or_mask": or_mask}, meta=meta
    )


@mcp.tool()
async def read_device_information(
    ctx: Context,
    slave_id: SlaveId = None,
    read_code: Annotated[
        int,
        Field(ge=1, le=4, description="1 = basic, 2 = regular, 3 = extended, 4 = one object."),
        BeforeValidator(_reject_bool),
    ] = 0x03,
    object_id: Annotated[
        int,
        Field(ge=0, le=255, description="First object to read (0 = VendorName)."),
        BeforeValidator(_reject_bool),
    ] = 0x00,
) -> dict[str, Any]:
    """Read the device identification objects (function 43 / MEI type 14).

    data maps object ids to text: 0 VendorName, 1 ProductCode, 2 MajorMinorRevision,
    3 VendorUrl, 4 ProductName, 5 ModelName, 6 UserApplicationName.
    """
    app = _app(ctx)
    client = app.modbus_client
    slave_id = _slave(app, slave_id)
    meta: dict[str, Any] = {"slave_id": slave_id, "read_code": read_code, "object_id": object_id}

    async def _call():
        return await client.read_device_information(
            read_code=read_code, object_id=object_id, **_unit(client, slave_id)
        )

    op = f"read_device_information slave={slave_id} code={read_code} obj={object_id}"
    result, failure = await _request(ctx, app, op, _call, meta)
    if failure:
        return failure
    information = getattr(result, "information", None) or {}
    data = {
        str(key): value.decode("utf-8", errors="replace")
        if isinstance(value, (bytes, bytearray))
        else value
        for key, value in dict(information).items()
    }
    return _make_result(True, data=data, meta=meta)


# -----------------------------
# Typed read helpers/tools
# -----------------------------


async def _read_typed(
    ctx: Context,
    table: str,
    address: int,
    dtype: str,
    count: int,
    byteorder: str,
    wordorder: str,
    scale: float,
    offset: float,
    slave_id: int | None,
) -> dict[str, Any]:
    app = _app(ctx)
    client = app.modbus_client
    slave_id = _slave(app, slave_id)
    base_meta: dict[str, Any] = {
        "address": address,
        "count": count,
        "dtype": dtype,
        "slave_id": slave_id,
    }
    try:
        size = codec.register_count(dtype)
        byteorder = codec.check_order(byteorder, "byteorder")
        wordorder = codec.check_order(wordorder, "wordorder")
    except codec.CodecError as e:
        return _make_result(False, error=str(e), meta=base_meta)
    method = client.read_holding_registers if table == "holding" else client.read_input_registers
    regs, err, meta = await _chunked_read(
        ctx,
        app,
        f"read_{table}_registers",
        lambda start, sz: method(address=start, count=sz, **_unit(client, slave_id)),
        address,
        size * count,
        MAX_READ_REGISTERS,
        "registers",
    )
    if err is not None:
        return _make_result(False, error=err, meta={**base_meta, **meta})
    try:
        vals = _apply_scale(codec.decode(regs, dtype, count, byteorder, wordorder), scale, offset)
    except codec.CodecError as e:
        return _make_result(False, error=str(e), meta=base_meta)
    return _make_result(
        True,
        data={"values": vals},
        meta={
            **base_meta,
            "byteorder": byteorder,
            "wordorder": wordorder,
            "scale": scale,
            "offset": offset,
            **meta,
        },
    )


@mcp.tool()
async def read_holding_typed(
    address: Address,
    dtype: DType,
    ctx: Context,
    count: TypedCount = 1,
    byteorder: ByteOrder = "big",
    wordorder: WordOrder = "big",
    scale: Scale = 1.0,
    offset: Offset = 0.0,
    slave_id: SlaveId = None,
) -> dict[str, Any]:
    """Read holding registers and decode them as typed values.

    data.values holds `count` values of `dtype`, each multiplied by `scale` and
    increased by `offset`. NaN and infinities come back as the strings "NaN",
    "Infinity" and "-Infinity".
    """
    return await _read_typed(
        ctx, "holding", address, dtype, count, byteorder, wordorder, scale, offset, slave_id
    )


@mcp.tool()
async def read_input_typed(
    address: Address,
    dtype: DType,
    ctx: Context,
    count: TypedCount = 1,
    byteorder: ByteOrder = "big",
    wordorder: WordOrder = "big",
    scale: Scale = 1.0,
    offset: Offset = 0.0,
    slave_id: SlaveId = None,
) -> dict[str, Any]:
    """Read input registers and decode them as typed values.

    data.values holds `count` values of `dtype`, each multiplied by `scale` and
    increased by `offset`. NaN and infinities come back as the strings "NaN",
    "Infinity" and "-Infinity".
    """
    return await _read_typed(
        ctx, "input", address, dtype, count, byteorder, wordorder, scale, offset, slave_id
    )


# -----------------------------
# Tag map tools
# -----------------------------


@mcp.tool()
async def list_tags(ctx: Context) -> dict[str, Any]:
    """List the named tags defined in the tag map (REGISTER_MAP_FILE), with their definitions."""
    app = _app(ctx)
    tags = [tag.as_dict() for _, tag in sorted(app.tags.items())]
    return _make_result(True, data={"tags": tags})


def _unknown_tag(name: str) -> dict[str, Any]:
    return _make_result(
        False, error=f"Unknown tag {name!r}; list_tags shows the defined tags", meta={"tag": name}
    )


@mcp.tool()
async def read_tag(
    name: Annotated[str, Field(description="Tag name from list_tags.")], ctx: Context
) -> dict[str, Any]:
    """Read a named tag from the tag map.

    Tags with a dtype are decoded and scaled like read_holding_typed/read_input_typed;
    other tags return raw registers or bits.
    """
    tag = _app(ctx).tags.get(name)
    if tag is None:
        return _unknown_tag(name)
    if tag.table in {"holding", "input"} and tag.dtype:
        return await _read_typed(
            ctx,
            tag.table,
            tag.address,
            tag.dtype,
            tag.count,
            tag.byteorder,
            tag.wordorder,
            tag.scale,
            tag.offset,
            tag.slave_id,
        )
    if tag.table == "holding":
        return await read_multiple_holding_registers(tag.address, tag.count, ctx, tag.slave_id)
    if tag.table == "input":
        return await read_input_registers(tag.address, tag.count, ctx, tag.slave_id)
    if tag.table == "coil":
        return await read_coils(tag.address, tag.count, ctx, tag.slave_id)
    return await read_discrete_inputs(tag.address, tag.count, ctx, tag.slave_id)


@mcp.tool()
async def write_tag(
    name: Annotated[str, Field(description="Tag name from list_tags.")],
    value: Annotated[
        Any,
        Field(
            description=(
                "Coil tags: true or false. Holding-register tags: a number, or a list "
                "of `count` numbers for a tag with count > 1."
            )
        ),
    ],
    ctx: Context,
) -> dict[str, Any]:
    """Write a named holding-register or coil tag from the tag map.

    Holding-register values are in the same units read_tag returns: the tag's
    scale and offset are reversed, then the value is encoded with its dtype
    (default uint16) and byte/word order. Input and discrete-input tags are
    read-only. Only works when MODBUS_WRITES_ENABLED=true; otherwise nothing is
    sent and success is false. meta.outcome and meta.request_sent work as in
    write_register.
    """
    app = _app(ctx)
    meta: dict[str, Any] = {"tag": name}
    if not app.settings.writes_enabled:
        return _not_sent(WRITES_DISABLED, meta=meta)
    tag = app.tags.get(name)
    if tag is None:
        return _not_sent(_unknown_tag(name)["error"], meta)
    if not tag.writable:
        return _not_sent(f"Tag {name!r} is read-only ({tag.table})", meta=meta)
    if tag.table == "coil":
        if tag.count != 1:
            error = f"Tag {name!r} covers {tag.count} coils; write them with write_coils_bulk"
            return _not_sent(error, meta=meta)
        if not (isinstance(value, bool) or (isinstance(value, int) and value in (0, 1))):
            return _not_sent(f"Coil tag {name!r} takes true or false, got {value!r}", meta=meta)
        result = await write_coil(tag.address, bool(value), ctx, tag.slave_id)
        result["meta"]["tag"] = name
        return result
    vals = value if isinstance(value, list) else [value]
    if len(vals) != tag.count:
        return _not_sent(
            f"Tag {name!r} holds {tag.count} value(s); got {len(vals)}",
            meta=meta,
        )
    try:
        raw = _unscale(vals, tag)
        regs = codec.encode(raw, tag.dtype or "uint16", tag.byteorder, tag.wordorder)
    except codec.CodecError as e:
        return _not_sent(f"Tag {name!r}: {e}", meta=meta)
    result = await write_registers(tag.address, regs, ctx, tag.slave_id)
    result["meta"].update(tag=name, raw_values=raw)
    return result


def _unscale(values: list[Any], tag: Tag) -> list[Any]:
    """Turn engineering values back into raw values: (value - offset) / scale.

    read_tag returns raw * scale + offset, so write_tag accepts the same units. For
    an integer dtype the result must be a whole number; 25.35 on a tag with scale
    0.1 is refused rather than rounded to a different value.
    """
    if tag.scale == 1 and tag.offset == 0:
        return values
    raw: list[Any] = []
    for value in values:
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise codec.CodecError(f"value must be a number, got {value!r}")
        r = (value - tag.offset) / tag.scale
        if tag.dtype not in ("float32", "float64"):
            if not math.isfinite(r) or abs(r - round(r)) > 1e-9 * max(1.0, abs(r)):
                raise codec.CodecError(
                    f"{value!r} is not a whole number of steps of scale {tag.scale}"
                    f" (offset {tag.offset}); the raw value would be {r!r}"
                )
            r = round(r)
        raw.append(r)
    return raw


# -----------------------------
# Health
# -----------------------------


@mcp.tool()
async def ping(
    ctx: Context,
    slave_id: SlaveId = None,
    address: Annotated[
        int,
        Field(ge=0, le=65535, description="Holding register to probe (default 0)."),
        BeforeValidator(_reject_bool),
    ] = 0,
) -> dict[str, Any]:
    """Check that the device answers, and report the server's configuration.

    Reads one holding register (address 0 unless given) as a probe. Any Modbus
    reply counts as reachable, including an exception reply such as Illegal Data
    Address from a device without that register; no reply (timeout, connection
    refused) does not, and then success is false. Connects first if the server
    is not connected (the device was unreachable at startup, or the connection
    dropped); if that fails, the error is "cannot reach device at ...".
    """
    app = _app(ctx)
    settings = app.settings
    client = app.modbus_client
    slave_id = _slave(app, slave_id)

    async def _probe():
        return await client.read_holding_registers(
            address=address, count=1, **_unit(client, slave_id)
        )

    details: dict[str, Any] = {}
    result, probe_error, probe_ms, _ = await _retry_call(
        ctx, app, "ping", _probe, max_retries=0, details=details
    )
    device_reachable = probe_error is None
    if probe_error is not None:
        probe_response = None
    elif _is_error(result):
        probe_response = _device_error(result)[0]
    else:
        probe_response = "ok"

    network = settings.modbus_type in {"tcp", "udp"}
    status = {
        "connected": bool(getattr(client, "connected", False)),
        "device_reachable": device_reachable,
        "probe_address": address,
        "probe_slave_id": slave_id,
        "probe_response": probe_response,
        "probe_latency_ms": round(probe_ms, 3),
        "probe_error": probe_error,
        "type": settings.modbus_type,
        "host": settings.host if network else None,
        "port": settings.port if network else None,
        "serial_port": settings.serial_port if settings.modbus_type == "serial" else None,
        "writes_enabled": settings.writes_enabled,
        "max_retries": settings.max_retries,
        "tool_timeout": settings.tool_timeout,
        "tag_count": len(app.tags),
    }
    if device_reachable:
        error = None
    elif probe_error.startswith("cannot reach device"):
        error = probe_error
    else:
        error = f"Device at {settings.target} did not answer: {probe_error}"
    return _make_result(device_reachable, data=status, error=error, meta=details)
