import struct
from typing import Annotated, Any

from pydantic import Field

from .. import codec
from ..application.request import _request
from ..domain.model import (
    MAX_WRITE_COILS,
    MAX_WRITE_REGISTERS,
    WRITES_DISABLED,
    Address,
    Mask,
    RegisterValue,
    SlaveId,
    StrictBool,
    _make_result,
    _not_sent,
    _slave,
    _span_error,
    _unknown_tag,
    _unscale,
    reply_check,
)


async def write_register(
    address: Address, value: RegisterValue, ctx: Any, slave_id: SlaveId = None
) -> dict[str, Any]:
    """Write one holding register (function 6)."""
    app = ctx.app
    slave_id = _slave(app, slave_id)
    meta: dict[str, Any] = {"address": address, "value": value, "slave_id": slave_id}
    if not app.settings.writes_enabled:
        return _not_sent(WRITES_DISABLED, meta=meta)
    if not 0 <= value <= 65535:
        return _not_sent(f"Value {value} out of uint16 range (0-65535)", meta=meta)
    client = app.modbus_client

    async def _call():
        return await client.write_register(
            address=address, value=value, **ctx.transport.unit(client, slave_id)
        )

    op = f"write_register addr={address} slave={slave_id}"
    check = reply_check(6, echo={"address": address, "registers": [value]})
    _, failure = await _request(ctx, app, op, _call, meta, check=check, write=True)
    if failure:
        return failure
    await ctx.info(f"Wrote register {address} on slave {slave_id}")
    return _make_result(True, data={"written": value}, meta=meta)


async def write_coil(
    address: Address,
    value: Annotated[bool, Field(strict=True, description="true = ON, false = OFF.")],
    ctx: Any,
    slave_id: SlaveId = None,
) -> dict[str, Any]:
    """Write one coil (function 5)."""
    app = ctx.app
    slave_id = _slave(app, slave_id)
    meta: dict[str, Any] = {"address": address, "value": value, "slave_id": slave_id}
    if not app.settings.writes_enabled:
        return _not_sent(WRITES_DISABLED, meta=meta)
    client = app.modbus_client

    async def _call():
        return await client.write_coil(
            address=address, value=value, **ctx.transport.unit(client, slave_id)
        )

    op = f"write_coil addr={address} slave={slave_id}"
    pdu = struct.pack(">BHH", 5, address, 65280 if value else 0)
    check = reply_check(5, echo={"address": address, "bits": [value]}, raw=(pdu, app.last_reply))
    _, failure = await _request(ctx, app, op, _call, meta, check=check, write=True)
    if failure:
        return failure
    await ctx.info(f"Wrote coil {address} on slave {slave_id}")
    return _make_result(True, data={"written": value}, meta=meta)


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
    ctx: Any,
    slave_id: SlaveId = None,
) -> dict[str, Any]:
    """Write consecutive holding registers in one request (function 16)."""
    app = ctx.app
    slave_id = _slave(app, slave_id)
    meta: dict[str, Any] = {"address": address, "count": len(values), "slave_id": slave_id}
    if not app.settings.writes_enabled:
        return _not_sent(WRITES_DISABLED, meta=meta)
    if not values or len(values) > MAX_WRITE_REGISTERS:
        return _not_sent(f"Write 1 to {MAX_WRITE_REGISTERS} registers per request", meta=meta)
    for i, v in enumerate(values):
        if not 0 <= v <= 65535:
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
            address=address, values=values, **ctx.transport.unit(client, slave_id)
        )

    op = f"write_registers addr={address} n={len(values)} slave={slave_id}"
    check = reply_check(16, echo={"address": address, "count": len(values)})
    _, failure = await _request(ctx, app, op, _call, meta, check=check, write=True)
    if failure:
        return failure
    await ctx.info(f"Wrote {len(values)} registers starting at {address} on slave {slave_id}")
    return _make_result(True, data={"written": len(values)}, meta=meta)


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
    ctx: Any,
    slave_id: SlaveId = None,
) -> dict[str, Any]:
    """Write consecutive coils in one request (function 15)."""
    app = ctx.app
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
        return await client.write_coils(
            address=address, values=values, **ctx.transport.unit(client, slave_id)
        )

    op = f"write_coils addr={address} n={len(values)} slave={slave_id}"
    check = reply_check(15, echo={"address": address, "count": len(values)})
    _, failure = await _request(ctx, app, op, _call, meta, check=check, write=True)
    if failure:
        return failure
    await ctx.info(f"Wrote {len(values)} coils starting at {address} on slave {slave_id}")
    return _make_result(True, data={"written": len(values)}, meta=meta)


async def mask_write_register(
    address: Address, and_mask: Mask, or_mask: Mask, ctx: Any, slave_id: SlaveId = None
) -> dict[str, Any]:
    """Change bits of one holding register in place (function 22)."""
    app = ctx.app
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
            address=address,
            and_mask=and_mask,
            or_mask=or_mask,
            **ctx.transport.unit(client, slave_id),
        )

    op = f"mask_write_register addr={address} slave={slave_id}"
    echo = {"address": address, "and_mask": and_mask, "or_mask": or_mask}
    check = reply_check(22, echo=echo)
    _, failure = await _request(ctx, app, op, _call, meta, check=check, write=True)
    if failure:
        return failure
    await ctx.info(f"Mask write register {address} on slave {slave_id}")
    return _make_result(
        True, data={"address": address, "and_mask": and_mask, "or_mask": or_mask}, meta=meta
    )


async def write_tag(
    name: Annotated[str, Field(description="Tag name from list_tags.")],
    value: Annotated[
        Any,
        Field(
            description=(
                "Coil tags: true or false. Holding-register tags:"
                " a number, or a list of `count` numbers for a "
                "tag with count > 1."
            )
        ),
    ],
    ctx: Any,
) -> dict[str, Any]:
    """Write a named holding-register or coil tag from the tag map."""
    app = ctx.app
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
        return _not_sent(f"Tag {name!r} holds {tag.count} value(s); got {len(vals)}", meta=meta)
    try:
        raw = _unscale(vals, tag)
        regs = codec.encode(raw, tag.dtype or "uint16", tag.byteorder, tag.wordorder)
    except codec.CodecError as e:
        return _not_sent(f"Tag {name!r}: {e}", meta=meta)
    result = await write_registers(tag.address, regs, ctx, tag.slave_id)
    result["meta"].update(tag=name, raw_values=raw)
    return result


async def readwrite_registers(
    read_address: Address,
    read_count: int,
    write_address: Address,
    values: list[int],
    ctx: Any,
    slave_id: SlaveId = None,
) -> dict[str, Any]:
    """FC23: one write-before-read transaction; never split or replay a sent write."""
    app = ctx.app
    slave_id = _slave(app, slave_id)
    meta = {
        "read_address": read_address,
        "read_count": read_count,
        "write_address": write_address,
        "slave_id": slave_id,
        "function_code": 23,
        "operation_order": "write_then_read",
    }
    if not app.settings.writes_enabled:
        return _not_sent(WRITES_DISABLED, meta=meta)
    if app.settings.modbus_type == "serial" and not 1 <= slave_id <= 247:
        return _not_sent(
            "FC23 requires serial unicast unit ID 1..247; "
            "broadcast/reserved addresses cannot return read data",
            meta=meta,
        )
    for name, value, lo, hi in (
        ("read_address", read_address, 0, 65535),
        ("write_address", write_address, 0, 65535),
        ("read_count", read_count, 1, 125),
    ):
        if type(value) is not int or not lo <= value <= hi:
            return _not_sent(f"{name} must be an integer in {lo}..{hi}", meta=meta)
    if not isinstance(values, list) or not 1 <= len(values) <= 121:
        return _not_sent("values must contain 1..121 registers", meta=meta)
    if any(type(value) is not int or not 0 <= value <= 65535 for value in values):
        return _not_sent("values must contain uint16 integers", meta=meta)
    for address, count in ((read_address, read_count), (write_address, len(values))):
        if error := _span_error(address, count):
            return _not_sent(error, meta=meta)
    client = app.modbus_client

    async def _call():
        return await client.readwrite_registers(
            read_address=read_address,
            read_count=read_count,
            write_address=write_address,
            values=values,
            **ctx.transport.unit(client, slave_id),
        )

    response, failure = await _request(
        ctx,
        app,
        f"readwrite_registers slave={slave_id}",
        _call,
        meta,
        check=reply_check(23, registers=read_count),
        write=True,
    )
    if failure:
        return failure
    return _make_result(
        True, data={"written": len(values), "registers": response.registers}, meta=meta
    )
