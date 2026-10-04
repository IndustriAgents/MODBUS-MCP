"""Modbus RTU: replies carry no transaction id, so a reply must be checked against its
request, and a late reply must not be taken for the next request's.

The device is a small Modbus RTU server behind a TCP socket; the real server reaches it
as a serial port with pyserial's socket://host:port URL (MODBUS_TYPE=serial).
"""

from __future__ import annotations

import asyncio
import json
import struct
from types import SimpleNamespace

import pytest
from test_stdio_integration import server

pytestmark = [pytest.mark.integration, pytest.mark.anyio]


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


def crc16(data: bytes) -> bytes:
    crc = 0xFFFF
    for byte in data:
        crc ^= byte
        for _ in range(8):
            crc = (crc >> 1) ^ 0xA001 if crc & 1 else crc >> 1
    return struct.pack("<H", crc)


class RtuDevice:
    """Answers FC 3 and FC 6 (8-byte requests). plan() decides each reply: (delay in s,
    PDU) or None for no reply. Writes are applied by plan()."""

    def __init__(self) -> None:
        self.hr = [0] * 100
        self.log: list[tuple[int, int, int]] = []  # (function code, address, value/count)
        self.server: asyncio.base_events.Server | None = None
        self.port = 0

    def plan(self, fc: int, address: int, value: int) -> tuple[float, bytes] | None:
        if fc == 3:
            regs = self.hr[address : address + value]
            return 0.0, struct.pack(f">BB{len(regs)}H", 3, 2 * len(regs), *regs)
        if fc == 6:
            self.hr[address] = value
            return 0.0, struct.pack(">BHH", 6, address, value)
        return 0.0, bytes([fc | 0x80, 1])

    def frames(self, fc: int) -> int:
        return sum(1 for f, _, _ in self.log if f == fc)

    async def __aenter__(self):
        self.server = await asyncio.start_server(self._serve, "127.0.0.1", 0)
        self.port = self.server.sockets[0].getsockname()[1]
        return self

    async def __aexit__(self, *exc) -> None:
        self.server.close()
        await self.server.wait_closed()

    async def _serve(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        loop = asyncio.get_running_loop()

        def send(frame: bytes) -> None:
            if not writer.is_closing():
                writer.write(frame)

        try:
            while True:
                request = await reader.readexactly(8)
                unit, fc, address, value = struct.unpack(">BBHH", request[:6])
                self.log.append((fc, address, value))
                planned = self.plan(fc, address, value)
                if planned is None:
                    continue
                delay, pdu = planned
                frame = bytes([unit]) + pdu
                frame += crc16(frame)
                if delay:
                    loop.call_later(delay, send, frame)
                else:
                    send(frame)
        except (asyncio.IncompleteReadError, ConnectionError):
            pass
        finally:
            writer.close()


def rtu_env(device: RtuDevice) -> dict[str, str]:
    return {
        "MODBUS_TYPE": "serial",
        "MODBUS_SERIAL_PORT": f"socket://127.0.0.1:{device.port}",
        "MODBUS_TIMEOUT": "1.0",
        "MODBUS_WRITES_ENABLED": "true",
    }


def envelope(result) -> dict:
    return json.loads(result.content[0].text)


async def test_a_late_refusal_of_another_request_does_not_make_a_write_rejected(tmp_path):
    """A ping of a register the device refuses only after the 1 s timeout; a write queued
    behind it. The late exception must not be taken as the write's reply (it would say
    "rejected" although the device applied the write)."""

    class LateRefusal(RtuDevice):
        def plan(self, fc, address, value):
            if fc == 3 and address >= 100:
                return 1.5, bytes([0x83, 0x02])  # Illegal Data Address, but late
            if fc == 6:
                self.hr[address] = value
                return 0.8, struct.pack(">BHH", 6, address, value)
            return super().plan(fc, address, value)

    async with LateRefusal() as device:
        target = SimpleNamespace(host="127.0.0.1", port=device.port)
        async with server(target, tmp_path, **rtu_env(device)) as srv:
            ping = asyncio.create_task(srv.session.call_tool("ping", {"address": 500}))
            await asyncio.sleep(0.2)
            write = asyncio.create_task(
                srv.session.call_tool("write_register", {"address": 7, "value": 1234})
            )
            ping, write = envelope(await ping), envelope(await write)
            await asyncio.sleep(1)
    assert ping["success"] is False
    assert write["meta"]["outcome"] == "written", write
    assert write["success"] is True
    assert device.frames(6) == 1 and device.hr[7] == 1234


async def test_a_late_write_echo_is_not_returned_as_a_read(tmp_path):
    """A write's echo arrives after the 1 s timeout; a read queued behind it must return
    the register's value, not the late echo."""

    class LateEcho(RtuDevice):
        def plan(self, fc, address, value):
            if fc == 6:
                self.hr[address] = value
                return 1.2, struct.pack(">BHH", 6, address, value)
            if fc == 3 and address == 50:
                return 0.5, struct.pack(">BBH", 3, 2, self.hr[50])
            return super().plan(fc, address, value)

    async with LateEcho() as device:
        device.hr[50] = 4321
        target = SimpleNamespace(host="127.0.0.1", port=device.port)
        async with server(target, tmp_path, **rtu_env(device)) as srv:
            write = asyncio.create_task(
                srv.session.call_tool("write_register", {"address": 7, "value": 1234})
            )
            await asyncio.sleep(0.2)
            read = asyncio.create_task(srv.session.call_tool("read_register", {"address": 50}))
            write, read = envelope(await write), envelope(await read)
            await asyncio.sleep(1.5)
    assert read["success"] is True and read["data"] == {"value": 4321}, read
    assert write["meta"]["outcome"] == "unknown" and write["meta"]["request_sent"] is True
    assert device.frames(6) == 1 and device.hr[7] == 1234


async def test_replies_that_do_not_match_the_request_are_not_used(tmp_path):
    """Immediate but wrong replies: an exception for another function code after a
    write, and the wrong number of registers for a read."""

    class WrongReplies(RtuDevice):
        def plan(self, fc, address, value):
            if fc == 6:
                self.hr[address] = value  # applied
                return 0.0, bytes([0x83, 0x02])  # an exception for FC 3, not FC 6
            if fc == 3:
                return 0.0, struct.pack(">BBH", 3, 2, 7)  # always one register
            return super().plan(fc, address, value)

    async with WrongReplies() as device:
        target = SimpleNamespace(host="127.0.0.1", port=device.port)
        env = {**rtu_env(device), "MODBUS_MAX_RETRIES": "1"}
        async with server(target, tmp_path, **env) as srv:
            write = await srv.call("write_register", address=7, value=1234)
            two = await srv.call("read_multiple_holding_registers", address=0, count=2)
            one = await srv.call("read_register", address=0)
    assert write["success"] is False
    assert write["meta"]["outcome"] == "unknown", write  # not "rejected": it was applied
    assert write["meta"]["reply_mismatch"]["function_code"]["got"] == 0x83
    assert device.frames(6) == 1 and device.hr[7] == 1234
    assert two["success"] is False
    assert two["error"] == "the device's reply does not match the request"
    assert two["meta"]["reply_mismatch"]["registers"] == {"expected": 2, "got": 1}
    assert one["success"] is True and one["data"] == {"value": 7}  # one register is right
