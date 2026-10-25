"""Bounded Modbus device-identification continuation, with honest completeness."""

from typing import Annotated, Any

from pydantic import Field

from ..application.request import _request
from ..domain.model import SlaveId, _make_result, _slave, reply_check


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

    cursor = object_id
    data: dict[str, str] = {}
    meta.update(pages=0, complete=False, attempts=0, duration_ms=0.0)
    while True:

        async def _call(page_cursor=cursor):
            return await client.read_device_information(
                read_code=read_code,
                object_id=page_cursor,
                **ctx.transport.unit(client, slave_id),
            )

        page_meta: dict[str, Any] = {}
        op = f"read_device_information slave={slave_id} code={read_code} obj={cursor}"
        result, failure = await _request(ctx, app, op, _call, page_meta, check=reply_check(43))
        meta["attempts"] += page_meta.get("attempts", 0)
        meta["duration_ms"] = round(meta["duration_ms"] + page_meta.get("duration_ms", 0), 3)
        if failure:
            meta.update(
                {k: v for k, v in page_meta.items() if k not in {"attempts", "duration_ms"}}
            )
            return _make_result(False, data=data, error=failure["error"], meta=meta)
        problem = _page_error(result, read_code, cursor, data)
        if problem:
            return _make_result(False, data=data, error=problem, meta=meta)
        information = result.information
        if not data and cursor > 0 and 0 in information:
            meta["restarted_at_zero"] = True
        data.update(
            {
                str(key): value.decode("utf-8", errors="replace")
                if isinstance(value, (bytes, bytearray))
                else value
                for key, value in information.items()
            }
        )
        meta["pages"] += 1
        if not getattr(result, "more_follows", 0):
            meta["complete"] = True
            return _make_result(True, data=data, meta=meta)
        cursor = result.next_object_id


def _page_error(result: Any, read_code: int, cursor: int, known: dict) -> str | None:
    if (
        getattr(result, "sub_function_code", None) != 14
        or getattr(result, "read_code", None) != read_code
    ):
        return "Device identification response does not match the requested MEI/read code"
    information = getattr(result, "information", None)
    if not isinstance(information, dict):
        return "Device identification response has no object map"
    if getattr(result, "number_of_objects", None) != len(information):
        return "Device identification object count does not match the returned map"
    if read_code == 4 and set(information) != {cursor}:
        return "Individual device identification did not return exactly the requested object"
    for key, value in information.items():
        if type(key) is not int or not 0 <= key <= 255 or str(key) in known:
            return "Device identification returned an invalid or repeated object ID"
        if known and key < cursor:
            return "Device identification continuation returned objects before its cursor"
        if not isinstance(value, (str, bytes, bytearray)) or len(value) > 255:
            return "Device identification returned an invalid object value"
    more = getattr(result, "more_follows", None)
    if not isinstance(more, int) or isinstance(more, bool) or more not in (0, 255):
        return "Device identification returned an invalid more-follows flag"
    if more:
        next_id = getattr(result, "next_object_id", None)
        # Section 6.21 permits an unknown initial stream object to restart at 0.
        floor = 0 if not known and 0 in information else cursor
        if read_code == 4 or type(next_id) is not int or not floor < next_id <= 255:
            return "Device identification continuation did not advance within the object-ID range"
        if information and next_id <= max(information):
            return "Device identification continuation overlaps the returned objects"
    elif getattr(result, "next_object_id", None) != 0:
        return "Terminal device identification response has a nonzero next object ID"
    return None
