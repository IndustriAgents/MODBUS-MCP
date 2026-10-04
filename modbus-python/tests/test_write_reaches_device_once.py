"""Regression test: a write whose reply is lost reaches the device exactly once.

A small Modbus TCP device applies every write and then drops the reply (reads
are answered). The real server, over stdio, must report "no reply; the write
may have been applied" and must not send the write again - neither through its
own retries nor through pymodbus' (which re-sends on timeout unless the client
is created with retries=0).
"""

from __future__ import annotations

import asyncio
import json
import struct
from collections import Counter
from types import SimpleNamespace

import pytest
from test_stdio_integration import server

from modbus_mcp.cli import WRITE_NO_REPLY

pytestmark = [pytest.mark.integration, pytest.mark.anyio]


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


class DropWriteRepliesDevice:
    """Modbus TCP device: answers reads (FC 1, 3), applies writes (FC 5, 6, 15, 16, 22)
    without replying, and counts every request frame it receives."""

    def __init__(self) -> None:
        self.hr = [0] * 100
        self.co = [False] * 100
        self.received: Counter = Counter()  # (function code, address) -> frames received
        self.server: asyncio.base_events.Server | None = None
        self.host, self.port = "127.0.0.1", 0

    async def __aenter__(self):
        self.server = await asyncio.start_server(self._serve, self.host, 0)  # free port
        self.port = self.server.sockets[0].getsockname()[1]
        return self

    async def __aexit__(self, *exc) -> None:
        self.server.close()
        await self.server.wait_closed()

    def writes(self) -> Counter:
        return Counter({k: v for k, v in self.received.items() if k[0] in (5, 6, 15, 16, 22)})

    async def _serve(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            while True:
                tid, _pid, length, unit = struct.unpack(">HHHB", await reader.readexactly(7))
                pdu = await reader.readexactly(length - 1)
                reply = self._handle(pdu[0], pdu[1:])
                if reply is not None:
                    writer.write(struct.pack(">HHHB", tid, 0, len(reply) + 1, unit) + reply)
                    await writer.drain()
        except (asyncio.IncompleteReadError, ConnectionError):
            pass
        finally:
            writer.close()

    def _handle(self, fc: int, data: bytes) -> bytes | None:
        address = struct.unpack(">H", data[:2])[0]
        self.received[(fc, address)] += 1
        if fc == 3:
            (count,) = struct.unpack(">H", data[2:4])
            regs = self.hr[address : address + count]
            return struct.pack(f">BB{count}H", 3, 2 * count, *regs)
        if fc == 1:
            (count,) = struct.unpack(">H", data[2:4])
            packed = bytearray((count + 7) // 8)
            for i, bit in enumerate(self.co[address : address + count]):
                packed[i // 8] |= bit << (i % 8)
            return bytes([1, len(packed)]) + bytes(packed)
        # Writes: apply, then lose the reply.
        if fc == 5:
            self.co[address] = struct.unpack(">H", data[2:4])[0] == 0xFF00
        elif fc == 6:
            self.hr[address] = struct.unpack(">H", data[2:4])[0]
        elif fc == 15:
            (count,) = struct.unpack(">H", data[2:4])
            for i in range(count):
                self.co[address + i] = bool(data[5 + i // 8] >> (i % 8) & 1)
        elif fc == 16:
            (count,) = struct.unpack(">H", data[2:4])
            self.hr[address : address + count] = struct.unpack(
                f">{count}H", data[5 : 5 + 2 * count]
            )
        elif fc == 22:
            and_mask, or_mask = struct.unpack(">HH", data[2:6])
            self.hr[address] = (self.hr[address] & and_mask) | (or_mask & ~and_mask & 0xFFFF)
        else:
            return bytes([fc | 0x80, 1])  # Illegal Function
        return None


async def test_lost_write_replies_with_pymodbus_timeout(tmp_path) -> None:
    """No tool timeout: pymodbus' own response timeout (3 s) fires, and must not re-send."""
    async with DropWriteRepliesDevice() as device:
        target = SimpleNamespace(host=device.host, port=device.port)
        env = {"MODBUS_WRITES_ENABLED": "true", "MODBUS_MAX_RETRIES": "3"}
        async with server(target, tmp_path, **env) as srv:
            result = await srv.call("write_register", address=5, value=1234)
            assert result["success"] is False
            assert result["error"] == WRITE_NO_REPLY
            assert result["meta"]["attempts"] == 1
            result = await srv.call("write_coils_bulk", address=10, values=[True, False, True])
            assert result["error"] == WRITE_NO_REPLY

            assert device.writes() == {(6, 5): 1, (15, 10): 1}
            # "may have been applied": it was.
            assert (await srv.call("read_register", address=5))["data"] == {"value": 1234}
            coils = await srv.call("read_coils", address=10, count=3)
            assert coils["data"]["values"] == [True, False, True]


async def test_every_write_tool_with_a_tool_timeout(tmp_path) -> None:
    """MODBUS_TOOL_TIMEOUT cuts the wait short; still exactly one frame per write."""
    tags = tmp_path / "tags.json"
    tags.write_text(
        json.dumps(
            {
                "Setpoint": {"table": "holding", "address": 30, "dtype": "float32"},
                "Lamp": {"table": "coil", "address": 20},
            }
        )
    )
    async with DropWriteRepliesDevice() as device:
        target = SimpleNamespace(host=device.host, port=device.port)
        env = {
            "MODBUS_WRITES_ENABLED": "true",
            "MODBUS_MAX_RETRIES": "3",
            "MODBUS_TOOL_TIMEOUT": "0.5",
            "REGISTER_MAP_FILE": str(tags),
        }
        calls = [
            ("write_register", {"address": 1, "value": 7}, (6, 1)),
            ("write_registers", {"address": 2, "values": [8, 9]}, (16, 2)),
            ("write_coil", {"address": 3, "value": True}, (5, 3)),
            ("write_coils_bulk", {"address": 4, "values": [True, True]}, (15, 4)),
            ("mask_write_register", {"address": 6, "and_mask": 0xFFFC, "or_mask": 3}, (22, 6)),
            ("write_tag", {"name": "Setpoint", "value": 42.5}, (16, 30)),
            ("write_tag", {"name": "Lamp", "value": True}, (5, 20)),
        ]
        async with server(target, tmp_path, **env) as srv:
            for tool, arguments, _frame in calls:
                result = await srv.call(tool, **arguments)
                assert result["success"] is False, (tool, result)
                assert result["error"] == WRITE_NO_REPLY, (tool, result)
            assert device.writes() == {frame: 1 for _, _, frame in calls}
            assert device.hr[1:4] == [7, 8, 9] and device.hr[6] == 3
            assert device.co[3:6] == [True, True, True] and device.co[20] is True
