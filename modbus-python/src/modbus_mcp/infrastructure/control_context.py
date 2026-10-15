import asyncio
import contextvars
from collections.abc import Awaitable
from dataclasses import dataclass
from typing import Any

import anyio
import anyio.lowlevel


@dataclass
class _CallState:
    """One tool call in progress: its deadline, and whether it may still send."""

    expires: float
    seconds: float
    cancelled: bool = False
    frames: int = 0
    request_scope: Any = None

    @property
    def sent(self) -> bool:
        return self.frames > 0

    @classmethod
    def start(cls, seconds: float) -> "_CallState":
        return cls(asyncio.get_running_loop().time() + seconds, seconds)


_CALL: contextvars.ContextVar[_CallState | None] = contextvars.ContextVar(
    "modbus_mcp_call", default=None
)
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
    if getattr(state.request_scope, "cancel_called", False):
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
