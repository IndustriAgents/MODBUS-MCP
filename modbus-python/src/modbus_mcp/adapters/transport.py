import asyncio
import contextlib
import functools
import inspect
import time
from collections.abc import Awaitable, Callable
from typing import Any

import anyio
import anyio.lowlevel
from pymodbus.client import AsyncModbusSerialClient, AsyncModbusTcpClient, AsyncModbusUdpClient
from pymodbus.exceptions import ConnectionException, ModbusException

from ..config import Settings
from ..domain.model import READ_REPLY_MISMATCH, WRITE_NO_REPLY, WRITE_REPLY_MISMATCH
from ..infrastructure.control_context import (
    _CALL,
    _abandoned,
    _deadline_error,
    _limit,
    _remaining,
    _why_not_send,
)
from ..state import AppContext


class RequestNotSent(Exception):
    """Raised by SendGate in place of sending a request that must not go out."""


class NotConnected(RequestNotSent):
    """Raised by SendGate when the connection is gone: nothing was written (pymodbus
    would drop the frame silently), and a new connection may succeed."""


_RX_KEEP = 512


class SendGate:
    """pymodbus trace_packet hook: the last check before a request leaves, and a counter.

    pymodbus calls trace_packet(True, frame) once a request is fully encoded,
    synchronously and immediately before handing it to the transport: there is
    no await between this check and the send. It refuses the send (raising
    RequestNotSent, so nothing is written) when the MCP client has disconnected,
    the tool call was cancelled (also by notifications/cancelled that arrived in
    the same event-loop step), or its deadline has passed; a request that was
    queued, reconnecting or waiting then is never sent later. It also refuses
    (NotConnected) when the connection has been closed, since pymodbus would
    otherwise drop the frame silently. Otherwise it counts the frame, so an
    unchanged count means nothing went out.

    trace_packet(False, data) is called with the bytes received; the gate keeps
    those that arrived since the last send (`rx`).
    """

    def __init__(self, closing: Callable[[], bool] = lambda: False) -> None:
        self.sent = 0
        self.client: Any = None
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
        if context is not None and (not getattr(context, "transport", None)):
            raise NotConnected("not connected (the connection was closed)")
        self.sent += 1
        self.rx = bytearray()
        state = _CALL.get()
        if state is not None:
            state.frames += 1
        return data


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


async def _reconnect(ctx: Any, app: AppContext, op: str) -> bool:
    """Re-open a dropped connection; True if the client is connected afterwards.

    pymodbus raises ConnectionException while the connection is down and keeps
    retrying in the background with a back-off of up to 5 minutes. close() stops
    that loop, so it cannot race the connect() here, and connect() tries now.
    """
    client = app.modbus_client
    async with app.reconnect_lock:
        if getattr(client, "connected", False):
            return True
        await ctx.info(f"{op}: not connected, reconnecting to {app.settings.target}")
        quiet = app.quiet_until - time.monotonic()
        if quiet > 0:
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
    ctx: Any,
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
    could not be opened, or the send counter proves no frame left - are
    retried. A ConnectionException alone is not proof of non-dispatch. Once a
    write has gone out, a missing or late reply returns WRITE_NO_REPLY; sending
    it again could apply it twice. (The pymodbus client is created with
    retries=0 for the same reason; see
    _make_client.) The underlying error, if any, goes to `details["cause"]`.

    `check(reply)` says whether the reply belongs to this request (function code,
    count, echo); a reply that does not is not used (a read is retried, a write
    returns WRITE_REPLY_MISMATCH) and details["reply_mismatch"] says why. Whenever a
    request went out and its reply was not taken, the connection is closed
    (_desync), so a late reply is not taken for the next request's (on a serial
    line, one up to twice MODBUS_TIMEOUT late; see _reconnect's quiet period).
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
            backoff = settings.retry_backoff_base * 2 ** (attempt - 2)
            await asyncio.sleep(max(0.0, _limit(backoff) or 0.0))
        not_now = _abandoned(app.closing())
        if not_now is not None:
            last_err = f"the request was not sent: {not_now}"
            details.update(request_sent=_sent_since(app, frames_at_start), cause=not_now)
            break
        remaining = _remaining()
        if remaining is not None and remaining <= 0:
            last_err = _deadline_error() if last_err is None else f"{_deadline_error()}: {last_err}"
            if write:
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
                    _desync(app)
                raise
            mismatch = check(result) if check is not None else None
            if mismatch:
                _desync(app)
                details["reply_mismatch"] = mismatch
                await ctx.error(
                    f"{op}: the reply does not match the request ({', '.join(sorted(mismatch))})"
                )
                if write:
                    return (None, WRITE_REPLY_MISMATCH, elapsed_ms(), attempt)
                last_err = READ_REPLY_MISMATCH
                continue
            return (result, None, elapsed_ms(), attempt)
        except NotConnected as e:
            last_err = f"cannot reach device at {settings.target}"
            details["cause"] = str(e)
            await ctx.error(f"{op} failed on attempt {attempt}: {e}")
        except RequestNotSent as e:
            last_err = f"the request was not sent: {e}"
            details.update(request_sent=False, cause=str(e))
            break
        except ConnectionException as e:
            last_err = f"cannot reach device at {settings.target}"
            details["cause"] = f"{type(e).__name__}: {e}"
            await ctx.error(f"{op} failed on attempt {attempt}: {details['cause']}")
            if write and _sent_since(app, frames_before) is not False:
                # A client/library can fail with this exception after sending.
                # Trust the send fence, not the exception name, before retrying.
                return (None, WRITE_NO_REPLY, elapsed_ms(), attempt)
        except (ModbusException, asyncio.TimeoutError, ConnectionError, OSError) as e:
            cause = f"{type(e).__name__}: {e}"
            await ctx.error(f"{op} failed on attempt {attempt}: {cause}")
            if write:
                details["cause"] = cause
                if _sent_since(app, frames_before) is False:
                    details["request_sent"] = False
                    return (None, f"the write was not sent: {cause}", elapsed_ms(), attempt)
                return (None, WRITE_NO_REPLY, elapsed_ms(), attempt)
            last_err = cause
        except Exception as e:
            last_err = f"Unexpected {type(e).__name__}: {e}"
            await ctx.error(f"{op} unexpected error on attempt {attempt}: {last_err}")
            if write:
                details["request_sent"] = _sent_since(app, frames_before)
                if details["request_sent"] is False:
                    last_err = f"the write was not sent: {last_err}"
            break
    return (None, last_err, elapsed_ms(), attempt)


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


def _make_client(settings: Settings, trace_packet: Callable[[bool, bytes], bytes] | None = None):
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


class ModbusTransport:
    """Stateless port adapter; lifecycle and send tracking remain instance-owned."""

    unit = staticmethod(_unit)
    request = staticmethod(_retry_call)
