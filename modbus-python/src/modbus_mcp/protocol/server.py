import asyncio
import json
import sys
from typing import Any

import anyio
import anyio.lowlevel
from mcp.server.fastmcp import FastMCP
from mcp.server.fastmcp.exceptions import ToolError
from mcp.server.stdio import stdio_server
from mcp.types import TextContent
from pydantic import ValidationError

from ..config import Settings
from ..domain.model import MAY_HAVE_BEEN_APPLIED, WRITE_TOOLS, _make_result, log
from ..infrastructure.control_context import _CALL, _CallState, run_with_deadline

if sys.version_info < (3, 11):
    from exceptiongroup import BaseExceptionGroup


def _describe_validation_error(exc: ValidationError) -> str:
    parts = []
    for err in exc.errors():
        where = ".".join(str(p) for p in err.get("loc", ())) or "arguments"
        part = f"{where}: {err.get('msg', 'invalid')}"
        if err.get("type") != "missing" and "input" in err:
            part += f" (got {err['input']!r})"
        parts.append(part)
    return "; ".join(parts)


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

    def _request_cancel_scope(self) -> Any:
        """The cancel scope mcp opened for the current request, or None.

        mcp's RequestResponder.cancel() (notifications/cancelled) cancels this scope
        synchronously, before the running call notices; SendGate reads its
        cancel_called. Private mcp<2 internals (session._in_flight,
        RequestResponder._cancel_scope); tests/test_cancel_race.py fails if they move.
        """
        try:
            context = self._mcp_server.request_context
        except LookupError:
            return None
        in_flight = getattr(context.session, "_in_flight", None)
        responder = in_flight.get(context.request_id) if isinstance(in_flight, dict) else None
        return getattr(responder, "_cancel_scope", None)

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
            return await super().call_tool(name, arguments)
        state = _CallState.start(self._call_timeout())
        state.request_scope = self._request_cancel_scope()
        result = await self._run_call(tool, name, arguments, state)
        if getattr(state.request_scope, "cancel_called", False):
            what = "a request had already been sent" if state.sent else "nothing was sent"
            log.info("%s was cancelled by the client; %s", name, what)
            await anyio.lowlevel.checkpoint_if_cancelled()
            raise anyio.get_cancelled_exc_class()()
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
            state.cancelled = True
            why = f"did not finish within the {seconds:g} s deadline (MODBUS_CALL_TIMEOUT)"
            return self._envelope(tool, self._unfinished(name, state, why))
        except BaseException:
            state.cancelled = True
            raise
        finally:
            self._call_scopes.discard(scope)
        state.cancelled = True
        envelope = self._unfinished(name, state, "the MCP client disconnected before it finished")
        return self._envelope(tool, envelope)

    @staticmethod
    def _envelope(tool: Any, envelope: dict[str, Any]) -> Any:
        convert = getattr(getattr(tool, "fn_metadata", None), "convert_result", None)
        if convert is not None:
            return convert(envelope)
        return [TextContent(type="text", text=json.dumps(envelope))]

    async def _call_tool(self, name: str, arguments: dict[str, Any]) -> Any:
        try:
            return await super().call_tool(name, arguments)
        except ToolError as exc:
            tool = self._tool_manager.get_tool(name)
            if tool is None:
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
                state = _CALL.get()
                if isinstance(cause, ValidationError) or (state is not None and (not state.sent)):
                    meta.update(outcome="not_sent", request_sent=False)
                else:
                    meta.update(outcome="unknown", request_sent=True if state else None)
                    error += f"; {MAY_HAVE_BEEN_APPLIED}"
            return self._envelope(tool, _make_result(False, error=error, meta=meta))
