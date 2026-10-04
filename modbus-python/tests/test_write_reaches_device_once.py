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
import time
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
    and counts every request frame it receives.

    `write_reply` says how it answers a write: "drop" (no reply, the default),
    "echo" (a correct reply), "bad_echo" (a reply whose echoed value is wrong), a
    Modbus exception code (the write is then not applied), or "garbage" (every
    request, read or write, gets bytes that never form a Modbus frame).
    """

    def __init__(
        self,
        write_reply: str | int = "drop",
        port: int = 0,
        hold: asyncio.Event | None = None,
        read_delay: float = 0.0,
    ) -> None:
        self.write_reply = write_reply
        self.requested_port = port  # 0: a free port
        self.hold = hold  # while set and not yet triggered, reads get no answer
        self.read_delay = read_delay  # a slow device: seconds before a read is answered
        # (loop time, function code, address) of every frame, when it arrived
        self.arrivals: list[tuple[float, int, int]] = []
        self.hr = [0] * 400  # room for reads split into several requests
        self.co = [False] * 100
        self.received: Counter = Counter()  # (function code, address) -> frames received
        self.server: asyncio.base_events.Server | None = None
        self.host, self.port = "127.0.0.1", 0

    async def __aenter__(self):
        self.server = await asyncio.start_server(self._serve, self.host, self.requested_port)
        self.port = self.server.sockets[0].getsockname()[1]
        return self

    async def __aexit__(self, *exc) -> None:
        self.server.close()
        await self.server.wait_closed()

    def writes(self) -> Counter:
        return Counter({k: v for k, v in self.received.items() if k[0] in (5, 6, 15, 16, 22)})

    async def _serve(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        pending: set[asyncio.Task] = set()
        try:
            while True:
                tid, _pid, length, unit = struct.unpack(">HHHB", await reader.readexactly(7))
                pdu = await reader.readexactly(length - 1)
                address = struct.unpack(">H", pdu[1:3])[0] if len(pdu) >= 3 else 0
                self.arrivals.append((asyncio.get_running_loop().time(), pdu[0], address))
                reply = self._handle(pdu[0], pdu[1:])
                if reply is None:
                    continue
                frame = struct.pack(">HHHB", tid, 0, len(reply) + 1, unit) + reply
                if pdu[0] in (1, 2, 3, 4) and (self.hold is not None or self.read_delay):
                    # Answer later, from a task, so frames behind it are still read
                    # (and timestamped) as soon as they arrive.
                    task = asyncio.create_task(self._answer_later(writer, frame))
                    pending.add(task)
                    task.add_done_callback(pending.discard)
                else:
                    writer.write(frame)
                    await writer.drain()
        except (asyncio.IncompleteReadError, ConnectionError):
            pass
        finally:
            for task in pending:
                task.cancel()
            writer.close()

    async def _answer_later(self, writer: asyncio.StreamWriter, frame: bytes) -> None:
        if self.hold is not None:
            await self.hold.wait()  # a hung device: the read waits
        await asyncio.sleep(self.read_delay)
        if not writer.is_closing():
            writer.write(frame)
            await writer.drain()

    def _handle(self, fc: int, data: bytes) -> bytes | None:
        address = struct.unpack(">H", data[:2])[0]
        self.received[(fc, address)] += 1
        if self.write_reply == "garbage":  # answer everything with bytes that never form a frame
            return b"garbage!" * 3
        if fc in (5, 6, 15, 16, 22) and isinstance(self.write_reply, int):
            return bytes([fc | 0x80, self.write_reply])  # refused, not applied
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
        if self.write_reply == "drop":
            return None
        # A write reply echoes the function code, the address and the value
        # (FC 5/6), the count (FC 15/16) or the masks (FC 22).
        echo = bytes([fc]) + data[: 6 if fc == 22 else 4]
        if self.write_reply == "bad_echo":
            if fc == 5:  # report the other coil state (pymodbus reads 0xFF01 as ON too)
                echo = echo[:3] + (b"\x00\x00" if echo[3:5] == b"\xff\x00" else b"\xff\x00")
            else:  # value, count or mask off by one bit
                echo = echo[:-1] + bytes([echo[-1] ^ 0x01])
        return echo


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
            assert result["meta"]["outcome"] == "unknown"
            assert result["meta"]["request_sent"] is True
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
                assert result["meta"]["outcome"] == "unknown", (tool, result)
                assert result["meta"]["request_sent"] is True, (tool, result)
            assert device.writes() == {frame: 1 for _, _, frame in calls}
            assert device.hr[1:4] == [7, 8, 9] and device.hr[6] == 3
            assert device.co[3:6] == [True, True, True] and device.co[20] is True


async def test_reply_classification_over_the_wire(tmp_path) -> None:
    """Real pymodbus decoding: a correct echo is "written"; a wrong echo is "unknown"
    (it may have been applied); a refusing exception is "rejected"; an exception that
    does not rule a write out is "unknown". Each write reaches the device once."""
    cases = [
        ("echo", True, "written"),
        ("bad_echo", False, "unknown"),
        (2, False, "rejected"),  # Illegal Data Address
        (4, False, "unknown"),  # Server Device Failure
    ]
    for reply, success, outcome in cases:
        async with DropWriteRepliesDevice(write_reply=reply) as device:
            target = SimpleNamespace(host=device.host, port=device.port)
            async with server(target, tmp_path, MODBUS_WRITES_ENABLED="true") as srv:
                for tool, arguments, frame in [
                    ("write_register", {"address": 1, "value": 7}, (6, 1)),
                    ("write_coil", {"address": 2, "value": True}, (5, 2)),
                    ("write_registers", {"address": 3, "values": [1, 2]}, (16, 3)),
                    ("write_coils_bulk", {"address": 4, "values": [True]}, (15, 4)),
                    ("mask_write_register", {"address": 5, "and_mask": 1, "or_mask": 2}, (22, 5)),
                ]:
                    result = await srv.call(tool, **arguments)
                    label = (reply, tool, result)
                    assert result["success"] is success, label
                    assert result["meta"]["outcome"] == outcome, label
                    assert result["meta"]["request_sent"] is True, label
                    may = "may have been applied" in (result["error"] or "")
                    assert may is (outcome == "unknown"), label
                    assert device.received[frame] == 1, label


async def test_a_peer_that_answers_garbage_cannot_hang_a_call(tmp_path) -> None:
    """pymodbus drops the garbled bytes and keeps waiting; the deadline (2 s, shorter
    than pymodbus' own 3 s timeout) ends every call with an outcome."""
    async with DropWriteRepliesDevice(write_reply="garbage") as device:
        target = SimpleNamespace(host=device.host, port=device.port)
        env = {"MODBUS_WRITES_ENABLED": "true", "MODBUS_CALL_TIMEOUT": "2"}
        async with server(target, tmp_path, **env) as srv:
            start = time.monotonic()
            read = await srv.call("read_register", address=0)
            assert time.monotonic() - start < 2.9  # 2 s deadline + slack
            assert read["success"] is False
            assert read["error"].startswith("no result within the 2 s deadline"), read

            start = time.monotonic()
            write = await srv.call("write_register", address=1, value=5)
            assert time.monotonic() - start < 2.9  # 2 s deadline + slack
            assert write["error"] == WRITE_NO_REPLY, write
            assert write["meta"]["outcome"] == "unknown" and write["meta"]["request_sent"] is True
            assert device.received[(6, 1)] == 1

            ping = await srv.call("ping")
            assert ping["success"] is False and ping["data"]["device_reachable"] is False
