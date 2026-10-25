import functools
from collections.abc import Awaitable, Callable
from typing import Annotated, Any

from pydantic import Field

from .. import codec
from ..application.request import _request
from ..domain.model import (
    MAX_READ_BITS,
    MAX_READ_REGISTERS,
    Address,
    BitCount,
    ByteOrder,
    DType,
    Offset,
    RegisterCount,
    Scale,
    SlaveId,
    TypedCount,
    WordOrder,
    _apply_scale,
    _device_error,
    _is_error,
    _make_result,
    _slave,
    _span_error,
    _unknown_tag,
    reply_check,
)
from ..state import AppContext


async def _chunked_read(
    ctx: Any,
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
        return (None, "Count must be positive", {})
    span_error = _span_error(start_address, total_count)
    if span_error:
        return (None, span_error, {})
    values: list[Any] = []
    chunks: list[dict[str, Any]] = []
    remaining = total_count
    current = start_address
    while remaining > 0:
        size = min(remaining, per_request_limit)
        op = f"{label}[{current}:{current + size - 1}]"
        details: dict[str, Any] = {}
        check = reply_check(function_code, **{attr: size})
        result, err, duration_ms, attempts = await ctx.transport.request(
            ctx, app, op, functools.partial(read_func, current, size), details=details, check=check
        )
        if err is not None:
            return (None, err, {"partial": values, "chunks": chunks, **details})
        if _is_error(result):
            message, extra = _device_error(result)
            return (None, message, {"partial": values, "chunks": chunks, **extra})
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
    return (values, None, {"chunks": chunks})


async def read_register(address: Address, ctx: Any, slave_id: SlaveId = None) -> dict[str, Any]:
    """Read one holding register (function 3); data.value is its raw 16-bit value."""
    app = ctx.app
    client = app.modbus_client
    slave_id = _slave(app, slave_id)
    meta: dict[str, Any] = {"address": address, "slave_id": slave_id}

    async def _call():
        return await client.read_holding_registers(
            address=address, count=1, **ctx.transport.unit(client, slave_id)
        )

    op = f"read_register addr={address} slave={slave_id}"
    result, failure = await _request(ctx, app, op, _call, meta, check=reply_check(3, registers=1))
    if failure:
        return failure
    value = result.registers[0]
    await ctx.info(f"Read register {address} from slave {slave_id}")
    return _make_result(True, data={"value": value}, meta=meta)


async def read_coils(
    address: Address, count: BitCount, ctx: Any, slave_id: SlaveId = None
) -> dict[str, Any]:
    """Read coils (function 1); data.values is a list of booleans."""
    app = ctx.app
    client = app.modbus_client
    slave_id = _slave(app, slave_id)
    values, err, meta = await _chunked_read(
        ctx,
        app,
        "read_coils",
        lambda start, size: client.read_coils(
            address=start, count=size, **ctx.transport.unit(client, slave_id)
        ),
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


async def read_input_registers(
    address: Address, count: RegisterCount, ctx: Any, slave_id: SlaveId = None
) -> dict[str, Any]:
    """Read input registers (function 4); data.registers is a list of raw 16-bit values."""
    app = ctx.app
    client = app.modbus_client
    slave_id = _slave(app, slave_id)
    values, err, meta = await _chunked_read(
        ctx,
        app,
        "read_input_registers",
        lambda start, size: client.read_input_registers(
            address=start, count=size, **ctx.transport.unit(client, slave_id)
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


async def read_multiple_holding_registers(
    address: Address, count: RegisterCount, ctx: Any, slave_id: SlaveId = None
) -> dict[str, Any]:
    """Read holding registers (function 3); data.registers is a list of raw 16-bit values."""
    app = ctx.app
    client = app.modbus_client
    slave_id = _slave(app, slave_id)
    values, err, meta = await _chunked_read(
        ctx,
        app,
        "read_holding_registers",
        lambda start, size: client.read_holding_registers(
            address=start, count=size, **ctx.transport.unit(client, slave_id)
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


async def read_discrete_inputs(
    address: Address, count: BitCount, ctx: Any, slave_id: SlaveId = None
) -> dict[str, Any]:
    """Read discrete inputs (function 2); data.values is a list of booleans."""
    app = ctx.app
    client = app.modbus_client
    slave_id = _slave(app, slave_id)
    values, err, meta = await _chunked_read(
        ctx,
        app,
        "read_discrete_inputs",
        lambda start, size: client.read_discrete_inputs(
            address=start, count=size, **ctx.transport.unit(client, slave_id)
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


async def _read_typed(
    ctx: Any,
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
    app = ctx.app
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
        lambda start, sz: method(address=start, count=sz, **ctx.transport.unit(client, slave_id)),
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


async def read_holding_typed(
    address: Address,
    dtype: DType,
    ctx: Any,
    count: TypedCount = 1,
    byteorder: ByteOrder = "big",
    wordorder: WordOrder = "big",
    scale: Scale = 1.0,
    offset: Offset = 0.0,
    slave_id: SlaveId = None,
) -> dict[str, Any]:
    """Read holding registers and decode them as typed values."""
    return await _read_typed(
        ctx, "holding", address, dtype, count, byteorder, wordorder, scale, offset, slave_id
    )


async def read_input_typed(
    address: Address,
    dtype: DType,
    ctx: Any,
    count: TypedCount = 1,
    byteorder: ByteOrder = "big",
    wordorder: WordOrder = "big",
    scale: Scale = 1.0,
    offset: Offset = 0.0,
    slave_id: SlaveId = None,
) -> dict[str, Any]:
    """Read input registers and decode them as typed values."""
    return await _read_typed(
        ctx, "input", address, dtype, count, byteorder, wordorder, scale, offset, slave_id
    )


async def read_tag(
    name: Annotated[str, Field(description="Tag name from list_tags.")], ctx: Any
) -> dict[str, Any]:
    """Read a named tag from the tag map."""
    tag = ctx.app.tags.get(name)
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
