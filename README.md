# Modbus MCP Server

[![CI](https://github.com/IndustriAgents/MODBUS-MCP/actions/workflows/ci.yml/badge.svg)](https://github.com/IndustriAgents/MODBUS-MCP/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)
[![MCP](https://img.shields.io/badge/MCP-compatible-purple)](https://modelcontextprotocol.io)

A Model Context Protocol (MCP) server for Modbus that lets AI agents and MCP‑compatible apps read and write Modbus devices over TCP, UDP, or Serial (RTU). This repo contains:

- modbus-python: Python MCP server (uv managed)
- modbus-mock-server: Lightweight Modbus TCP mock device for local testing

It is part of the [IndustriConnect](https://github.com/IndustriAgents/IndustriConnect) suite of industrial-protocol MCP servers — see [Part of IndustriConnect](#part-of-industriconnect).

## Features

- Core Modbus tools: coils, discrete inputs, holding/input registers; masked write; device info
- Typed decode/encode: int16/uint16/int32/uint32/float32/int64/uint64/float64 with byteorder/wordorder, scale, offset (currently broken, see the warning below)
- Reliability: retries with backoff, per‑tool timeouts, automatic chunking
- Tag map: optional JSON map for named points with `list_tags`, `read_tag`, `write_tag`
- Health: `ping` reports status and configuration
- Structured results: uniform `{ success, data, error, meta }` responses

> [!WARNING]
> Typed values currently fail on every call. `read_holding_typed`, `read_input_typed`, `read_tag` on a tag with a `dtype`, and `write_tag` on a holding-register tag return `{"success": false, "error": "Big"}`, because `modbus-python/src/modbus_mcp/cli.py` uses `Endian.Big`/`Endian.Little` and the pinned pymodbus 3.9.2 names them `Endian.BIG`/`Endian.LITTLE`. The raw register, coil and discrete-input tools are unaffected. Details: [server Known issues](modbus-python/README.md#known-issues).

## Repo Layout

```text
MODBUS-MCP/
├── modbus-python/        # Python MCP server (uv project, `modbus-mcp` entry point)
├── modbus-mock-server/   # Local Modbus TCP simulator (uv project, default port 1502)
└── .github/              # CI, issue and pull request templates
```

Inside IndustriConnect this repository is mounted as the `MODBUS-Project/` submodule, so the same tree appears there under that name.

## Install

Requires Python 3.10+ and [uv](https://docs.astral.sh/uv/).

```bash
git clone https://github.com/IndustriAgents/MODBUS-MCP.git
cd MODBUS-MCP
```

Each of `modbus-python/` and `modbus-mock-server/` is its own uv project with a committed `uv.lock` and a `.python-version` of 3.10. `uv sync` inside it installs exactly the pinned versions, using Python 3.10 (uv downloads it if none is installed). Use the lockfile: the server runs on the versions its `uv.lock` pins (`mcp` 1.6.0, `pymodbus` 3.9.2), while the ranges in `pyproject.toml` also admit releases it does not work with (`mcp` 2.x, `pymodbus` 3.10+).

## Quick Start (Local)

1) Start the mock Modbus device (from the repository root)

```bash
cd modbus-mock-server
uv sync
# MODBUS_HOST defaults to 0.0.0.0 (every interface); 127.0.0.1 keeps the mock on this machine
MODBUS_HOST=127.0.0.1 uv run modbus-mock-server
```

2) Install the MCP server (Python) in a second terminal, also from the repository root

```bash
cd modbus-python
uv sync
```

3) Connect an MCP client to it

The MCP server talks to its client over stdio and to the device (here, the mock) over Modbus TCP, so an MCP client launches it. Started by hand with `uv run modbus-mcp`, it prints nothing and waits for JSON-RPC on stdin. To try it out, let the [MCP Inspector](https://modelcontextprotocol.io/docs/tools/inspector) launch it. Run this from the repository root (it needs Node.js):

```bash
npx @modelcontextprotocol/inspector \
  -e MODBUS_HOST=127.0.0.1 -e MODBUS_PORT=1502 \
  uv --directory modbus-python run modbus-mcp
```

Or add the server to Claude Desktop as shown [below](#claude-desktop).

The server tries to connect to the device at startup. If the device is unreachable, it exits with `RuntimeError: Failed to connect to Modbus tcp device` once a client connects, so start the mock first.

## MCP Client Examples

### Claude Desktop

Python server (uv):
```json
{
  "mcpServers": {
    "Modbus MCP (Python)": {
      "command": "uv",
      "args": ["--directory", "/absolute/path/to/MODBUS-MCP/modbus-python", "run", "modbus-mcp"],
      "env": { "MODBUS_TYPE": "tcp", "MODBUS_HOST": "127.0.0.1", "MODBUS_PORT": "1502", "MODBUS_DEFAULT_SLAVE_ID": "1" }
    }
  }
}
```

### MCP Inspector / CLI

The Inspector command in [Quick Start](#quick-start-local) step 3 opens a web UI to list and call the tools. You can also use any MCP runner that connects to a stdio server. The tools are listed via MCP’s `list_tools` and invoked with `call_tool`.

## Tools and Example Calls

All tools return `{ success, data, error, meta }`.

| Kind | Tools |
|------|-------|
| Read | `read_register`, `read_multiple_holding_registers`, `read_input_registers`, `read_coils`, `read_discrete_inputs`, `read_holding_typed`, `read_input_typed`, `read_device_information`, `list_tags`, `read_tag`, `ping` |
| Write (allowed unless `MODBUS_WRITES_ENABLED=false`) | `write_register`, `write_registers`, `write_coil`, `write_coils_bulk`, `mask_write_register`, `write_tag` |
| Prompt | `analyze_register` |

- Read a holding register
```json
{ "tool": "read_register", "parameters": { "address": 0, "slave_id": 1 } }
```

- Write a holding register
```json
{ "tool": "write_register", "parameters": { "address": 5, "value": 42, "slave_id": 1 } }
```

- Read coils (chunked automatically)
```json
{ "tool": "read_coils", "parameters": { "address": 0, "count": 8, "slave_id": 1 } }
```

- Write multiple coils
```json
{ "tool": "write_coils_bulk", "parameters": { "address": 0, "values": [true, false, true], "slave_id": 1 } }
```

- Read input registers
```json
{ "tool": "read_input_registers", "parameters": { "address": 0, "count": 4, "slave_id": 1 } }
```

- Read discrete inputs
```json
{ "tool": "read_discrete_inputs", "parameters": { "address": 0, "count": 8, "slave_id": 1 } }
```

- Read multiple holding registers
```json
{ "tool": "read_multiple_holding_registers", "parameters": { "address": 0, "count": 3, "slave_id": 1 } }
```

- Mask write register
```json
{ "tool": "mask_write_register", "parameters": { "address": 100, "and_mask": 65535, "or_mask": 3, "slave_id": 1 } }
```

- Device identification (MEI 0x2B/0x0E)
```json
{ "tool": "read_device_information", "parameters": { "slave_id": 1, "read_code": 3 } }
```

- Typed read (endianness + scaling); currently fails with `"Big"`, see the warning under [Features](#features)
```json
{ "tool": "read_holding_typed", "parameters": { "address": 200, "dtype": "float32", "count": 2, "byteorder": "little", "wordorder": "big", "scale": 1.0, "offset": 0.0, "slave_id": 1 } }
```

- Write multiple holding registers
```json
{ "tool": "write_registers", "parameters": { "address": 50, "values": [1, 2, 3], "slave_id": 1 } }
```

- Health
```json
{ "tool": "ping", "parameters": {} }
```

## Tag Map (Optional)

Point `REGISTER_MAP_FILE` to a JSON file to define named points. Example:

```json
{
  "PumpSpeed": { "table": "holding", "address": 100, "dtype": "float32", "count": 1, "byteorder": "big", "wordorder": "big", "slave_id": 1 },
  "ValveOpen": { "table": "coil", "address": 5, "slave_id": 1 },
  "Alarm": { "table": "discrete", "address": 10, "count": 4, "slave_id": 1 }
}
```

- List tags:
```json
{ "tool": "list_tags", "parameters": {} }
```
- Read a tag (`PumpSpeed` has a `dtype`, so this typed read currently fails; see the warning under [Features](#features)):
```json
{ "tool": "read_tag", "parameters": { "name": "PumpSpeed" } }
```
- Write a tag (coils use booleans; holding uses values encoded by dtype, which currently fails for every holding-register tag):
```json
{ "tool": "write_tag", "parameters": { "name": "ValveOpen", "value": true } }
```

## Environment Variables

- MODBUS_TYPE: tcp | udp | serial (default: tcp)
- MODBUS_HOST: host for TCP/UDP (default: 127.0.0.1)
- MODBUS_PORT: port for TCP/UDP (default: 502; mock uses 1502)
- MODBUS_DEFAULT_SLAVE_ID: default unit id (default: 1)
- MODBUS_SERIAL_PORT: serial device path (default: /dev/ttyUSB0)
- MODBUS_BAUDRATE: serial baud (default: 9600)
- MODBUS_PARITY: N | E | O (default: N)
- MODBUS_STOPBITS: stop bits (default: 1)
- MODBUS_BYTESIZE: data bits (default: 8)
- MODBUS_TIMEOUT: serial timeout in seconds (default: 1; only used when MODBUS_TYPE=serial)
- MODBUS_MAX_RETRIES: retry attempts (default: 2)
- MODBUS_RETRY_BACKOFF_BASE: backoff seconds (default: 0.2)
- MODBUS_TOOL_TIMEOUT: per‑tool timeout (seconds, optional)
- MODBUS_WRITES_ENABLED: true/false to allow writes (default: true)
- REGISTER_MAP_FILE: path to tag‑map JSON (optional)

## Testing With The Mock Server

- Open [modbus-mock-server/README.md](modbus-mock-server/README.md) for register map and controls.
- Common smoke tests against the mock:
  - `read_register(address=0)` → Valve Position
  - `write_register(address=1, value=50)` → Heater Power = 50%
  - `read_input_registers(address=0, count=4)` → Temperature/Pressure/Flow/Tank Level
  - `write_register(address=6, value=1)` → Start command

> [!WARNING]
> The mock currently answers every register, coil and discrete-input request with Modbus exception 4 (server device failure), so the register smoke tests above return `success: false` until it is fixed. `ping`, `list_tags` and `read_device_information` do work against it. Details: [mock Known issues](modbus-mock-server/README.md#known-issues).

## Development

- Python
  - Requires Python 3.10+ and uv
  - From `modbus-python`: `uv sync`, then launch it from an MCP client (see [Quick Start](#quick-start-local) step 3)
- CI ([.github/workflows/ci.yml](.github/workflows/ci.yml)) runs device-free checks on both projects: `uv sync --locked`, byte-compilation, importing the server and listing its registered tools, and building the mock's datastore.
- How to contribute, test a change and open a pull request: [CONTRIBUTING.md](CONTRIBUTING.md).

## Troubleshooting

- Permission denied on port 502 → use non‑root port 1502
- No response → verify firewall and MODBUS_HOST/MODBUS_PORT; ensure server connects to mock or device
- `RuntimeError: Failed to connect to Modbus tcp device` when a client connects → nothing is listening at MODBUS_HOST:MODBUS_PORT; start the mock or check the address
- `uv run modbus-mcp` prints nothing → expected: it is a stdio server waiting for an MCP client on stdin; launch it from a client instead (see [Quick Start](#quick-start-local) step 3)
- Writes rejected → set `MODBUS_WRITES_ENABLED=true`
- Serial issues → confirm port name, baud, parity, stop bits, and device permissions

## Security

Modbus has no authentication or encryption: anything that can reach the device can read and write it. This server allows writes unless `MODBUS_WRITES_ENABLED=false`, so set that for read-only use, start against the mock, and never point it at a safety system. See [SECURITY.md](SECURITY.md) for the full posture and how to report a vulnerability privately.

## Part of IndustriConnect

This server is one of the protocol servers in [IndustriConnect](https://github.com/IndustriAgents/IndustriConnect), a suite of MCP servers for industrial automation protocols (Modbus, MQTT/Sparkplug B, OPC UA, BACnet, DNP3, EtherCAT, EtherNet/IP, PROFIBUS, PROFINET, S7comm) that share one response envelope and come with a mock device each.

IndustriConnect includes this repository as its `MODBUS-Project` git submodule, tracking `main`. Development happens here: open issues and pull requests for the Modbus server or mock against [IndustriAgents/MODBUS-MCP](https://github.com/IndustriAgents/MODBUS-MCP), not IndustriConnect.

## Contributing

Contributions are welcome — see [CONTRIBUTING.md](CONTRIBUTING.md). By taking part you agree to the [Code of Conduct](CODE_OF_CONDUCT.md).

## License

MIT — see [LICENSE](LICENSE). `modbus-python/` is derived from [kukapay/modbus-mcp](https://github.com/kukapay/modbus-mcp) (MIT), so the license names two copyright holders: IndustriAgents, and kukapay for the portions taken from it. kukapay's original license text is kept in [modbus-python/LICENSE-kukapay](modbus-python/LICENSE-kukapay).
