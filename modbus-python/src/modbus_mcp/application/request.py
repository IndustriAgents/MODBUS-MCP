from collections.abc import Awaitable, Callable
from typing import Any

from ..domain.model import (
    MAY_HAVE_BEEN_APPLIED,
    REFUSED,
    WRITE_NO_REPLY,
    WRITE_REPLY_MISMATCH,
    _device_error,
    _is_error,
    _make_result,
)
from ..state import AppContext


async def _request(
    ctx: Any,
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
    result, err, duration_ms, attempts = await ctx.transport.request(
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
            else:
                meta.update(outcome="unknown", request_sent=request_sent)
                err += f"; {MAY_HAVE_BEEN_APPLIED}"
        return (None, _make_result(False, error=err, meta=meta))
    if _is_error(result):
        message, extra = _device_error(result)
        meta.update(extra)
        if write:
            if extra.get("exception_code") in REFUSED:
                meta.update(outcome="rejected", request_sent=True)
            else:
                meta.update(outcome="unknown", request_sent=True)
                message += f"; {MAY_HAVE_BEEN_APPLIED}"
        return (None, _make_result(False, error=message, meta=meta))
    if write:
        meta.update(outcome="written", request_sent=True)
    return (result, None)
