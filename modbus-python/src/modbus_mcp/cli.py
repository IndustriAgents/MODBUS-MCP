"""Modbus MCP server: Modbus TCP/UDP/serial devices as MCP tools over stdio.

stdout carries the MCP JSON-RPC stream, so nothing in this module may print;
diagnostics go to stderr (logging) or to the client as MCP log messages.
"""

# No `from __future__ import annotations` here: FastMCP 1.6 finds the Context
# parameter by comparing the annotation object itself (`is Context`).
import argparse
import asyncio
import contextlib
import contextvars
import functools
import inspect
import json
import logging
import math
import os
import re
import struct
import sys
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Annotated, Any, Literal

import anyio
from dotenv import dotenv_values
from mcp.server.fastmcp import Context, FastMCP
from mcp.server.fastmcp.exceptions import ToolError
from mcp.server.fastmcp.prompts import base
from mcp.server.stdio import stdio_server
from mcp.types import TextContent, ToolAnnotations
from pydantic import Field, ValidationError
from pymodbus.client import AsyncModbusSerialClient, AsyncModbusTcpClient, AsyncModbusUdpClient
from pymodbus.exceptions import ConnectionException, ModbusException

from . import __version__, codec

if sys.version_info < (3, 11):  # anyio raises the backport's groups on 3.10
    from exceptiongroup import BaseExceptionGroup
from .config import ConfigError, Settings, Tag, load_tag_map

log = logging.getLogger("modbus_mcp")  # stderr, via FastMCP's logging setup

WRITES_DISABLED = (
    "Writes are disabled by configuration (set MODBUS_WRITES_ENABLED=true to allow them)"
)
# A write request went out but no reply came back. It is never sent again.
WRITE_NO_REPLY = "no reply; the write may have been applied; check before retrying"
# A reply came back, but it is not the reply to this write (another request's, or
# garbled): the write went out and may have been applied.
WRITE_REPLY_MISMATCH = (
    "the device's reply does not match the write; "
    "the write may have been applied; check before retrying"
)
READ_REPLY_MISMATCH = "the device's reply does not match the request"
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
# MCP tool annotations. A write changes the device and must never be advertised
# as idempotent (a client could retry it); reads only read.
READ_ONLY = ToolAnnotations(readOnlyHint=True)
DEVICE_WRITE = ToolAnnotations(readOnlyHint=False, destructiveHint=True, idempotentHint=False)

# meta.outcome of a write: "written" (the device acknowledged it with a reply
# that echoes the request), "rejected" (the device refused it with an exception
# that means nothing was done, see REFUSED), "not_sent" (refused before anything
# was sent), "unknown" (sent, then anything else: no reply, a reply that does not
# match the request, or an exception that does not rule out a partial or later
# write; it may have been applied).
MAY_HAVE_BEEN_APPLIED = "the write may have been applied; check before retrying"
# Modbus exceptions that mean the device did not act on the request at all:
# 1 Illegal Function, 2 Illegal Data Address, 3 Illegal Data Value (the
# request is refused as invalid), 6 Server Device Busy (retransmit later),
# 10 Gateway Path Unavailable (the gateway could not forward it). The others
# do not rule a write out: 4 Server Device Failure (failed while performing
# it), 5 Acknowledge (accepted, still processing), 8 Memory Parity Error,
# 11 Gateway Target Device Failed to Respond (forwarded, no answer).
REFUSED = frozenset({1, 2, 3, 6, 10})

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


# Integers and booleans are strict: pydantic's lax mode would turn true into 1,
# "5" into 5 and 5.0 into 5, and 1 or "on" into true. For an address, a register
# value or a coil state that would quietly read or write something nobody asked
# for, so only a JSON integer (or true/false) of the right type is accepted.
def _int(lo: int, hi: int, description: str) -> Any:
    return Annotated[int, Field(ge=lo, le=hi, strict=True, description=description)]


StrictBool = Annotated[bool, Field(strict=True)]


Address = _int(0, 65535, "Zero-based Modbus address (0-65535).")
RegisterValue = _int(0, 65535, "Unsigned 16-bit register value (0-65535).")
Mask = _int(0, 65535, "16-bit mask (0-65535).")
RegisterCount = _int(1, ADDRESS_SPACE, "Number of registers to read (at least 1).")
BitCount = _int(1, ADDRESS_SPACE, "Number of bits to read (at least 1).")
TypedCount = _int(1, ADDRESS_SPACE, "Number of typed values to decode (at least 1).")
SlaveId = Annotated[
    Annotated[int, Field(ge=0, le=255, strict=True)] | None,
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
    # Number of request frames handed to the transport so far (see SendGate);
    # None when unknown. It tells a write that went out from one that did not.
    frames_sent: Callable[[], int] | None = None
    # True once the MCP client has disconnected: nothing may be sent any more.
    closing: Callable[[], bool] = lambda: False
    # The raw bytes received since the last request was sent (see SendGate), for
    # checks the decoded reply cannot make; None when not available.
    last_reply: Callable[[], bytes] | None = None
    # After a request went unanswered on a serial line, no new connection before this
    # (time.monotonic()), so a late reply has time to arrive and be dropped.
    quiet_until: float = 0.0


class RequestNotSent(Exception):
    """Raised by SendGate in place of sending a request that must not go out."""


class NotConnected(RequestNotSent):
    """Raised by SendGate when the connection is gone: nothing was written (pymodbus
    would drop the frame silently), and a new connection may succeed."""


_RX_KEEP = 512  # bytes of the latest reply kept for checks (a Modbus frame is <= 260)


class SendGate:
    """pymodbus trace_packet hook: the last check before a request leaves, and a counter.

    pymodbus calls trace_packet(True, frame) once a request is fully encoded,
    synchronously and immediately before handing it to the transport: there is
    no await between this check and the send. It refuses the send (raising
    RequestNotSent, so nothing is written) when the MCP client has disconnected,
    the tool call was cancelled, or its deadline has passed; a request that was
    queued, reconnecting or waiting then is never sent later. It also refuses
    (NotConnected) when the connection has been closed, since pymodbus would
    otherwise drop the frame silently. Otherwise it counts the frame, so an
    unchanged count means nothing went out.

    trace_packet(False, data) is called with the bytes received; the gate keeps
    those that arrived since the last send (`rx`).
    """

    def __init__(self, closing: Callable[[], bool] = lambda: False) -> None:
        self.sent = 0
        self.client: Any = None  # the pymodbus client, once created
        self.rx = bytearray()
        self._closing = closing

    def __call__(self, sending: bool, data: bytes) -> bytes:
        if not sending:
            self.rx += data
            del self.rx[:-_RX_KEEP]
            return data
        reason = _why_not_send(self._closing())
        if reason is not None:
            raise RequestNotSent(reason)
        context = getattr(self.client, "ctx", None)
        if context is not None and not getattr(context, "transport", None):
            raise NotConnected("not connected (the connection was closed)")
        self.sent += 1
        self.rx = bytearray()
        state = _CALL.get()  # the call whose task is sending this frame
        if state is not None:
            state.frames += 1
        return data


# -----------------------------
# Configuration
# -----------------------------

# Set by main(); the lifespan falls back to load_config() when the server is
# started some other way (e.g. `mcp dev`).
_config: tuple[Settings, dict[str, Tag]] | None = None


# Settings that let the server change the device. They are honoured only from
# the real process environment or a file passed explicitly with --env-file,
# never from the .env file loaded automatically.
GATE_VARIABLES = ("MODBUS_WRITES_ENABLED",)
# Besides MODBUS_*, the only setting the automatically loaded .env may set.
OWN_EXTRAS = ("REGISTER_MAP_FILE",)
PACKAGE_DIR = Path(__file__).resolve().parent
_EXPORT_PREFIX = re.compile(r"^\s*export\s+", re.IGNORECASE)
_PROJECT_NAME = re.compile(r'^name\s*=\s*"modbus-mcp"\s*$', re.MULTILINE)


def implicit_dotenv(package_dir: Path = PACKAGE_DIR) -> Path | None:
    """The one .env file read automatically: modbus-python/.env of a source checkout.

    It is found relative to the package source (src/modbus_mcp -> ../../.env) and
    only when that directory is this project (its pyproject.toml names
    modbus-mcp). Never the working directory or a parent directory: the working
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
            f"ignoring {gate} from {found}: it is only read from the process"
            " environment or a file given with --env-file"
        )
    for key, value in values.items():
        if key in GATE_VARIABLES:
            continue
        if not (key.startswith("MODBUS_") or key in OWN_EXTRAS):
            _warn(
                f"ignoring {key} from {found}: the automatically loaded .env may only set"
                f" modbus-mcp's own settings (MODBUS_*, {', '.join(OWN_EXTRAS)})"
            )
            continue
        env.setdefault(key, value)
    return env


def load_config(env_file: str | None = None) -> tuple[Settings, dict[str, Tag]]:
    """Read the settings (see read_environment) and the tag map."""
    settings = Settings.from_env(read_environment(env_file))
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
    """Await `coro`, giving up after `timeout` seconds (None: no limit; <= 0: at once).

    Raises TimeoutError. anyio runs `coro` in this task (asyncio.wait_for used a
    separate task on Python 3.10, whose late exception was then never retrieved).
    """
    if timeout is None:
        return await coro
    with anyio.fail_after(max(timeout, 0.0)):
        return await coro


def _desync(app: AppContext) -> None:
    """A request went out and its reply was not taken: close the connection.

    A late reply could otherwise be read as the reply to the next request: Modbus
    RTU has no transaction id, and pymodbus before 3.12 does not check TCP's. Called
    synchronously in the step the request failed, before another task (queued on
    pymodbus' lock) can send on the same connection; that one then finds no
    transport (SendGate: NotConnected) and reconnects. On a serial line the new
    connection waits for a quiet period (MODBUS_TIMEOUT) first.
    """
    with contextlib.suppress(Exception):
        app.modbus_client.close()
    if app.settings.modbus_type == "serial":
        app.quiet_until = time.monotonic() + app.settings.serial_timeout


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
        quiet = app.quiet_until - time.monotonic()
        if quiet > 0:  # let a late reply on the serial line arrive (and be dropped) first
            await asyncio.sleep(quiet)
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


@dataclass
class _CallState:
    """One tool call in progress: its deadline, and whether it may still send."""

    expires: float  # event-loop time
    seconds: float
    cancelled: bool = False  # it ended without a result: nothing of it may be sent
    frames: int = 0  # requests of THIS call handed to the transport (counted by SendGate)

    @property
    def sent(self) -> bool:
        return self.frames > 0

    @classmethod
    def start(cls, seconds: float) -> "_CallState":
        return cls(asyncio.get_running_loop().time() + seconds, seconds)


# The tool call in progress, seen by everything it runs, including SendGate.
_CALL: contextvars.ContextVar[_CallState | None] = contextvars.ContextVar(
    "modbus_mcp_call", default=None
)
# How long after the deadline the hard backstop in ModbusMCP.call_tool fires.
DEADLINE_BACKSTOP = 2.0


def _remaining() -> float | None:
    """Seconds left before the current tool call's deadline; None without one."""
    state = _CALL.get()
    if state is None:
        return None
    return state.expires - asyncio.get_running_loop().time()


def _abandoned(closing: bool) -> str | None:
    """Why nobody wants the current call's result any more, or None."""
    if closing:
        return "the MCP client disconnected"
    state = _CALL.get()
    if state is None:
        return None
    if state.cancelled:
        return "the call was cancelled"
    return None


def _why_not_send(closing: bool) -> str | None:
    """Why the current call must not send anything now, or None."""
    reason = _abandoned(closing)
    if reason is not None:
        return reason
    state = _CALL.get()
    if state is not None and asyncio.get_running_loop().time() >= state.expires:
        return _deadline_error()
    return None


def _deadline_error() -> str:
    state = _CALL.get()
    seconds = state.seconds if state else 0
    return f"no result within the {seconds:g} s deadline (MODBUS_CALL_TIMEOUT)"


def _limit(timeout: float | None) -> float | None:
    """`timeout`, shortened to what is left of the deadline."""
    remaining = _remaining()
    if remaining is None:
        return timeout
    return remaining if timeout is None else min(timeout, remaining)


async def run_with_deadline(
    seconds: float, coro: Awaitable[Any], state: _CallState | None = None
) -> Any:
    """Run `coro` (a tool call) with an overall deadline of `seconds`.

    _retry_call shortens every request, reconnect and backoff to fit, so a call
    ends with a precise outcome when the deadline is reached. As a backstop, the
    whole call is cancelled DEADLINE_BACKSTOP seconds later (TimeoutError).
    If the call ends without a result for any reason (cancelled by the client,
    the client disconnecting, the backstop), it is marked cancelled first, so
    nothing it queued can be sent afterwards (see SendGate).
    """
    state = state or _CallState.start(seconds)
    token = _CALL.set(state)
    try:
        with anyio.fail_after(seconds + DEADLINE_BACKSTOP):
            return await coro
    except BaseException:
        state.cancelled = True
        raise
    finally:
        _CALL.reset(token)


def _frames_now(app: AppContext) -> int | None:
    """Frames handed to the transport so far: by the current tool call if there is one
    (other calls run concurrently and must not count), else by the client; None if
    unknown."""
    state = _CALL.get()
    if state is not None:
        return state.frames
    return app.frames_sent() if app.frames_sent else None


def _sent_since(app: AppContext, frames_before: int | None) -> bool | None:
    """Was a frame handed to the transport since `frames_before`? None if unknown."""
    now = _frames_now(app)
    if frames_before is None or now is None:
        return None
    return now != frames_before


async def _retry_call(
    ctx: Context,
    app: AppContext,
    op: str,
    call_factory: Callable[[], Awaitable[Any]],
    max_retries: int | None = None,
    *,
    write: bool = False,
    details: dict[str, Any] | None = None,
    check: Callable[[Any], dict[str, Any] | None] | None = None,
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

    `check(reply)` says whether the reply belongs to this request (function code,
    count, echo); a reply that does not is not used (a read is retried, a write
    returns WRITE_REPLY_MISMATCH) and details["reply_mismatch"] says why. Whenever a
    request went out and its reply was not taken, the connection is closed
    (_desync), so a late reply cannot be taken for the next request's.
    """
    settings = app.settings
    client = app.modbus_client
    if max_retries is None:
        max_retries = settings.max_retries
    details = {} if details is None else details
    attempt = 0
    start = time.perf_counter()
    last_err: str | None = None
    frames_at_start = _frames_now(app)

    def elapsed_ms() -> float:
        return (time.perf_counter() - start) * 1000.0

    while attempt <= max_retries:
        attempt += 1
        if attempt > 1:
            backoff = settings.retry_backoff_base * (2 ** (attempt - 2))
            await asyncio.sleep(max(0.0, _limit(backoff) or 0.0))
        not_now = _abandoned(app.closing())
        if not_now is not None:
            # The client disconnected or the call was cancelled: stop, send nothing more.
            last_err = f"the request was not sent: {not_now}"
            details.update(request_sent=_sent_since(app, frames_at_start), cause=not_now)
            break
        remaining = _remaining()
        if remaining is not None and remaining <= 0:  # the call's deadline has passed
            last_err = _deadline_error() if last_err is None else f"{_deadline_error()}: {last_err}"
            if write:  # writes are only retried while nothing has gone out
                details["request_sent"] = _sent_since(app, frames_at_start)
            break
        if not getattr(client, "connected", True):
            try:
                connected = await _with_timeout(_reconnect(ctx, app, op), _limit(None))
            except (TimeoutError, asyncio.TimeoutError):
                connected = False
            if not connected:
                last_err = f"cannot reach device at {settings.target}"
                continue
        frames_before = _frames_now(app)
        try:
            try:
                result = await _with_timeout(call_factory(), _limit(settings.tool_timeout))
            except BaseException:
                if _sent_since(app, frames_before) is not False:
                    _desync(app)  # synchronously, before a queued request can send
                raise
            mismatch = check(result) if check is not None else None
            if mismatch:
                _desync(app)
                details["reply_mismatch"] = mismatch
                await ctx.error(f"{op}: the reply does not match the request: {mismatch}")
                if write:
                    return None, WRITE_REPLY_MISMATCH, elapsed_ms(), attempt
                last_err = READ_REPLY_MISMATCH
                continue
            return result, None, elapsed_ms(), attempt
        except NotConnected as e:  # the connection was closed under it: nothing went out
            last_err = f"cannot reach device at {settings.target}"
            details["cause"] = str(e)
            await ctx.error(f"{op} failed on attempt {attempt}: {e}")
        except RequestNotSent as e:  # SendGate refused: nothing went out
            last_err = f"the request was not sent: {e}"
            details.update(request_sent=False, cause=str(e))
            break
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
                if _sent_since(app, frames_before) is False:  # nothing went out
                    details["request_sent"] = False
                    return None, f"the write was not sent: {cause}", elapsed_ms(), attempt
                return None, WRITE_NO_REPLY, elapsed_ms(), attempt
            last_err = cause
        except Exception as e:  # unexpected, e.g. while building or encoding the request
            last_err = f"Unexpected {type(e).__name__}: {e}"
            await ctx.error(f"{op} unexpected error on attempt {attempt}: {last_err}")
            if write:
                details["request_sent"] = _sent_since(app, frames_before)
                if details["request_sent"] is False:
                    last_err = f"the write was not sent: {last_err}"
            break
    return None, last_err, elapsed_ms(), attempt


def reply_check(
    function_code: int,
    *,
    registers: int | None = None,
    bits: int | None = None,
    echo: dict[str, Any] | None = None,
    raw: tuple[bytes, Callable[[], bytes] | None] | None = None,
) -> Callable[[Any], dict[str, Any] | None]:
    """A check that a reply belongs to the request: what is wrong with it, or None.

    - an exception reply must carry this request's function code (+ 0x80);
    - a normal reply must have the function code, exactly `registers` registers or
      `bits` bits (padded to whole bytes, as on the wire), and echo the fields in
      `echo` (Modbus write replies repeat the request);
    - `raw` = (expected PDU, provider of the raw reply bytes): the reply frame must
      end with that PDU (for TCP/UDP) or with it followed by the CRC (RTU). It is for
      replies the decoded object cannot show exactly (pymodbus decodes any non-zero
      FC 5 value as ON).

    Modbus RTU has no transaction id, so a reply to an earlier request (a late one,
    or one for another unit) can otherwise be taken for this request's.
    """

    def check(response: Any) -> dict[str, Any] | None:
        got = getattr(response, "function_code", None)
        if _is_error(response):
            if not isinstance(got, int) or got & 0x7F != function_code:
                return {"function_code": {"expected": function_code | 0x80, "got": got}}
            return None
        problems: dict[str, Any] = {}
        if got != function_code:
            problems["function_code"] = {"expected": function_code, "got": got}
        if registers is not None:
            count = len(getattr(response, "registers", None) or [])
            if count != registers:
                problems["registers"] = {"expected": registers, "got": count}
        if bits is not None:
            count = len(getattr(response, "bits", None) or [])
            if count != 8 * ((bits + 7) // 8):
                problems["bits"] = {"expected": 8 * ((bits + 7) // 8), "got": count}
        if echo:
            problems.update(_echo_mismatch(response, echo))
        if raw is not None and raw[1] is not None:
            pdu, frame = raw[0], bytes(raw[1]())
            if not (frame.endswith(pdu) or frame[:-2].endswith(pdu)):
                problems["raw_reply"] = {"expected_pdu": pdu.hex(), "got": frame.hex()}
        return problems or None

    return check


async def _request(
    ctx: Context,
    app: AppContext,
    op: str,
    call: Callable[[], Awaitable[Any]],
    meta: dict,
    *,
    check: Callable[[Any], dict[str, Any] | None],
    write: bool = False,
) -> tuple[Any, dict[str, Any] | None]:
    """One request with retries. Returns (response, None) or (None, failure envelope).

    Adds duration_ms and attempts (and the cause of a failure) to `meta` in place.
    `check` (see reply_check) decides whether a reply belongs to this request.
    `write=True` sends the request at most once (see _retry_call) and sets
    meta.outcome/request_sent; a reply that does not match is "unknown", not
    "written" or "rejected".
    """
    details: dict[str, Any] = {}
    result, err, duration_ms, attempts = await _retry_call(
        ctx, app, op, call, write=write, details=details, check=check
    )
    meta.update(duration_ms=round(duration_ms, 3), attempts=attempts)
    if err is not None:
        meta.update(details)
        if write:
            request_sent = meta.pop("request_sent", None)
            if err in (WRITE_NO_REPLY, WRITE_REPLY_MISMATCH):
                meta.update(outcome="unknown", request_sent=True)
            elif err.startswith("cannot reach device") or request_sent is False:
                meta.update(outcome="not_sent", request_sent=False)
            else:  # it went out, or it is not known whether it did
                meta.update(outcome="unknown", request_sent=request_sent)
                err += f"; {MAY_HAVE_BEEN_APPLIED}"
        return None, _make_result(False, error=err, meta=meta)
    if _is_error(result):  # an exception reply to THIS request (see reply_check)
        message, extra = _device_error(result)
        meta.update(extra)
        if write:
            if extra.get("exception_code") in REFUSED:
                meta.update(outcome="rejected", request_sent=True)
            else:
                meta.update(outcome="unknown", request_sent=True)
                message += f"; {MAY_HAVE_BEEN_APPLIED}"
        return None, _make_result(False, error=message, meta=meta)
    if write:
        meta.update(outcome="written", request_sent=True)
    return result, None


def _echo_mismatch(response: Any, expected: dict[str, Any]) -> dict[str, Any]:
    """Fields of a write reply that do not echo the request (Modbus write replies do).

    `expected` maps response attributes to values; for the lists `registers` and
    `bits` only the leading elements are compared.
    """
    mismatch: dict[str, Any] = {}
    for name, want in expected.items():
        got = getattr(response, name, None)
        if isinstance(want, list):
            got = list(got)[: len(want)] if isinstance(got, (list, tuple)) else got
        if got != want:
            mismatch[name] = {"expected": want, "got": got}
    return mismatch


async def _chunked_read(
    ctx: Context,
    app: AppContext,
    label: str,
    read_func: Callable[[int, int], Awaitable[Any]],
    start_address: int,
    total_count: int,
    per_request_limit: int,
    attr: str,
    function_code: int,
) -> tuple[list[Any] | None, str | None, dict[str, Any]]:
    """Read `total_count` registers or bits in requests of at most `per_request_limit`.

    - read_func: async function(start, size) -> pymodbus response
    - attr: response attribute to extract ("registers" or "bits")
    - function_code: of the read, to check that each reply belongs to its request

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
        check = reply_check(function_code, **{attr: size})
        result, err, duration_ms, attempts = await _retry_call(
            ctx,
            app,
            op,
            functools.partial(read_func, current, size),
            details=details,
            check=check,
        )
        if err is not None:
            return None, err, {"partial": values, "chunks": chunks, **details}
        if _is_error(result):
            message, extra = _device_error(result)
            return None, message, {"partial": values, "chunks": chunks, **extra}
        # trim to the requested size: bit responses are padded to a multiple of 8
        values.extend(list(getattr(result, attr))[:size])
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


def _make_client(settings: Settings, trace_packet: Callable[[bool, bytes], bytes] | None = None):
    # retries=0: on a timeout, pymodbus (3.9-3.15, default retries=3) sends the
    # same request again, which would repeat a write. Reads are retried by
    # _retry_call instead (MODBUS_MAX_RETRIES); writes are never re-sent.
    common: dict[str, Any] = {"retries": 0}
    if trace_packet is not None:
        common["trace_packet"] = trace_packet
    if settings.modbus_type == "tcp":
        return AsyncModbusTcpClient(host=settings.host, port=settings.port, **common)
    if settings.modbus_type == "udp":
        return AsyncModbusUdpClient(host=settings.host, port=settings.port, **common)
    return AsyncModbusSerialClient(
        port=settings.serial_port,
        baudrate=settings.baudrate,
        parity=settings.parity,
        stopbits=settings.stopbits,
        bytesize=settings.bytesize,
        timeout=settings.serial_timeout,
        **common,
    )


@asynccontextmanager
async def app_lifespan(server: FastMCP) -> AsyncIterator[AppContext]:
    """Create the Modbus client and try to connect; an unreachable device is not fatal.

    The server starts either way, so initialize and tools/list work. Until the
    device answers, the device tools return success: false "cannot reach device
    at ..."; every call tries to connect again (see _retry_call/_reconnect).
    """
    settings, tags = _config if _config is not None else load_config()
    gate = SendGate(lambda: getattr(server, "client_gone", False))
    client = _make_client(settings, gate)
    gate.client = client
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


def _describe_validation_error(exc: ValidationError) -> str:
    parts = []
    for err in exc.errors():
        where = ".".join(str(p) for p in err.get("loc", ())) or "arguments"
        part = f"{where}: {err.get('msg', 'invalid')}"
        if err.get("type") != "missing" and "input" in err:
            part += f" (got {err['input']!r})"
        parts.append(part)
    return "; ".join(parts)


# Errors from writing a reply to a client that has gone away.
_UNDELIVERABLE = (
    anyio.ClosedResourceError,
    anyio.BrokenResourceError,
    BrokenPipeError,
    ConnectionResetError,
)


class ModbusMCP(FastMCP):
    """FastMCP that returns the {success, data, error, meta} envelope for every tool failure.

    FastMCP turns invalid arguments (and any exception a tool raises) into a bare
    error string. Clients of IndustriConnect servers parse the envelope, so give
    them one: success=false with the reason in `error`.

    It also bounds every call (MODBUS_CALL_TIMEOUT) and stops pending calls when
    the client disconnects: FastMCP stops reading at end of stdin but lets
    in-flight tool calls finish, so a write still connecting or queued would
    otherwise be sent after the client had gone.
    """

    client_gone: bool = False

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._call_scopes: set[anyio.CancelScope] = set()

    def _call_timeout(self) -> float:
        """MODBUS_CALL_TIMEOUT of the running server (the default outside a session)."""
        try:
            return self.get_context().request_context.lifespan_context.settings.call_timeout
        except Exception:
            return Settings().call_timeout

    def client_disconnected(self) -> None:
        """The client is gone: send nothing more, and cancel every call in progress."""
        if self.client_gone:
            return
        self.client_gone = True
        log.info("MCP client disconnected; stopping %d pending call(s)", len(self._call_scopes))
        for scope in list(self._call_scopes):
            scope.cancel()

    async def run_stdio_async(self) -> None:
        """Serve stdio; when stdin ends, stop pending calls before FastMCP finishes them."""
        self.client_gone = False
        try:
            async with stdio_server() as (read_stream, write_stream):
                relay_send, relay_receive = anyio.create_memory_object_stream(0)
                async with anyio.create_task_group() as tg:
                    tg.start_soon(self._relay_until_eof, read_stream, relay_send)
                    await self._mcp_server.run(
                        relay_receive,
                        write_stream,
                        self._mcp_server.create_initialization_options(),
                    )
                    tg.cancel_scope.cancel()
        except BaseExceptionGroup as group:
            # Replies to calls that were still running cannot be delivered once
            # the client is gone; that is a normal end, not a crash.
            _, rest = group.split(_UNDELIVERABLE)
            if not self.client_gone or rest is not None:
                raise
            log.info("dropped replies for the disconnected client")

    async def _relay_until_eof(self, source: Any, sink: Any) -> None:
        try:
            async with sink:
                async for message in source:
                    await sink.send(message)
        finally:
            self.client_disconnected()

    def _unfinished(self, name: str, state: _CallState, why: str) -> dict[str, Any]:
        """The envelope for a call that ended without a result."""
        meta: dict[str, Any] = {"tool": name}
        error = why
        if name in WRITE_TOOLS:
            if state.sent:
                meta.update(outcome="unknown", request_sent=True)
                error += f"; {MAY_HAVE_BEEN_APPLIED}"
            else:
                meta.update(outcome="not_sent", request_sent=False)
                error += "; the write was not sent"
        return _make_result(False, error=error, meta=meta)

    async def call_tool(self, name: str, arguments: dict[str, Any]) -> Any:
        tool = self._tool_manager.get_tool(name)
        if tool is None:
            return await super().call_tool(name, arguments)  # FastMCP: unknown tool
        state = _CallState.start(self._call_timeout())
        result = await self._run_call(tool, name, arguments, state)
        return result

    async def _run_call(
        self, tool: Any, name: str, arguments: dict[str, Any], state: _CallState
    ) -> Any:
        seconds = state.seconds
        if self.client_gone:
            state.cancelled = True
            envelope = self._unfinished(name, state, "the MCP client disconnected")
            return self._envelope(tool, envelope)
        scope = anyio.CancelScope()
        self._call_scopes.add(scope)
        try:
            with scope:
                return await run_with_deadline(seconds, self._call_tool(name, arguments), state)
        except (TimeoutError, asyncio.TimeoutError):
            # The backstop: something outside _retry_call did not finish in time.
            state.cancelled = True
            why = f"did not finish within the {seconds:g} s deadline (MODBUS_CALL_TIMEOUT)"
            return self._envelope(tool, self._unfinished(name, state, why))
        except BaseException:
            # Cancelled by the client (notifications/cancelled) or anything else that
            # ends the call without a result: whatever it queued must never be sent.
            state.cancelled = True
            raise
        finally:
            self._call_scopes.discard(scope)
        # Only reached when client_disconnected() cancelled this call.
        state.cancelled = True
        envelope = self._unfinished(name, state, "the MCP client disconnected before it finished")
        return self._envelope(tool, envelope)

    @staticmethod
    def _envelope(tool: Any, envelope: dict[str, Any]) -> Any:
        # mcp >= 1.10 also returns structured content; let the tool's own
        # converter shape the envelope exactly like a normal result.
        convert = getattr(getattr(tool, "fn_metadata", None), "convert_result", None)
        if convert is not None:
            return convert(envelope)
        return [TextContent(type="text", text=json.dumps(envelope))]

    async def _call_tool(self, name: str, arguments: dict[str, Any]) -> Any:
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
                    error += f"; {MAY_HAVE_BEEN_APPLIED}"
            return self._envelope(tool, _make_result(False, error=error, meta=meta))


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


@mcp.tool(annotations=READ_ONLY)
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

    op = f"read_register addr={address} slave={slave_id}"
    result, failure = await _request(ctx, app, op, _call, meta, check=reply_check(3, registers=1))
    if failure:
        return failure
    value = result.registers[0]
    await ctx.info(f"Read register {address} from slave {slave_id}: {value}")
    return _make_result(True, data={"value": value}, meta=meta)


@mcp.tool(annotations=DEVICE_WRITE)
async def write_register(
    address: Address, value: RegisterValue, ctx: Context, slave_id: SlaveId = None
) -> dict[str, Any]:
    """Write one holding register (function 6).

    Only works when MODBUS_WRITES_ENABLED=true; otherwise nothing is sent and
    success is false. The write is sent at most once. meta.outcome says what
    happened: written (acknowledged), rejected (refused with an exception that
    means nothing was done), not_sent, or unknown (no reply, a reply that does
    not match, or an exception that does not rule a write out: it may have been
    applied, so check before retrying); meta.request_sent says whether the
    request went out.
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
    check = reply_check(6, echo={"address": address, "registers": [value]})
    _, failure = await _request(ctx, app, op, _call, meta, check=check, write=True)
    if failure:
        return failure
    await ctx.info(f"Wrote {value} to register {address} on slave {slave_id}")
    return _make_result(True, data={"written": value}, meta=meta)


@mcp.tool(annotations=READ_ONLY)
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
        1,
    )
    meta = {"address": address, "count": count, "slave_id": slave_id, **meta}
    if err is not None:
        return _make_result(False, error=err, meta=meta)
    await ctx.info(f"Read {count} coils starting at {address} from slave {slave_id}")
    return _make_result(True, data={"values": values}, meta=meta)


@mcp.tool(annotations=DEVICE_WRITE)
async def write_coil(
    address: Address,
    value: Annotated[bool, Field(strict=True, description="true = ON, false = OFF.")],
    ctx: Context,
    slave_id: SlaveId = None,
) -> dict[str, Any]:
    """Write one coil (function 5).

    Only works when MODBUS_WRITES_ENABLED=true; otherwise nothing is sent and
    success is false. The write is sent at most once. meta.outcome says what
    happened: written (acknowledged), rejected (refused with an exception that
    means nothing was done), not_sent, or unknown (no reply, a reply that does
    not match, or an exception that does not rule a write out: it may have been
    applied, so check before retrying); meta.request_sent says whether the
    request went out.
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
    # pymodbus decodes any non-zero FC 5 echo as ON, so also check the raw reply.
    pdu = struct.pack(">BHH", 5, address, 0xFF00 if value else 0x0000)
    check = reply_check(5, echo={"address": address, "bits": [value]}, raw=(pdu, app.last_reply))
    _, failure = await _request(ctx, app, op, _call, meta, check=check, write=True)
    if failure:
        return failure
    await ctx.info(f"Wrote {value} to coil {address} on slave {slave_id}")
    return _make_result(True, data={"written": value}, meta=meta)


@mcp.tool(annotations=READ_ONLY)
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
        4,
    )
    meta = {"address": address, "count": count, "slave_id": slave_id, **meta}
    if err is not None:
        return _make_result(False, error=err, meta=meta)
    await ctx.info(f"Read {count} input registers starting at {address} from slave {slave_id}")
    return _make_result(True, data={"registers": values}, meta=meta)


@mcp.tool(annotations=READ_ONLY)
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
        3,
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


@mcp.tool(annotations=READ_ONLY)
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
        2,
    )
    meta = {"address": address, "count": count, "slave_id": slave_id, **meta}
    if err is not None:
        return _make_result(False, error=err, meta=meta)
    await ctx.info(f"Read {count} discrete inputs starting at {address} from slave {slave_id}")
    return _make_result(True, data={"values": values}, meta=meta)


@mcp.tool(annotations=DEVICE_WRITE)
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
    happened: written (acknowledged), rejected (refused with an exception that
    means nothing was done), not_sent, or unknown (no reply, a reply that does
    not match, or an exception that does not rule a write out: it may have been
    applied, so check before retrying); meta.request_sent says whether the
    request went out.
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
    check = reply_check(16, echo={"address": address, "count": len(values)})
    _, failure = await _request(ctx, app, op, _call, meta, check=check, write=True)
    if failure:
        return failure
    await ctx.info(f"Wrote {len(values)} registers starting at {address} on slave {slave_id}")
    return _make_result(True, data={"written": len(values)}, meta=meta)


@mcp.tool(annotations=DEVICE_WRITE)
async def write_coils_bulk(
    address: Address,
    values: Annotated[
        list[StrictBool],
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
    happened: written (acknowledged), rejected (refused with an exception that
    means nothing was done), not_sent, or unknown (no reply, a reply that does
    not match, or an exception that does not rule a write out: it may have been
    applied, so check before retrying); meta.request_sent says whether the
    request went out.
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
    check = reply_check(15, echo={"address": address, "count": len(values)})
    _, failure = await _request(ctx, app, op, _call, meta, check=check, write=True)
    if failure:
        return failure
    await ctx.info(f"Wrote {len(values)} coils starting at {address} on slave {slave_id}")
    return _make_result(True, data={"written": len(values)}, meta=meta)


@mcp.tool(annotations=DEVICE_WRITE)
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
    happened: written (acknowledged), rejected (refused with an exception that
    means nothing was done), not_sent, or unknown (no reply, a reply that does
    not match, or an exception that does not rule a write out: it may have been
    applied, so check before retrying); meta.request_sent says whether the
    request went out.
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
    echo = {"address": address, "and_mask": and_mask, "or_mask": or_mask}
    check = reply_check(22, echo=echo)
    _, failure = await _request(ctx, app, op, _call, meta, check=check, write=True)
    if failure:
        return failure
    await ctx.info(f"Mask write register {address} on slave {slave_id}")
    return _make_result(
        True, data={"address": address, "and_mask": and_mask, "or_mask": or_mask}, meta=meta
    )


@mcp.tool(annotations=READ_ONLY)
async def read_device_information(
    ctx: Context,
    slave_id: SlaveId = None,
    read_code: Annotated[
        int,
        Field(
            ge=1,
            le=4,
            strict=True,
            description="1 = basic, 2 = regular, 3 = extended, 4 = one object.",
        ),
    ] = 0x03,
    object_id: Annotated[
        int,
        Field(ge=0, le=255, strict=True, description="First object to read (0 = VendorName)."),
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
    result, failure = await _request(ctx, app, op, _call, meta, check=reply_check(0x2B))
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
        3 if table == "holding" else 4,
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


@mcp.tool(annotations=READ_ONLY)
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


@mcp.tool(annotations=READ_ONLY)
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


@mcp.tool(annotations=READ_ONLY)
async def list_tags(ctx: Context) -> dict[str, Any]:
    """List the named tags defined in the tag map (REGISTER_MAP_FILE), with their definitions."""
    app = _app(ctx)
    tags = [tag.as_dict() for _, tag in sorted(app.tags.items())]
    return _make_result(True, data={"tags": tags})


def _unknown_tag(name: str) -> dict[str, Any]:
    return _make_result(
        False, error=f"Unknown tag {name!r}; list_tags shows the defined tags", meta={"tag": name}
    )


@mcp.tool(annotations=READ_ONLY)
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


@mcp.tool(annotations=DEVICE_WRITE)
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
        if not isinstance(value, bool):
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


@mcp.tool(annotations=READ_ONLY)
async def ping(
    ctx: Context,
    slave_id: SlaveId = None,
    address: Annotated[
        int,
        Field(ge=0, le=65535, strict=True, description="Holding register to probe (default 0)."),
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
        ctx, app, "ping", _probe, max_retries=0, details=details, check=reply_check(3, registers=1)
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
