> **0.3.0 package and tool migration:** Install `industriagents-modbus-mcp`
> from npm (requires uv) or PyPI; the command remains `modbus-mcp`.
> The advertised catalog now contains **6 tools**. Older tool names in
> the examples below remain compatibility calls. See the
> [architecture and canonical tool guide](docs/architecture.md)
> and [release guide](docs/release.md).

# Modbus MCP Server

[![CI](https://github.com/IndustriAgents/MODBUS-MCP/actions/workflows/ci.yml/badge.svg)](https://github.com/IndustriAgents/MODBUS-MCP/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)
[![MCP](https://img.shields.io/badge/MCP-compatible-purple)](https://modelcontextprotocol.io)

A Model Context Protocol (MCP) server for Modbus that lets AI agents and MCP‑compatible apps read, and when you allow it write, Modbus devices over TCP, UDP, or Serial (RTU). This repo contains:

- modbus-python: Python MCP server (uv managed)
- modbus-mock-server: Lightweight Modbus TCP mock device for local testing

It is part of the [IndustriConnect](https://github.com/IndustriAgents/IndustriConnect) suite of industrial-protocol MCP servers — see [Part of IndustriConnect](#part-of-industriconnect).

## Features

- Read-only by default: the six write tools refuse to send anything unless the server is started with `MODBUS_WRITES_ENABLED=true`, taken only from the process environment or an explicit `--env-file`, never from the automatically loaded `.env`
- Writes are sent at most once: if the reply is lost, the result says so (`"no reply; the write may have been applied; check before retrying"`, `meta.outcome: "unknown"`) and nothing is sent again
- Core Modbus tools: coils, discrete inputs, holding/input registers; masked write; device identification
- Typed decode/encode: int16/uint16/int32/uint32/float32/int64/uint64/float64 with byteorder/wordorder, scale, offset
- Tag map: optional JSON map of named points with `list_tags`, `read_tag`, `write_tag`, validated when the server starts
- Reliability: reads retried with backoff, an overall deadline per tool call (`MODBUS_CALL_TIMEOUT`), automatic chunking of large reads; starts without the device and connects on first use, reconnects after a drop
- Replies are checked against their request (function code, count, echoed values), and after a request goes unanswered the connection is reset (on a serial line after a quiet period of `MODBUS_TIMEOUT`), so a late reply is not taken for another request's (Modbus RTU replies carry no transaction id). On a serial line, a reply later than twice `MODBUS_TIMEOUT` can still be mistaken; set `MODBUS_TIMEOUT` above the device's worst-case response time
- Tool annotations: write tools are marked destructive and not idempotent, read tools read-only, so MCP clients do not treat a write as safe to retry
- Health: `ping` sends a real Modbus request and reports whether the device answered, plus the configuration
- Structured results: every tool returns `{ success, data, error, meta }`, including for invalid arguments and device exceptions; arguments must be JSON integers and booleans of the right type (no `"5"`, `5.0` or `1` for `true`)

## Repo Layout

```text
MODBUS-MCP/
├── modbus-python/        # Python MCP server (uv project, `modbus-mcp` entry point, tests/)
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

Each of `modbus-python/` and `modbus-mock-server/` is its own uv project with a committed `uv.lock` and a `.python-version` of 3.10. `uv sync` inside it installs exactly the pinned versions, using Python 3.10 (uv downloads it if none is installed).

| Project | Locked | Supported range (in `pyproject.toml`) |
|---------|--------|----------------------------------------|
| `modbus-python` | `mcp` 1.30.0, `pymodbus` 3.15.0, `python-dotenv` 1.2.4, `pyserial` 3.5, `anyio` 4.15.1, `pydantic` 2.12.5 | `mcp>=1.22.0,<2`, `pymodbus[serial]>=3.9.2,<3.16`, `python-dotenv>=1.1.0`, `anyio>=4.5`, `pydantic>=2.8.0,<2.13` |
| `modbus-mock-server` | `pymodbus` 3.12.1 | `pymodbus>=3.11.1,<3.13` |

CI runs the server's full unit, stdio and mock integration suite on the lockfile and on the lowest resolvable direct dependencies. Version 0.3.0 requires MCP 1.22 or later for direct `CallToolResult` error responses and targets MCP 1.x. Pydantic is capped below 2.13 because the supported MCP floor depends on typing helpers removed there. The mock's limits are explained in its [Known issues](modbus-mock-server/README.md#known-issues).

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

This starts the server read-only. To try the write tools against the mock, add `-e MODBUS_WRITES_ENABLED=true`. Or add the server to Claude Desktop as shown [below](#claude-desktop).

The server starts even when the device cannot be reached (it waits at most 1 s for it at startup): `initialize` and `tools/list` work, and the device tools return `success: false` with `"cannot reach device at <host>:<port>"` until it answers. Every call tries to connect, so the mock can be started before or after the server, and a dropped connection is re-established the same way.

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

That configuration is read-only. Add `"MODBUS_WRITES_ENABLED": "true"` to `env` only when the model should be able to change the device, or keep the settings in a file and pass it explicitly: `"args": [..., "modbus-mcp", "--env-file", "/absolute/path/to/plant.env"]`. The `.env` the server loads by itself cannot enable writes (see [Environment Variables](#environment-variables)).

### MCP Inspector / CLI

The Inspector command in [Quick Start](#quick-start-local) step 3 opens a web UI to list and call the tools. You can also use any MCP runner that connects to a stdio server. The tools are listed via MCP’s `list_tools` and invoked with `call_tool`.

## Tools and Example Calls

All tools return `{ success, data, error, meta }`. A failure, whether an invalid argument, a Modbus exception from the device, a timeout, an unreachable device, or writes being disabled, comes back as `success: false` with the reason in `error`.

The write tools also say what happened to the write, in `meta.outcome` and `meta.request_sent`:

- `written`: the device acknowledged it with a reply that echoes the request;
- `rejected`: the device refused it with a Modbus exception that, by the protocol's definition, comes before the request is executed (1 Illegal Function, 2 Illegal Data Address, 3 Illegal Data Value, 6 Server Device Busy, 10 Gateway Path Unavailable);
- `not_sent`: refused before anything was sent (writes disabled, invalid arguments, device unreachable, a local failure while encoding);
- `unknown`: it went out, and then there was no reply, a reply that does not echo the request, an exception that does not rule a write out (such as 4 Server Device Failure), or the deadline passed. The error says `"the write may have been applied; check before retrying"`; for a lost reply it is `"no reply; the write may have been applied; check before retrying"`.

A write is sent at most once. Read the value back before writing again after an `unknown`.

A write that has not gone out yet is never sent once nobody wants it any more: when the client cancels the call (`notifications/cancelled`, even if it lands in the same instant the write gets its turn), when the client disconnects (stdin closes; the server then stops every pending call and exits), or when the call's deadline passes. A last check runs right before each request is handed to the transport. Such a write is `not_sent`; a call the client cancelled gets the SDK's "Request cancelled" reply and no second one.

| Kind | Tools |
|------|-------|
| Read | `read_register`, `read_multiple_holding_registers`, `read_input_registers`, `read_coils`, `read_discrete_inputs`, `read_holding_typed`, `read_input_typed`, `read_device_information`, `list_tags`, `read_tag`, `ping` |
| Write (refused unless `MODBUS_WRITES_ENABLED=true`) | `write_register`, `write_registers`, `write_coil`, `write_coils_bulk`, `mask_write_register`, `write_tag` |
| Prompt | `analyze_register` |

Addresses are zero-based (holding register 40001 is address 0). Every example below works against the mock device, whose register map is in [modbus-mock-server/README.md](modbus-mock-server/README.md#register-map-unitslave-id-any); the write examples need `MODBUS_WRITES_ENABLED=true`.

- Read a holding register (Valve Position on the mock: 50)
```json
{ "tool": "read_register", "parameters": { "address": 0, "slave_id": 1 } }
```

- Write a holding register (Heater Power on the mock: 50 %)
```json
{ "tool": "write_register", "parameters": { "address": 1, "value": 50, "slave_id": 1 } }
```

- Read coils (chunked automatically)
```json
{ "tool": "read_coils", "parameters": { "address": 0, "count": 2, "slave_id": 1 } }
```

- Write multiple coils (coils 10-12 are unused on the mock)
```json
{ "tool": "write_coils_bulk", "parameters": { "address": 10, "values": [true, false, true], "slave_id": 1 } }
```

- Read input registers (Temperature x10, Pressure, Flow, Tank Level x10 on the mock)
```json
{ "tool": "read_input_registers", "parameters": { "address": 0, "count": 4, "slave_id": 1 } }
```

- Read discrete inputs (Emergency Stop, System Running on the mock)
```json
{ "tool": "read_discrete_inputs", "parameters": { "address": 0, "count": 2, "slave_id": 1 } }
```

- Read multiple holding registers
```json
{ "tool": "read_multiple_holding_registers", "parameters": { "address": 0, "count": 3, "slave_id": 1 } }
```

- Mask write register: set the two low bits of register 20 and keep the rest (registers 10-99 are unused on the mock)
```json
{ "tool": "mask_write_register", "parameters": { "address": 20, "and_mask": 65532, "or_mask": 3, "slave_id": 1 } }
```

- Device identification (MEI 0x2B/0x0E)
```json
{ "tool": "read_device_information", "parameters": { "slave_id": 1, "read_code": 3 } }
```

- Typed read with scaling (the mock's temperature, stored as °C x10, read in °C)
```json
{ "tool": "read_input_typed", "parameters": { "address": 0, "dtype": "uint16", "scale": 0.1, "slave_id": 1 } }
```

- Typed read with endianness (a float32 in registers 40-41, low word first)
```json
{ "tool": "read_holding_typed", "parameters": { "address": 40, "dtype": "float32", "count": 1, "byteorder": "big", "wordorder": "little", "scale": 1.0, "offset": 0.0, "slave_id": 1 } }
```

- Write multiple holding registers
```json
{ "tool": "write_registers", "parameters": { "address": 21, "values": [1, 2, 3], "slave_id": 1 } }
```

- Health
```json
{ "tool": "ping", "parameters": {} }
```

## Tag Map (Optional)

Point `REGISTER_MAP_FILE` to a JSON file to define named points. This example matches the mock device:

```json
{
  "ValvePosition": { "table": "holding", "address": 0, "unit": "%" },
  "HeaterPower": { "table": "holding", "address": 1, "unit": "%" },
  "Temperature": { "table": "input", "address": 0, "dtype": "uint16", "scale": 0.1, "unit": "°C" },
  "Setpoint": { "table": "holding", "address": 30, "dtype": "float32", "byteorder": "big", "wordorder": "big" },
  "PumpEnabled": { "table": "coil", "address": 0 },
  "Status": { "table": "discrete", "address": 0, "count": 2, "description": "emergency stop, running" }
}
```

The file is checked when the server starts; a missing file, invalid JSON, or an invalid tag (unknown table or key, address out of range, unsupported dtype) stops the server with a message saying which. The keys are described in [modbus-python/README.md](modbus-python/README.md#tag-map-optional).

- List tags:
```json
{ "tool": "list_tags", "parameters": {} }
```
- Read a tag (`Temperature` has a `dtype` and a `scale`, so this returns °C):
```json
{ "tool": "read_tag", "parameters": { "name": "Temperature" } }
```
- Write a tag (coils take true/false; holding-register tags take the same units `read_tag` returns, encoded by `dtype`):
```json
{ "tool": "write_tag", "parameters": { "name": "PumpEnabled", "value": true } }
```
```json
{ "tool": "write_tag", "parameters": { "name": "Setpoint", "value": 42.5 } }
```

## Environment Variables

Read once when the server starts, from these sources, highest precedence first:

1. the process environment (for an MCP client, the `env` block of its server configuration);
2. a file passed explicitly with `modbus-mcp --env-file PATH`;
3. `modbus-python/.env` of a source checkout, loaded automatically. It is found relative to the package source: a `.env` in the working directory (wherever the MCP client launched the server, possibly an untrusted project) or in any parent directory is never read, and an installed package has no automatic `.env` at all.

The automatically loaded `.env` is limited, because settings such as the host and the tag map decide where a write goes. It may set only modbus-mcp's own settings (`MODBUS_*` and `REGISTER_MAP_FILE`; anything else, such as `PATH` or `PYTHONPATH`, is ignored), and **not** `MODBUS_WRITES_ENABLED`, in any spelling (keys are compared case-insensitively, as Windows does, with `export` prefixes and quotes removed). Each ignored key gets a `modbus-mcp: warning: ignoring … from …` line on stderr. [modbus-python/.env.example](modbus-python/.env.example) lists every setting.

An invalid value stops the server with a `configuration error` message on stderr. An empty value means the default.

- MODBUS_TYPE: tcp | udp | serial (default: tcp)
- MODBUS_HOST: host for TCP/UDP (default: 127.0.0.1)
- MODBUS_PORT: port for TCP/UDP (default: 502; mock uses 1502)
- MODBUS_DEFAULT_SLAVE_ID: default unit id, 0-255 (default: 1)
- MODBUS_SERIAL_PORT: serial device path (default: /dev/ttyUSB0)
- MODBUS_BAUDRATE: serial baud (default: 9600)
- MODBUS_PARITY: N | E | O (default: N)
- MODBUS_STOPBITS: 1 | 2 (default: 1)
- MODBUS_BYTESIZE: 5-8 data bits (default: 8)
- MODBUS_TIMEOUT: serial timeout in seconds (default: 1; only used when MODBUS_TYPE=serial; TCP/UDP wait up to 3 s for a reply, pymodbus' default)
- MODBUS_MAX_RETRIES: retries after a failed read or a failed connection attempt, 0-10 (default: 2); a write is never sent twice
- MODBUS_RETRY_BACKOFF_BASE: backoff seconds, doubled per retry (default: 0.2)
- MODBUS_TOOL_TIMEOUT: per‑request timeout in seconds (default: unset or 0 = none; TCP/UDP requests still end after pymodbus' 3 s)
- MODBUS_CALL_TIMEOUT: overall deadline for one tool call in seconds (default: 30). It starts when the call starts and covers everything the call does: waiting behind other calls, reconnecting, and every request it makes. When it passes, a read fails cleanly, a write that went out is `unknown`, and a write still waiting is `not_sent` and never sent
- MODBUS_WRITES_ENABLED: true/false to allow writes (default: **false**; only from the process environment or `--env-file`)
- REGISTER_MAP_FILE: path to tag‑map JSON (optional)

## Testing With The Mock Server

- Open [modbus-mock-server/README.md](modbus-mock-server/README.md) for register map and controls.
- Common smoke tests against the mock (the writes need `MODBUS_WRITES_ENABLED=true`):
  - `read_register(address=0)` → Valve Position (50)
  - `write_register(address=1, value=50)` → Heater Power = 50%
  - `read_input_registers(address=0, count=4)` → Temperature/Pressure/Flow/Tank Level
  - `write_register(address=6, value=1)` → Start command

## Development

- Python
  - Requires Python 3.10+ and uv
  - From `modbus-python`: `uv sync`, then launch it from an MCP client (see [Quick Start](#quick-start-local) step 3)
  - Tests: `uv run pytest` in `modbus-python` runs the unit tests and the integration tests, which start the mock (with `uv`) and drive the real server over stdio with the MCP client SDK
  - Lint and format: `uv run ruff check` and `uv run ruff format --check`, in either project
- CI ([.github/workflows/ci.yml](.github/workflows/ci.yml)) runs ruff and the test suite on the locked versions, the test suite again on the lowest version of every direct dependency, and the mock's own checks, including starting it on its lowest allowed pymodbus.
- How to contribute, test a change and open a pull request: [CONTRIBUTING.md](CONTRIBUTING.md).

## Troubleshooting

- Permission denied on port 502 → use non‑root port 1502
- No response → verify firewall and MODBUS_HOST/MODBUS_PORT; ensure server connects to mock or device
- `cannot reach device at …` → nothing answers at MODBUS_HOST:MODBUS_PORT; start the mock or check the address (the server keeps trying on every call)
- `no reply; the write may have been applied; check before retrying` → the write went out but its reply was lost; read the value back before deciding to write again
- `modbus-mcp: warning: ignoring MODBUS_WRITES_ENABLED from …/.env` → set it in the MCP client's `env` block, or pass the file with `--env-file`
- `no result within the 30 s deadline (MODBUS_CALL_TIMEOUT)` → the device answered too slowly, or with something pymodbus could not decode; raise `MODBUS_CALL_TIMEOUT` if the device is just slow
- `modbus-mcp: configuration error: …` → a `MODBUS_*` variable or the tag map is invalid; the message names it
- `uv run modbus-mcp` prints nothing → expected: it is a stdio server waiting for an MCP client on stdin; launch it from a client instead (see [Quick Start](#quick-start-local) step 3)
- `Writes are disabled by configuration` → writes are off by default; set `MODBUS_WRITES_ENABLED=true` if the model should be allowed to write
- `Modbus exception 2 (Illegal Data Address)` → the device has no such register/coil; on the mock, every table holds addresses 0-99
- Serial issues → confirm port name, baud, parity, stop bits, and device permissions

## Security

Modbus has no authentication or encryption: anything that can reach the device can read and write it. This server is read-only unless it is started with `MODBUS_WRITES_ENABLED=true` in its environment or `--env-file` (the `.env` it loads by itself cannot enable writes); enable writes only when the model should be able to change the device, start against the mock, and never point it at a safety system. Each write is sent at most once. See [SECURITY.md](SECURITY.md) for the full posture and how to report a vulnerability privately.

## Part of IndustriConnect

This server is one of the protocol servers in [IndustriConnect](https://github.com/IndustriAgents/IndustriConnect), a suite of MCP servers for industrial automation protocols (Modbus, MQTT/Sparkplug B, OPC UA, BACnet, DNP3, EtherCAT, EtherNet/IP, PROFIBUS, PROFINET, S7comm) that share one response envelope and come with a mock device each.

IndustriConnect includes this repository as its `MODBUS-Project` git submodule, tracking `main`. Development happens here: open issues and pull requests for the Modbus server or mock against [IndustriAgents/MODBUS-MCP](https://github.com/IndustriAgents/MODBUS-MCP), not IndustriConnect.

## Contributing

Contributions are welcome — see [CONTRIBUTING.md](CONTRIBUTING.md). Questions and usage help: [Discussions](https://github.com/IndustriAgents/MODBUS-MCP/discussions). Security issues: report privately via [a security advisory](https://github.com/IndustriAgents/MODBUS-MCP/security/advisories/new), not an issue. By taking part you agree to the [Code of Conduct](CODE_OF_CONDUCT.md).

## License

MIT — see [LICENSE](LICENSE). `modbus-python/` is derived from [kukapay/modbus-mcp](https://github.com/kukapay/modbus-mcp) (MIT), so the license names two copyright holders: IndustriAgents, and kukapay for the portions taken from it. kukapay's original license text is kept in [modbus-python/LICENSE-kukapay](modbus-python/LICENSE-kukapay).
