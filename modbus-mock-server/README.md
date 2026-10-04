# Modbus Mock Server

A lightweight Modbus TCP mock server to test Modbus clients (e.g., the MCP server in [`../modbus-python`](../modbus-python/README.md)), part of [MODBUS-MCP](../README.md).

> [!WARNING]
> The mock currently answers register, coil and discrete-input requests with Modbus exception 4 instead of the values below — see [Known issues](#known-issues).

## Features

- Simulated process values updated every second
- Writable setpoints/controls via holding registers and coils
- Read-only sensors via input registers and discrete inputs
- Simple command registers to start/stop/reset/emergency-stop
- Defaults to TCP port `1502` (non-root)

## Register Map (unit/slave ID: any)

- Coils (RW)
  - `00001` (addr 0): Pump Enabled
  - `00002` (addr 1): Alarm Active (set by simulation; write ignored)

- Discrete Inputs (RO)
  - `10001` (addr 0): Emergency Stop Active
  - `10002` (addr 1): System Running

- Holding Registers (RW)
  - `40001` (addr 0): Valve Position (0-100)
  - `40002` (addr 1): Heater Power (0-100)
  - `40003` (addr 2): Fan Speed (0-100)
  - `40004` (addr 3): Conveyor Speed (0-100)
  - `40005` (addr 4): Production Rate Setpoint (units/hour)
  - `40006` (addr 5): System Mode (0=MANUAL,1=AUTO,2=MAINT)
  - `40007` (addr 6): Command Start (write >0 to start)
  - `40008` (addr 7): Command Stop (write >0 to stop)
  - `40009` (addr 8): Command Emergency Stop (write >0)
  - `40010` (addr 9): Command Reset (write >0)

- Input Registers (RO, scaled where noted)
  - `30001` (addr 0): Temperature x10 (e.g., 253 = 25.3°C)
  - `30002` (addr 1): Pressure (hPa)
  - `30003` (addr 2): Flow Rate (L/min)
  - `30004` (addr 3): Tank Level x10 (0-1000)
  - `30005` (addr 4): Vibration x100
  - `30006` (addr 5): pH Level x100
  - `30007` (addr 6): Humidity x10
  - `30008` (addr 7): Motor Speed (RPM)
  - `30009` (addr 8): Total Production (units, 16-bit, wraps)

Notes: Addresses shown as Modbus-style 1-based with zero-based in parentheses.

## Install

Requires Python 3.10+. From the repository root:

```bash
cd modbus-mock-server
uv sync  # installs deps (pinned in uv.lock) and entry points
```

`.python-version` asks for Python 3.10, so `uv sync` uses a 3.10 it finds or downloads one.

## Run

```bash
# MODBUS_HOST defaults to 0.0.0.0 (every interface); 127.0.0.1 keeps the mock on this machine
MODBUS_HOST=127.0.0.1 uv run modbus-mock-server
# or
MODBUS_HOST=127.0.0.1 uv run python -m modbus_local_server
```

Environment overrides:
- `MODBUS_HOST` (default: `0.0.0.0`; use `127.0.0.1` to keep the mock off your network)
- `MODBUS_PORT` (default: `1502`)
- `UPDATE_INTERVAL` seconds (default: `1.0`)

## Test with modbus-mcp

The MCP server in [`modbus-python/`](../modbus-python/README.md) is a stdio server, so let an MCP client launch it rather than running it in a terminal (by hand it prints nothing and waits for a client on stdin). For example, with the [MCP Inspector](https://modelcontextprotocol.io/docs/tools/inspector), from the repository root:

```bash
(cd modbus-python && uv sync)
npx @modelcontextprotocol/inspector \
  -e MODBUS_HOST=127.0.0.1 -e MODBUS_PORT=1502 \
  uv --directory modbus-python run modbus-mcp
```

Then try these tools from the client:
- `read_register(address=0)` → Valve Position
- `write_register(address=1, value=50)` → Heater Power = 50%
- `read_input_registers(address=0, count=4)` → Temperature/Pressure/Flow/Tank Level
- `write_register(address=6, value=1)` → Start command (AUTO mode + pump on)

## Known issues

These are bugs in `modbus_local_server.py`, documented here until they are fixed:

- **Every register, coil and discrete-input request fails.** `build_context()` passes `devices={0x00: device}` together with `single=True`, and pymodbus wraps that dict again, so the datastore lookup returns a `dict` instead of the device context. The server logs `'dict' object has no attribute 'async_getValues'` and answers with exception 4 (server device failure); the simulation loop fails the same way and silently retries. Device identification (`read_device_information`) is unaffected and returns the identity below.
- **Initial values are one register off.** `build_context()` seeds the data blocks at index 0, but pymodbus maps protocol address 0 to block index 1, so e.g. Valve Position would start at 0 rather than 50.
- **Only pymodbus 3.10–3.12 can start it.** It imports `ModbusDeviceContext` (new in 3.10), and pymodbus 3.13+ rejects `ModbusSequentialDataBlock(0, ...)` at startup. `uv.lock` therefore pins pymodbus 3.12.1. pymodbus 3.10+ needs Python 3.10+, so the project requires Python 3.10+ too.

Device identity served: VendorName `Mock ICS`, ProductCode `MODBUS-MOCK`, MajorMinorRevision `0.1`.

## Troubleshooting

- Port in use → change `MODBUS_PORT`
- Permission denied on 502 → use 1502 (non-root)
- No response → confirm firewall, host/port, and that `modbus-mcp` points to this server
