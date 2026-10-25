from typing import Annotated, Any

from mcp.server.fastmcp import Context
from mcp.server.fastmcp.prompts import base
from pydantic import Field

from ..application import diagnostics as diagnostics_operations
from ..application import read as read_operations
from ..application import write as write_operations
from ..domain.model import (
    MAX_READWRITE_REGISTERS,
    MAX_WRITE_COILS,
    MAX_WRITE_REGISTERS,
    Address,
    BitCount,
    ByteOrder,
    DType,
    Mask,
    Offset,
    RegisterCount,
    RegisterValue,
    Scale,
    SlaveId,
    StrictBool,
    TypedCount,
    WordOrder,
)
from ..protocol.hints import DEVICE_WRITE, READ_ONLY


def register_tools(mcp, context_for, policy=None):

    @mcp.tool(annotations=READ_ONLY)
    async def get_connection_status(ctx: Context) -> dict[str, Any]:
        """Inspect configured endpoint and backend support without device I/O."""
        return await diagnostics_operations.get_connection_status(context_for(ctx))

    @mcp.tool(annotations=READ_ONLY)
    async def read_register(
        address: Address, ctx: Context, slave_id: SlaveId = None
    ) -> dict[str, Any]:
        """Read one holding register (function 3); data.value is its raw 16-bit value."""
        return await read_operations.read_register(address, context_for(ctx), slave_id)

    @mcp.tool(annotations=DEVICE_WRITE)
    async def write_register(
        address: Address, value: RegisterValue, ctx: Context, slave_id: SlaveId = None
    ) -> dict[str, Any]:
        """Write one holding register (function 6).

        Only works when MODBUS_WRITES_ENABLED=true; otherwise nothing is sent and
        success is false. The write is sent at most once. meta.outcome says what
        happened: written (acknowledged), rejected (refused with an exception that
        means nothing was done), not_sent, or unknown (no reply, a reply that does
        not match, or an exception that does not rule a write out: it may have been
        applied, so check before retrying); meta.request_sent says whether the
        request went out.
        """
        return await write_operations.write_register(address, value, context_for(ctx), slave_id)

    @mcp.tool(annotations=READ_ONLY)
    async def read_coils(
        address: Address, count: BitCount, ctx: Context, slave_id: SlaveId = None
    ) -> dict[str, Any]:
        """Read coils (function 1); data.values is a list of booleans.

        Counts above 2000 are split into several requests.
        """
        return await read_operations.read_coils(address, count, context_for(ctx), slave_id)

    @mcp.tool(annotations=DEVICE_WRITE)
    async def write_coil(
        address: Address,
        value: Annotated[bool, Field(strict=True, description="true = ON, false = OFF.")],
        ctx: Context,
        slave_id: SlaveId = None,
    ) -> dict[str, Any]:
        """Write one coil (function 5).

        Only works when MODBUS_WRITES_ENABLED=true; otherwise nothing is sent and
        success is false. The write is sent at most once. meta.outcome says what
        happened: written (acknowledged), rejected (refused with an exception that
        means nothing was done), not_sent, or unknown (no reply, a reply that does
        not match, or an exception that does not rule a write out: it may have been
        applied, so check before retrying); meta.request_sent says whether the
        request went out.
        """
        return await write_operations.write_coil(address, value, context_for(ctx), slave_id)

    @mcp.tool(annotations=READ_ONLY)
    async def read_input_registers(
        address: Address, count: RegisterCount, ctx: Context, slave_id: SlaveId = None
    ) -> dict[str, Any]:
        """Read input registers (function 4); data.registers is a list of raw 16-bit values.

        Counts above 125 are split into several requests.
        """
        return await read_operations.read_input_registers(
            address, count, context_for(ctx), slave_id
        )

    @mcp.tool(annotations=READ_ONLY)
    async def read_multiple_holding_registers(
        address: Address, count: RegisterCount, ctx: Context, slave_id: SlaveId = None
    ) -> dict[str, Any]:
        """Read holding registers (function 3); data.registers is a list of raw 16-bit values.

        Counts above 125 are split into several requests.
        """
        return await read_operations.read_multiple_holding_registers(
            address, count, context_for(ctx), slave_id
        )

    @mcp.tool(annotations=READ_ONLY)
    async def read_discrete_inputs(
        address: Address, count: BitCount, ctx: Context, slave_id: SlaveId = None
    ) -> dict[str, Any]:
        """Read discrete inputs (function 2); data.values is a list of booleans.

        Counts above 2000 are split into several requests.
        """
        return await read_operations.read_discrete_inputs(
            address, count, context_for(ctx), slave_id
        )

    @mcp.tool(annotations=DEVICE_WRITE)
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
        ctx: Context,
        slave_id: SlaveId = None,
    ) -> dict[str, Any]:
        """Write consecutive holding registers in one request (function 16).

        Only works when MODBUS_WRITES_ENABLED=true; otherwise nothing is sent and
        success is false. The write is sent at most once. meta.outcome says what
        happened: written (acknowledged), rejected (refused with an exception that
        means nothing was done), not_sent, or unknown (no reply, a reply that does
        not match, or an exception that does not rule a write out: it may have been
        applied, so check before retrying); meta.request_sent says whether the
        request went out.
        """
        return await write_operations.write_registers(address, values, context_for(ctx), slave_id)

    @mcp.tool(annotations=DEVICE_WRITE)
    async def readwrite_registers(
        read_address: Address,
        read_count: Annotated[int, Field(strict=True, ge=1, le=125)],
        write_address: Address,
        values: Annotated[
            list[RegisterValue], Field(min_length=1, max_length=MAX_READWRITE_REGISTERS)
        ],
        ctx: Context,
        slave_id: SlaveId = None,
    ) -> dict[str, Any]:
        """Write 1..121 and then read 1..125 holding registers in one FC23 request.

        Requires MODBUS_WRITES_ENABLED=true and device FC23 support. Ranges may
        overlap; the write precedes the read. No split, read-back retry or replay
        after possible dispatch. An acknowledgement is not physical completion
        or a PLC scan snapshot. Lost/mismatched replies leave the write unknown.
        """
        return await write_operations.readwrite_registers(
            read_address, read_count, write_address, values, context_for(ctx), slave_id
        )

    @mcp.tool(annotations=DEVICE_WRITE)
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
        ctx: Context,
        slave_id: SlaveId = None,
    ) -> dict[str, Any]:
        """Write consecutive coils in one request (function 15).

        Only works when MODBUS_WRITES_ENABLED=true; otherwise nothing is sent and
        success is false. The write is sent at most once. meta.outcome says what
        happened: written (acknowledged), rejected (refused with an exception that
        means nothing was done), not_sent, or unknown (no reply, a reply that does
        not match, or an exception that does not rule a write out: it may have been
        applied, so check before retrying); meta.request_sent says whether the
        request went out.
        """
        return await write_operations.write_coils_bulk(address, values, context_for(ctx), slave_id)

    @mcp.tool(annotations=DEVICE_WRITE)
    async def mask_write_register(
        address: Address, and_mask: Mask, or_mask: Mask, ctx: Context, slave_id: SlaveId = None
    ) -> dict[str, Any]:
        """Change bits of one holding register in place (function 22).

        The device sets: register = (register AND and_mask) OR (or_mask AND NOT and_mask).
        Only works when MODBUS_WRITES_ENABLED=true; otherwise nothing is sent and
        success is false. The write is sent at most once. meta.outcome says what
        happened: written (acknowledged), rejected (refused with an exception that
        means nothing was done), not_sent, or unknown (no reply, a reply that does
        not match, or an exception that does not rule a write out: it may have been
        applied, so check before retrying); meta.request_sent says whether the
        request went out.
        """
        return await write_operations.mask_write_register(
            address, and_mask, or_mask, context_for(ctx), slave_id
        )

    @mcp.tool(annotations=READ_ONLY)
    async def read_device_information(
        ctx: Context,
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
            int,
            Field(ge=0, le=255, strict=True, description="First object to read (0 = VendorName)."),
        ] = 0,
    ) -> dict[str, Any]:
        """Read the device identification objects (function 43 / MEI type 14).

        data maps object ids to text: 0 VendorName, 1 ProductCode, 2 MajorMinorRevision,
        3 VendorUrl, 4 ProductName, 5 ModelName, 6 UserApplicationName.
        """
        return await diagnostics_operations.read_device_information(
            context_for(ctx), slave_id, read_code, object_id
        )

    @mcp.tool(annotations=READ_ONLY)
    async def read_holding_typed(
        address: Address,
        dtype: DType,
        ctx: Context,
        count: TypedCount = 1,
        byteorder: ByteOrder = "big",
        wordorder: WordOrder = "big",
        scale: Scale = 1.0,
        offset: Offset = 0.0,
        slave_id: SlaveId = None,
    ) -> dict[str, Any]:
        """Read holding registers and decode them as typed values.

        data.values holds `count` values of `dtype`, each multiplied by `scale` and
        increased by `offset`. NaN and infinities come back as the strings "NaN",
        "Infinity" and "-Infinity".
        """
        return await read_operations.read_holding_typed(
            address, dtype, context_for(ctx), count, byteorder, wordorder, scale, offset, slave_id
        )

    @mcp.tool(annotations=READ_ONLY)
    async def read_input_typed(
        address: Address,
        dtype: DType,
        ctx: Context,
        count: TypedCount = 1,
        byteorder: ByteOrder = "big",
        wordorder: WordOrder = "big",
        scale: Scale = 1.0,
        offset: Offset = 0.0,
        slave_id: SlaveId = None,
    ) -> dict[str, Any]:
        """Read input registers and decode them as typed values.

        data.values holds `count` values of `dtype`, each multiplied by `scale` and
        increased by `offset`. NaN and infinities come back as the strings "NaN",
        "Infinity" and "-Infinity".
        """
        return await read_operations.read_input_typed(
            address, dtype, context_for(ctx), count, byteorder, wordorder, scale, offset, slave_id
        )

    @mcp.tool(annotations=READ_ONLY)
    async def list_tags(ctx: Context) -> dict[str, Any]:
        "List the named tags defined in the tag map (REGISTER_MAP_FILE), with their definitions."
        return await diagnostics_operations.list_tags(context_for(ctx))

    @mcp.tool(annotations=READ_ONLY)
    async def read_tag(
        name: Annotated[str, Field(description="Tag name from list_tags.")], ctx: Context
    ) -> dict[str, Any]:
        """Read a named tag from the tag map.

        Tags with a dtype are decoded and scaled like read_holding_typed/read_input_typed;
        other tags return raw registers or bits.
        """
        return await read_operations.read_tag(name, context_for(ctx))

    @mcp.tool(annotations=DEVICE_WRITE)
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
        ctx: Context,
    ) -> dict[str, Any]:
        """Write a named holding-register or coil tag from the tag map.

        Holding-register values are in the same units read_tag returns: the tag's
        scale and offset are reversed, then the value is encoded with its dtype
        (default uint16) and byte/word order. Input and discrete-input tags are
        read-only. Only works when MODBUS_WRITES_ENABLED=true; otherwise nothing is
        sent and success is false. meta.outcome and meta.request_sent work as in
        write_register.
        """
        return await write_operations.write_tag(name, value, context_for(ctx))

    @mcp.tool(annotations=READ_ONLY)
    async def ping(
        ctx: Context,
        slave_id: SlaveId = None,
        address: Annotated[
            int,
            Field(
                ge=0, le=65535, strict=True, description="Holding register to probe (default 0)."
            ),
        ] = 0,
    ) -> dict[str, Any]:
        """Check that the device answers, and report the server's configuration.

        Reads one holding register (address 0 unless given) as a probe. Any Modbus
        reply counts as reachable, including an exception reply such as Illegal Data
        Address from a device without that register; no reply (timeout, connection
        refused) does not, and then success is false. Connects first if the server
        is not connected (the device was unreachable at startup, or the connection
        dropped); if that fails, the error is "cannot reach device at ...".
        """
        return await diagnostics_operations.ping(context_for(ctx), slave_id, address)

    @mcp.prompt()
    def analyze_register(value: str) -> list[base.Message]:
        """Prompt to analyze a Modbus register value."""
        return [
            base.UserMessage(f"I read this value from a Modbus register: {value}"),
            base.UserMessage("Can you help me understand what it means?"),
            base.AssistantMessage(
                "I'll help analyze the register value. Please "
                "provide any context about the device or system."
            ),
        ]

    from .contract import install_contract

    install_contract(mcp, policy)
