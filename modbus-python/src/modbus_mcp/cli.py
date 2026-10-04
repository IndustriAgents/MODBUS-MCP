"""Modbus MCP server: Modbus TCP/UDP/serial devices as MCP tools over stdio.

stdout carries the MCP JSON-RPC stream, so nothing in this module may print;
diagnostics go to stderr (logging) or to the client as MCP log messages.
"""

# No `from __future__ import annotations` here: FastMCP 1.6 finds the Context
# parameter by comparing the annotation object itself (`is Context`).
import asyncio
import functools
import inspect
import math
import sys
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import Any

from dotenv import load_dotenv
from mcp.server.fastmcp import Context, FastMCP
from mcp.server.fastmcp.prompts import base
from pymodbus.client import AsyncModbusSerialClient, AsyncModbusTcpClient, AsyncModbusUdpClient
from pymodbus.exceptions import ModbusException

from . import codec
from .config import ConfigError, Settings, Tag, load_tag_map

WRITES_DISABLED = (
    "Writes are disabled by configuration (set MODBUS_WRITES_ENABLED=true to allow them)"
)


@dataclass
class AppContext:
    """Per-server state, created by the lifespan once the device is connected."""

    modbus_client: Any
    settings: Settings = field(default_factory=Settings)
    tags: dict[str, Tag] = field(default_factory=dict)


# -----------------------------
# Configuration
# -----------------------------

# Set by main(); the lifespan falls back to load_config() when the server is
# started some other way (e.g. `mcp dev`).
_config: tuple[Settings, dict[str, Tag]] | None = None


def load_config() -> tuple[Settings, dict[str, Tag]]:
    """Load `.env` (it never overrides variables already set), then read the settings."""
    load_dotenv()
    settings = Settings.from_env()
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


def _app(ctx: Context) -> AppContext:
    return ctx.request_context.lifespan_context


def _slave(app: AppContext, slave_id: int | None) -> int:
    return app.settings.default_slave_id if slave_id is None else slave_id


async def _with_timeout(coro: Awaitable[Any], timeout: float | None) -> Any:
    if timeout is None or timeout <= 0:
        return await coro
    return await asyncio.wait_for(coro, timeout=timeout)


async def _retry_call(
    ctx: Context,
    app: AppContext,
    op: str,
    call_factory: Callable[[], Awaitable[Any]],
    max_retries: int | None = None,
) -> tuple[Any | None, str | None, float, int]:
    """Execute call with retries and timeout. Returns (result, error, duration, attempts).

    A connection error triggers a reconnect attempt before the next retry, making the
    server resilient to transient TCP drops.
    """
    settings = app.settings
    client = app.modbus_client
    if max_retries is None:
        max_retries = settings.max_retries
    attempt = 0
    start = time.perf_counter()
    last_err: str | None = None
    while True:
        try:
            attempt += 1
            result = await _with_timeout(call_factory(), settings.tool_timeout)
            return result, None, (time.perf_counter() - start) * 1000.0, attempt
        except (ModbusException, asyncio.TimeoutError) as e:
            last_err = f"{type(e).__name__}: {e}"
            await ctx.error(f"{op} failed on attempt {attempt}: {last_err}")
            if attempt > max_retries:
                break
            backoff = settings.retry_backoff_base * (2 ** (attempt - 1))
            await asyncio.sleep(backoff)
        except (ConnectionError, OSError) as e:
            # TCP connection dropped — attempt reconnect before retrying
            last_err = f"{type(e).__name__}: {e}"
            await ctx.error(f"{op} connection error on attempt {attempt}: {last_err}")
            if attempt > max_retries:
                break
            try:
                await ctx.info(f"{op} attempting reconnect before retry {attempt + 1}")
                await client.connect()
            except Exception as reconnect_err:
                await ctx.error(f"{op} reconnect failed: {reconnect_err}")
            backoff = settings.retry_backoff_base * (2 ** (attempt - 1))
            await asyncio.sleep(backoff)
        except Exception as e:  # unexpected
            last_err = f"Unexpected {type(e).__name__}: {e}"
            await ctx.error(f"{op} unexpected error on attempt {attempt}: {last_err}")
            break
    return None, last_err, (time.perf_counter() - start) * 1000.0, attempt


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
    """
    if total_count <= 0:
        return None, "Count must be positive", {}

    values: list[Any] = []
    chunks: list[dict[str, Any]] = []
    remaining = total_count
    current = start_address
    while remaining > 0:
        size = min(remaining, per_request_limit)
        op = f"{label}[{current}:{current + size - 1}]"
        result, err, duration_ms, attempts = await _retry_call(
            ctx, app, op, functools.partial(read_func, current, size)
        )
        if err is not None:
            return None, err, {"partial": values, "chunks": chunks}
        if hasattr(result, "isError") and result.isError():
            return None, f"Device error: {result}", {"partial": values, "chunks": chunks}
        chunk_vals = getattr(result, attr, None)
        if chunk_vals is None:
            return None, "Malformed response", {"partial": values, "chunks": chunks}
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
@functools.lru_cache(maxsize=None)
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
    """JSON has no NaN or infinity; spell a non-finite float out instead of emitting invalid JSON."""
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
    if settings.modbus_type == "tcp":
        return AsyncModbusTcpClient(host=settings.host, port=settings.port)
    if settings.modbus_type == "udp":
        return AsyncModbusUdpClient(host=settings.host, port=settings.port)
    return AsyncModbusSerialClient(
        port=settings.serial_port,
        baudrate=settings.baudrate,
        parity=settings.parity,
        stopbits=settings.stopbits,
        bytesize=settings.bytesize,
        timeout=settings.serial_timeout,
    )


@asynccontextmanager
async def app_lifespan(server: FastMCP) -> AsyncIterator[AppContext]:
    """Connect to the configured device for the lifetime of the server."""
    settings, tags = _config if _config is not None else load_config()
    client = _make_client(settings)
    await client.connect()
    if not getattr(client, "connected", False):
        client.close()
        raise RuntimeError(
            f"Failed to connect to Modbus {settings.modbus_type} device at {settings.target}"
        )
    try:
        yield AppContext(modbus_client=client, settings=settings, tags=tags)
    finally:
        client.close()


mcp = FastMCP(name="Modbus MCP Server", dependencies=["pymodbus"], lifespan=app_lifespan)


# -----------------------------
# Holding registers and coils
# -----------------------------


@mcp.tool()
async def read_register(address: int, ctx: Context, slave_id: int | None = None) -> dict[str, Any]:
    """
    Read a single Modbus holding register.

    Parameters:
        address (int): The address of the holding register (0-65535).
        slave_id (int): The Modbus slave ID (device ID); defaults to MODBUS_DEFAULT_SLAVE_ID.
    """
    app = _app(ctx)
    client = app.modbus_client
    slave_id = _slave(app, slave_id)
    op = f"read_register addr={address} slave={slave_id}"

    async def _call():
        return await client.read_holding_registers(
            address=address, count=1, **_unit(client, slave_id)
        )

    result, err, duration_ms, attempts = await _retry_call(ctx, app, op, _call)
    meta = {"address": address, "slave_id": slave_id}
    if err is not None:
        meta.update(duration_ms=round(duration_ms, 3), attempts=attempts)
        return _make_result(False, error=err, meta=meta)
    if hasattr(result, "isError") and result.isError():
        return _make_result(False, error=str(result), meta=meta)
    value = result.registers[0]
    await ctx.info(f"Read register {address} from slave {slave_id}: {value}")
    meta.update(duration_ms=round(duration_ms, 3), attempts=attempts)
    return _make_result(True, data={"value": value}, meta=meta)


@mcp.tool()
async def write_register(
    address: int, value: int, ctx: Context, slave_id: int | None = None
) -> dict[str, Any]:
    """
    Write a value to a Modbus holding register.

    Parameters:
        address (int): The address of the holding register (0-65535).
        value (int): The value to write (0-65535).
        slave_id (int): The Modbus slave ID (device ID); defaults to MODBUS_DEFAULT_SLAVE_ID.
    """
    app = _app(ctx)
    slave_id = _slave(app, slave_id)
    if not app.settings.writes_enabled:
        return _make_result(
            False, error=WRITES_DISABLED, meta={"address": address, "slave_id": slave_id}
        )
    if not (0 <= value <= 65535):
        return _make_result(
            False,
            error=f"Value {value} out of uint16 range (0-65535)",
            meta={"address": address, "value": value},
        )
    client = app.modbus_client
    op = f"write_register addr={address} value={value} slave={slave_id}"

    async def _call():
        return await client.write_register(address=address, value=value, **_unit(client, slave_id))

    result, err, duration_ms, attempts = await _retry_call(ctx, app, op, _call)
    meta = {"address": address, "value": value, "slave_id": slave_id}
    if err is not None:
        meta.update(duration_ms=round(duration_ms, 3), attempts=attempts)
        return _make_result(False, error=err, meta=meta)
    if hasattr(result, "isError") and result.isError():
        return _make_result(False, error=str(result), meta=meta)
    await ctx.info(f"Wrote {value} to register {address} on slave {slave_id}")
    return _make_result(
        True,
        data={"written": value},
        meta={
            "address": address,
            "slave_id": slave_id,
            "duration_ms": round(duration_ms, 3),
            "attempts": attempts,
        },
    )


@mcp.tool()
async def read_coils(
    address: int, count: int, ctx: Context, slave_id: int | None = None
) -> dict[str, Any]:
    """
    Read the status of multiple Modbus coils.

    Parameters:
        address (int): The starting address of the coils (0-65535).
        count (int): The number of coils to read; split into requests of at most 2000.
        slave_id (int): The Modbus slave ID (device ID); defaults to MODBUS_DEFAULT_SLAVE_ID.
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
        2000,
        "bits",
    )
    meta = {"address": address, "count": count, "slave_id": slave_id, **meta}
    if err is not None:
        return _make_result(False, error=err, meta=meta)
    await ctx.info(f"Read {count} coils starting at {address} from slave {slave_id}")
    return _make_result(True, data={"values": values}, meta=meta)


@mcp.tool()
async def write_coil(
    address: int, value: bool, ctx: Context, slave_id: int | None = None
) -> dict[str, Any]:
    """
    Write a value to a single Modbus coil.

    Parameters:
        address (int): The address of the coil (0-65535).
        value (bool): The value to write (True for ON, False for OFF).
        slave_id (int): The Modbus slave ID (device ID); defaults to MODBUS_DEFAULT_SLAVE_ID.
    """
    app = _app(ctx)
    slave_id = _slave(app, slave_id)
    if not app.settings.writes_enabled:
        return _make_result(
            False, error=WRITES_DISABLED, meta={"address": address, "slave_id": slave_id}
        )
    client = app.modbus_client
    op = f"write_coil addr={address} value={value} slave={slave_id}"

    async def _call():
        return await client.write_coil(address=address, value=value, **_unit(client, slave_id))

    result, err, duration_ms, attempts = await _retry_call(ctx, app, op, _call)
    meta = {"address": address, "value": value, "slave_id": slave_id}
    if err is not None:
        meta.update(duration_ms=round(duration_ms, 3), attempts=attempts)
        return _make_result(False, error=err, meta=meta)
    if hasattr(result, "isError") and result.isError():
        return _make_result(False, error=str(result), meta=meta)
    await ctx.info(f"Wrote {value} to coil {address} on slave {slave_id}")
    return _make_result(
        True,
        data={"written": value},
        meta={
            "address": address,
            "slave_id": slave_id,
            "duration_ms": round(duration_ms, 3),
            "attempts": attempts,
        },
    )


@mcp.tool()
async def read_input_registers(
    address: int, count: int, ctx: Context, slave_id: int | None = None
) -> dict[str, Any]:
    """
    Read multiple Modbus input registers.

    Parameters:
        address (int): The starting address of the input registers (0-65535).
        count (int): The number of registers to read; split into requests of at most 125.
        slave_id (int): The Modbus slave ID (device ID); defaults to MODBUS_DEFAULT_SLAVE_ID.
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
        125,
        "registers",
    )
    meta = {"address": address, "count": count, "slave_id": slave_id, **meta}
    if err is not None:
        return _make_result(False, error=err, meta=meta)
    await ctx.info(f"Read {count} input registers starting at {address} from slave {slave_id}")
    return _make_result(True, data={"registers": values}, meta=meta)


@mcp.tool()
async def read_multiple_holding_registers(
    address: int, count: int, ctx: Context, slave_id: int | None = None
) -> dict[str, Any]:
    """
    Read multiple Modbus holding registers.

    Parameters:
        address (int): The starting address of the holding registers (0-65535).
        count (int): The number of registers to read; split into requests of at most 125.
        slave_id (int): The Modbus slave ID (device ID); defaults to MODBUS_DEFAULT_SLAVE_ID.
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
        125,
        "registers",
    )
    meta = {"address": address, "count": count, "slave_id": slave_id, **meta}
    if err is not None:
        return _make_result(False, error=err, meta=meta)
    await ctx.info(f"Read {count} holding registers starting at {address} from slave {slave_id}")
    return _make_result(True, data={"registers": values}, meta=meta)


# Prompts: Templates for Modbus interactions
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


def main() -> None:
    """Entry point of `modbus-mcp`: read the configuration, then serve MCP over stdio."""
    global _config
    try:
        _config = load_config()
    except ConfigError as exc:
        sys.stderr.write(f"modbus-mcp: configuration error: {exc}\n")
        raise SystemExit(2) from None
    mcp.run()


# -----------------------------
# Additional protocol coverage tools
# -----------------------------


@mcp.tool()
async def read_discrete_inputs(
    address: int, count: int, ctx: Context, slave_id: int | None = None
) -> dict[str, Any]:
    """
    Read multiple Modbus discrete inputs (function 2).

    Parameters:
        address (int): Starting address (0-65535)
        count (int): Number of inputs to read (1+)
        slave_id (int): Device ID; defaults to MODBUS_DEFAULT_SLAVE_ID.
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
        2000,
        "bits",
    )
    meta = {"address": address, "count": count, "slave_id": slave_id, **meta}
    if err is not None:
        return _make_result(False, error=err, meta=meta)
    await ctx.info(f"Read {count} discrete inputs starting at {address} from slave {slave_id}")
    return _make_result(True, data={"values": values}, meta=meta)


@mcp.tool()
async def write_registers(
    address: int, values: list[int], ctx: Context, slave_id: int | None = None
) -> dict[str, Any]:
    """
    Write multiple holding registers (function 16).

    Parameters:
        address (int): Starting address
        values (List[int]): Register values
        slave_id (int): Device ID; defaults to MODBUS_DEFAULT_SLAVE_ID.
    """
    app = _app(ctx)
    slave_id = _slave(app, slave_id)
    if not app.settings.writes_enabled:
        return _make_result(
            False, error=WRITES_DISABLED, meta={"address": address, "slave_id": slave_id}
        )
    if not values:
        return _make_result(False, error="Values list must not be empty")
    for i, v in enumerate(values):
        if not (0 <= v <= 65535):
            return _make_result(
                False,
                error=f"Value {v} at index {i} out of uint16 range (0-65535)",
                meta={"address": address, "index": i, "value": v},
            )
    client = app.modbus_client
    op = f"write_registers addr={address} n={len(values)} slave={slave_id}"

    async def _call():
        return await client.write_registers(
            address=address, values=values, **_unit(client, slave_id)
        )

    result, err, duration_ms, attempts = await _retry_call(ctx, app, op, _call)
    meta = {"address": address, "count": len(values), "slave_id": slave_id}
    if err is not None:
        meta.update(duration_ms=round(duration_ms, 3), attempts=attempts)
        return _make_result(False, error=err, meta=meta)
    if hasattr(result, "isError") and result.isError():
        return _make_result(False, error=str(result), meta=meta)
    await ctx.info(f"Wrote {len(values)} registers starting at {address} on slave {slave_id}")
    return _make_result(
        True,
        data={"written": len(values)},
        meta={
            "address": address,
            "slave_id": slave_id,
            "duration_ms": round(duration_ms, 3),
            "attempts": attempts,
        },
    )


@mcp.tool()
async def write_coils_bulk(
    address: int, values: list[bool], ctx: Context, slave_id: int | None = None
) -> dict[str, Any]:
    """
    Write multiple coils (function 15).
    """
    app = _app(ctx)
    slave_id = _slave(app, slave_id)
    if not app.settings.writes_enabled:
        return _make_result(
            False, error=WRITES_DISABLED, meta={"address": address, "slave_id": slave_id}
        )
    if not values:
        return _make_result(False, error="Values list must not be empty")
    client = app.modbus_client
    op = f"write_coils addr={address} n={len(values)} slave={slave_id}"

    async def _call():
        return await client.write_coils(address=address, values=values, **_unit(client, slave_id))

    result, err, duration_ms, attempts = await _retry_call(ctx, app, op, _call)
    meta = {"address": address, "count": len(values), "slave_id": slave_id}
    if err is not None:
        meta.update(duration_ms=round(duration_ms, 3), attempts=attempts)
        return _make_result(False, error=err, meta=meta)
    if hasattr(result, "isError") and result.isError():
        return _make_result(False, error=str(result), meta=meta)
    await ctx.info(f"Wrote {len(values)} coils starting at {address} on slave {slave_id}")
    return _make_result(
        True,
        data={"written": len(values)},
        meta={
            "address": address,
            "slave_id": slave_id,
            "duration_ms": round(duration_ms, 3),
            "attempts": attempts,
        },
    )


@mcp.tool()
async def mask_write_register(
    address: int, and_mask: int, or_mask: int, ctx: Context, slave_id: int | None = None
) -> dict[str, Any]:
    """
    Mask write register (function 22): (register = (register & and_mask) | (or_mask & ~and_mask)).
    """
    app = _app(ctx)
    slave_id = _slave(app, slave_id)
    if not app.settings.writes_enabled:
        return _make_result(
            False, error=WRITES_DISABLED, meta={"address": address, "slave_id": slave_id}
        )
    client = app.modbus_client
    op = f"mask_write_register addr={address} and={and_mask} or={or_mask} slave={slave_id}"

    async def _call():
        return await client.mask_write_register(
            address=address, and_mask=and_mask, or_mask=or_mask, **_unit(client, slave_id)
        )

    result, err, duration_ms, attempts = await _retry_call(ctx, app, op, _call)
    meta = {"address": address, "and_mask": and_mask, "or_mask": or_mask, "slave_id": slave_id}
    if err is not None:
        meta.update(duration_ms=round(duration_ms, 3), attempts=attempts)
        return _make_result(False, error=err, meta=meta)
    if hasattr(result, "isError") and result.isError():
        return _make_result(False, error=str(result), meta=meta)
    await ctx.info(f"Mask write register {address} on slave {slave_id}")
    return _make_result(
        True,
        data={"address": address, "and_mask": and_mask, "or_mask": or_mask},
        meta={"slave_id": slave_id, "duration_ms": round(duration_ms, 3), "attempts": attempts},
    )


@mcp.tool()
async def read_device_information(
    ctx: Context, slave_id: int | None = None, read_code: int = 0x03, object_id: int = 0x00
) -> dict[str, Any]:
    """
    Read device information (MEI type 0x2B/0x0E). read_code: 0x01=basic, 0x02=regular, 0x03=extended.
    """
    app = _app(ctx)
    client = app.modbus_client
    slave_id = _slave(app, slave_id)
    op = f"read_device_information slave={slave_id} code={read_code} obj={object_id}"

    async def _call():
        return await client.read_device_information(
            read_code=read_code, object_id=object_id, **_unit(client, slave_id)
        )

    result, err, duration_ms, attempts = await _retry_call(ctx, app, op, _call)
    meta = {"slave_id": slave_id, "duration_ms": round(duration_ms, 3), "attempts": attempts}
    if err is not None:
        return _make_result(False, error=err, meta=meta)
    if hasattr(result, "isError") and result.isError():
        return _make_result(False, error=str(result), meta={"slave_id": slave_id})

    # Attempt to normalize fields
    data: dict[str, Any] = {}
    for attr in ("information", "informations", "items", "values"):
        v = getattr(result, attr, None)
        if v:
            data = dict(v)
            break
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
    try:
        size = codec.register_count(dtype)
        byteorder = codec.check_order(byteorder, "byteorder")
        wordorder = codec.check_order(wordorder, "wordorder")
    except codec.CodecError as e:
        return _make_result(False, error=str(e))
    total = size * max(count, 1)
    method = client.read_holding_registers if table == "holding" else client.read_input_registers
    regs, err, meta = await _chunked_read(
        ctx,
        app,
        f"read_{table}_registers",
        lambda start, sz: method(address=start, count=sz, **_unit(client, slave_id)),
        address,
        total,
        125,
        "registers",
    )
    base_meta = {"address": address, "count": count, "dtype": dtype, "slave_id": slave_id}
    if err is not None:
        return _make_result(False, error=err, meta={**base_meta, **meta})
    try:
        vals = _apply_scale(codec.decode(regs, dtype, count, byteorder, wordorder), scale, offset)
    except Exception as e:
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
    address: int,
    dtype: str,
    ctx: Context,
    count: int = 1,
    byteorder: str = "big",
    wordorder: str = "big",
    scale: float = 1.0,
    offset: float = 0.0,
    slave_id: int | None = None,
) -> dict[str, Any]:
    """
    Read holding registers and decode as typed values.
    """
    return await _read_typed(
        ctx, "holding", address, dtype, count, byteorder, wordorder, scale, offset, slave_id
    )


@mcp.tool()
async def read_input_typed(
    address: int,
    dtype: str,
    ctx: Context,
    count: int = 1,
    byteorder: str = "big",
    wordorder: str = "big",
    scale: float = 1.0,
    offset: float = 0.0,
    slave_id: int | None = None,
) -> dict[str, Any]:
    """
    Read input registers and decode as typed values.
    """
    return await _read_typed(
        ctx, "input", address, dtype, count, byteorder, wordorder, scale, offset, slave_id
    )


# -----------------------------
# Tag map tools
# -----------------------------


@mcp.tool()
async def list_tags(ctx: Context) -> dict[str, Any]:
    """List available tags from the register map file."""
    app = _app(ctx)
    tags = [tag.as_dict() for _, tag in sorted(app.tags.items())]
    return _make_result(True, data={"tags": tags})


@mcp.tool()
async def read_tag(name: str, ctx: Context) -> dict[str, Any]:
    """Read a value using the configured tag map (REGISTER_MAP_FILE)."""
    tag = _app(ctx).tags.get(name)
    if tag is None:
        return _make_result(False, error=f"Unknown tag: {name}")
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
    # Fallback to raw reads
    if tag.table == "holding":
        return await read_multiple_holding_registers(tag.address, tag.count, ctx, tag.slave_id)
    if tag.table == "input":
        return await read_input_registers(tag.address, tag.count, ctx, tag.slave_id)
    if tag.table == "coil":
        return await read_coils(tag.address, tag.count, ctx, tag.slave_id)
    return await read_discrete_inputs(tag.address, tag.count, ctx, tag.slave_id)


@mcp.tool()
async def write_tag(
    name: str,
    value: Any,
    ctx: Context,
) -> dict[str, Any]:
    """Write a value using the configured tag map. Only holding regs and coils are writable."""
    app = _app(ctx)
    if not app.settings.writes_enabled:
        return _make_result(False, error=WRITES_DISABLED, meta={"tag": name})
    tag = app.tags.get(name)
    if tag is None:
        return _make_result(False, error=f"Unknown tag: {name}")
    if tag.table == "coil":
        return await write_coil(tag.address, bool(value), ctx, tag.slave_id)
    if tag.table == "holding":
        # value could be scalar or list
        vals = value if isinstance(value, list) else [value]
        try:
            regs = codec.encode(vals, tag.dtype or "uint16", tag.byteorder, tag.wordorder)
        except Exception as e:
            return _make_result(False, error=str(e))
        return await write_registers(tag.address, regs, ctx, tag.slave_id)
    return _make_result(False, error=f"Unsupported/writable table for tag '{name}': {tag.table}")


# -----------------------------
# Health
# -----------------------------


@mcp.tool()
async def ping(ctx: Context, slave_id: int | None = None) -> dict[str, Any]:
    """Return server health and verify end-to-end device reachability.

    Unlike a socket-level check, this sends a real Modbus request (read 1 holding
    register at address 0) so it catches cases where the TCP session is open but
    the device is unresponsive.
    """
    app = _app(ctx)
    settings = app.settings
    client = app.modbus_client
    slave_id = _slave(app, slave_id)
    socket_connected = bool(getattr(client, "connected", False))

    device_reachable = False
    probe_error: str | None = None

    async def _probe():
        return await client.read_holding_registers(address=0, count=1, **_unit(client, slave_id))

    probe_result, probe_err, probe_ms, _ = await _retry_call(
        ctx, app, "ping_probe", _probe, max_retries=0
    )
    probe_latency_ms = round(probe_ms, 3)
    if probe_err is not None:
        probe_error = probe_err
    elif hasattr(probe_result, "isError") and probe_result.isError():
        probe_error = str(probe_result)
    else:
        device_reachable = True

    network = settings.modbus_type in {"tcp", "udp"}
    status = {
        "socket_connected": socket_connected,
        "device_reachable": device_reachable,
        "probe_latency_ms": probe_latency_ms,
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
    return _make_result(
        device_reachable, data=status, error=probe_error if not device_reachable else None
    )
