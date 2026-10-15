from typing import Annotated, Any

from pydantic import Field

from ..adapters.transport import _retry_call, _unit
from ..application.request import _request
from ..domain.model import SlaveId, _device_error, _is_error, _make_result, _slave, reply_check


async def read_device_information(
    ctx: Any,
    slave_id: SlaveId = None,
    read_code: Annotated[
        int,
        Field(
            ge=1,
            le=4,
            strict=True,
            description="1 = basic, 2 = regular, 3 = extended, 4 = one object.",
        ),
    ] = 3,
    object_id: Annotated[
        int, Field(ge=0, le=255, strict=True, description="First object to read (0 = VendorName).")
    ] = 0,
) -> dict[str, Any]:
    """Read the device identification objects (function 43 / MEI type 14)."""
    app = ctx.app
    client = app.modbus_client
    slave_id = _slave(app, slave_id)
    meta: dict[str, Any] = {"slave_id": slave_id, "read_code": read_code, "object_id": object_id}

    async def _call():
        return await client.read_device_information(
            read_code=read_code, object_id=object_id, **_unit(client, slave_id)
        )

    op = f"read_device_information slave={slave_id} code={read_code} obj={object_id}"
    result, failure = await _request(ctx, app, op, _call, meta, check=reply_check(43))
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


async def list_tags(ctx: Any) -> dict[str, Any]:
    """List the named tags defined in the tag map (REGISTER_MAP_FILE), with their definitions."""
    app = ctx.app
    tags = [tag.as_dict() for _, tag in sorted(app.tags.items())]
    return _make_result(True, data={"tags": tags})


async def ping(
    ctx: Any,
    slave_id: SlaveId = None,
    address: Annotated[
        int,
        Field(ge=0, le=65535, strict=True, description="Holding register to probe (default 0)."),
    ] = 0,
) -> dict[str, Any]:
    """Check that the device answers, and report the server's configuration."""
    app = ctx.app
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
