from typing import Annotated, Any

from pydantic import Field

from ..domain.capabilities import capability_report
from ..domain.model import SlaveId, _device_error, _is_error, _make_result, _slave, reply_check
from .identity import read_device_information as read_device_information


async def list_tags(ctx: Any) -> dict[str, Any]:
    """List the named tags defined in the tag map (REGISTER_MAP_FILE), with their definitions."""
    app = ctx.app
    tags = [tag.as_dict() for _, tag in sorted(app.tags.items())]
    return _make_result(True, data={"tags": tags})


async def get_connection_status(ctx: Any) -> dict[str, Any]:
    """Inspect configuration and backend support without connecting or reading."""
    settings = ctx.app.settings
    return _make_result(
        True,
        data={
            "connected": bool(getattr(ctx.app.modbus_client, "connected", False)),
            "type": settings.modbus_type,
            "target": settings.target,
            "default_slave_id": settings.default_slave_id,
            "writes_enabled": settings.writes_enabled,
            "tag_count": len(ctx.app.tags),
            "capabilities": capability_report(
                writes_enabled=settings.writes_enabled,
                transport=settings.modbus_type,
            ),
        },
    )


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
            address=address, count=1, **ctx.transport.unit(client, slave_id)
        )

    details: dict[str, Any] = {}
    result, probe_error, probe_ms, _ = await ctx.transport.request(
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
