# Modbus Mock Server

A lightweight Modbus TCP mock server to test Modbus clients (e.g., the MCP server in [`../modbus-python`](../modbus-python/README.md)), part of [MODBUS-MCP](../README.md).

## Features

- Simulated process values updated every second
- Writable setpoints/controls via holding registers and coils
- Read-only sensors via input registers and discrete inputs
- Simple command registers to start/stop/reset/emergency-stop
- Device identification (function 43 / MEI 14)
- Defaults to TCP port `1502` (non-root)

## Register Map (unit/slave ID: any)

Each table (coils, discrete inputs, holding registers, input registers) holds 100 entries, addresses 0-99; a request beyond that gets Modbus exception 2 (Illegal Data Address). The addresses not listed below are unused and start at 0, so they are free for experiments. The values listed are the ones the mock starts with.

- Coils (RW)
  - `00001` (addr 0): Pump Enabled (starts off)
  - `00002` (addr 1): Alarm Active (set by simulation; write ignored)

- Discrete Inputs (RO)
  - `10001` (addr 0): Emergency Stop Active
  - `10002` (addr 1): System Running

- Holding Registers (RW)
  - `40001` (addr 0): Valve Position (0-100), starts at 50
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
  - `30001` (addr 0): Temperature x10 (e.g., 253 = 25.3°C), starts near 250
  - `30002` (addr 1): Pressure (hPa), starts near 1013
  - `30003` (addr 2): Flow Rate (L/min)
  - `30004` (addr 3): Tank Level x10 (0-1000), starts near 750
  - `30005` (addr 4): Vibration x100
  - `30006` (addr 5): pH Level x100, starts near 720
  - `30007` (addr 6): Humidity x10, starts near 450
  - `30008` (addr 7): Motor Speed (RPM)
  - `30009` (addr 8): Total Production (units, 16-bit, wraps)

Notes: Addresses shown as Modbus-style 1-based with zero-based in parentheses. The command registers are cleared once the simulation has acted on them (within one update interval).

## Install

Requires Python 3.10+. From the repository root:

```bash
cd modbus-mock-server
uv sync  # installs deps (pinned in uv.lock) and entry points
```

`.python-version` asks for Python 3.10, so `uv sync` uses a 3.10 it finds or downloads one. `uv.lock` pins pymodbus 3.12.1; see [Known issues](#known-issues) for the supported range.

## Run

```bash
# MODBUS_HOST defaults to 0.0.0.0 (every interface); 127.0.0.1 keeps the mock on this machine
MODBUS_HOST=127.0.0.1 uv run modbus-mock-server
# or
MODBUS_HOST=127.0.0.1 uv run python -m modbus_local_server
```

Environment overrides (read when the mock starts):
- `MODBUS_HOST` (default: `0.0.0.0`; use `127.0.0.1` to keep the mock off your network)
- `MODBUS_PORT` (default: `1502`)
- `UPDATE_INTERVAL` seconds (default: `1.0`)

The mock logs to stderr. If a simulation step fails, it logs the error with a traceback (once per distinct error) and keeps running.

`--exit-on-stdin-eof` makes the mock stop when its stdin is closed. Test harnesses use it with a pipe they hold, so the mock cannot outlive the process that started it (the modbus-python tests do); run by hand, leave it out.

## Test with modbus-mcp

The MCP server in [`modbus-python/`](../modbus-python/README.md) is a stdio server, so let an MCP client launch it rather than running it in a terminal (by hand it prints nothing and waits for a client on stdin). For example, with the [MCP Inspector](https://modelcontextprotocol.io/docs/tools/inspector), from the repository root:

```bash
(cd modbus-python && uv sync)
npx @modelcontextprotocol/inspector \
  -e MODBUS_HOST=127.0.0.1 -e MODBUS_PORT=1502 -e MODBUS_WRITES_ENABLED=true \
  uv --directory modbus-python run modbus-mcp
```

`MODBUS_WRITES_ENABLED=true` lets the server write to the mock; leave it out to try the server read-only, its default.

Then try these tools from the client:
- `read_register(address=0)` → Valve Position (50)
- `write_register(address=1, value=50)` → Heater Power = 50% (the temperature rises towards 50°C)
- `read_input_registers(address=0, count=4)` → Temperature/Pressure/Flow/Tank Level
- `write_register(address=4, value=10)`, then `write_register(address=6, value=1)` → production setpoint 10/h, then Start command (AUTO mode, pump on, System Running)

The modbus-python test suite runs the same kind of checks automatically: `uv run pytest` in `modbus-python` starts this mock on a free port.

## Known issues

- **Only pymodbus 3.11.1 to 3.12 can run it.** It is built on `ModbusDeviceContext`/`ModbusServerContext`, which appeared in pymodbus 3.10. pymodbus 3.10.0 and 3.11.0 answer reads past the 100-entry tables with short replies instead of exception 2, and pymodbus 3.13 replaced this datastore API (the mock fails at startup). `pyproject.toml` therefore asks for `pymodbus>=3.11.1,<3.13` (tested: 3.11.1-3.11.4, 3.12.0, 3.12.1; CI starts it on 3.11.1), and `uv.lock` pins 3.12.1. Moving to pymodbus' newer `SimDevice` API would lift the upper bound. pymodbus 3.10+ needs Python 3.10+, so the project requires Python 3.10+ too.

Device identity served: VendorName `Mock ICS`, ProductCode `MODBUS-MOCK`, MajorMinorRevision `0.1`.

## Troubleshooting

- Port in use → change `MODBUS_PORT`
- Permission denied on 502 → use 1502 (non-root)
- No response → confirm firewall, host/port, and that `modbus-mcp` points to this server
