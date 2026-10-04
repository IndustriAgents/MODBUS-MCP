# Modbus MCP Server (Python)

The MCP server of [MODBUS-MCP](../README.md). It is a [uv](https://docs.astral.sh/uv/) project with the `modbus-mcp` entry point; the mock device to test it against lives in [`../modbus-mock-server`](../modbus-mock-server/README.md).

## Features

- **Read-only by default**: the write tools (`write_register`, `write_registers`, `write_coil`, `write_coils_bulk`, `mask_write_register`, `write_tag`) send nothing unless `MODBUS_WRITES_ENABLED=true`.
- **Core tools**:
  - Read/write holding registers: `read_register`, `write_register`, `read_multiple_holding_registers`, `write_registers`.
  - Read/write coils: `read_coils`, `write_coil`, `write_coils_bulk`.
  - Read input registers: `read_input_registers`.
  - Read discrete inputs: `read_discrete_inputs`.
  - Bitwise masked write: `mask_write_register`.
  - Device information (MEI 0x2B/0x0E): `read_device_information`.
- **Typed reads**:
  - `read_holding_typed`, `read_input_typed` for `int16/uint16/int32/uint32/int64/uint64/float32/float64` with byte/word endianness, scale, and offset.
  - Encoding and decoding are done in `src/modbus_mcp/codec.py` with the same register layout pymodbus' former `BinaryPayloadBuilder`/`Decoder` used (a test checks this against them on pymodbus 3.9).
- **Tag map (optional)**:
  - Load a JSON register map via `REGISTER_MAP_FILE` and use `list_tags`, `read_tag`, `write_tag`. Typed tags are decoded and scaled on read; `write_tag` takes the same engineering units and reverses the scale and offset.
- **Reliability**:
  - Reads are retried with exponential backoff (`MODBUS_MAX_RETRIES`); per-tool timeouts; automatic chunking of reads to protocol limits (125 registers, 2000 bits per request).
  - Writes are sent at most once. pymodbus' own re-sending is switched off (`retries=0`), and a write is only retried when nothing was sent (the connection could not be opened). If a write went out and no reply came back, the tool returns `success: false`, `"no reply; the write may have been applied; check before retrying"`.
  - Starts without the device: `initialize` and `tools/list` work, device tools return `"cannot reach device at …"` until it answers, and every call tries to connect (also after a dropped connection).
- **Validation**:
  - Arguments are checked against Modbus limits before anything is sent (addresses and register values 0-65535, unit ids 0-255, at most 123 registers or 1968 coils per write, known dtypes and byte/word orders). Integers and booleans are strict: `"5"`, `5.0` or `true` is not an address, and `1` or `"on"` is not a coil state. Typed values the dtype cannot hold exactly (out of range, a fraction for an integer type) are refused rather than truncated.
- **Structured results**:
  - Every tool returns `{ success, data, error, meta }` for predictable parsing, including invalid arguments (`"error": "Invalid arguments: …"`) and device exceptions (`"error": "Modbus exception 2 (Illegal Data Address) for function 0x03"`, `meta.exception_code`).
- **Health**:
  - `ping` reads one holding register (address 0 by default) to check that the device answers. Any Modbus reply counts as reachable, including an exception reply; only no reply makes it return `success: false`. It also reports the configuration.
- **Prompt**:
  - Analyze register values with a simple prompt: `analyze_register`.

## Requirements

- **Python**: 3.10+ (uv uses 3.10 from `.python-version`)
- **uv** for dependency and virtual environment management.

## Installation

1. **Install `uv`**:
   ```bash
   curl -LsSf https://astral.sh/uv/install.sh | sh
   ```

2. **Clone the Repository**:
   ```bash
   git clone https://github.com/IndustriAgents/MODBUS-MCP.git
   cd MODBUS-MCP/modbus-python
   ```

3. **Install Dependencies**:
   ```bash
   uv sync
   ```
   This installs the versions pinned in the committed `uv.lock` (`mcp` 1.30.0, `pymodbus` 3.15.0 with `pyserial` 3.5 for serial ports) plus the dev tools (pytest, ruff). The supported ranges in `pyproject.toml` are `mcp>=1.6.0,<2`, `pymodbus[serial]>=3.9.2,<3.16` and `python-dotenv>=1.1.0`: the full test suite passed with the latest patch release of every pymodbus minor from 3.9 to 3.15 and with mcp 1.6.0, 1.9.4, 1.10.1, 1.12.4, 1.16.0, 1.20.0, 1.25.0 and 1.30.0, and CI runs it on every change with the lowest version of every direct dependency (mcp 1.6.0, pymodbus 3.9.2, python-dotenv 1.1.0, pyserial 3.5). pymodbus 3.10 renamed the unit-id keyword from `slave` to `device_id`; the server uses whichever the installed version takes. mcp 2.x removed `mcp.server.fastmcp` and is not supported.

## Configuration

The server connects to a Modbus device using parameters specified via environment variables. They are read once, when the server starts, from these sources, highest precedence first:

1. the process environment (for an MCP client, the `env` block of its server configuration);
2. a file passed explicitly with `modbus-mcp --env-file PATH`;
3. a `.env` found automatically, by searching up from the installed package's directory (for this checkout, `modbus-python/.env`).

**A `.env` found automatically cannot enable writes.** It may set connection settings, but `MODBUS_WRITES_ENABLED` is only honoured from (1) or (2): when an MCP client launches the server straight from a virtualenv, the search can reach the client's own project, which may not be trusted. If an automatically found `.env` sets it, the server ignores it and prints `modbus-mcp: warning: ignoring MODBUS_WRITES_ENABLED from <file>` on stderr. [`.env.example`](.env.example) lists every setting.

The configuration is checked when it is read. An invalid value (a port that is not a number, an unknown `MODBUS_TYPE`, `MODBUS_WRITES_ENABLED=maybe`, a tag map that does not exist or does not validate) stops the server with exit status 2 and a `modbus-mcp: configuration error: …` message on stderr. An empty value means the default.

`modbus-mcp` is a stdio server, meant to be launched by an MCP client (see [Installing for Claude Desktop](#installing-for-claude-desktop), or the MCP Inspector command in [CONTRIBUTING.md](../CONTRIBUTING.md#testing-a-change)). Run by hand in a terminal, it prints nothing and waits for JSON-RPC on stdin; nothing but JSON-RPC is ever written to stdout. It tries to connect to the device at startup, but starts either way: if the device is unreachable it logs `Cannot reach Modbus <type> device at <address> yet` on stderr, and the device tools return `success: false`, `"cannot reach device at <address>"` until the device answers. Every tool call tries to connect first, which also re-establishes a dropped connection. `modbus-mcp --help` lists the command-line options.

### Environment Variables

| Variable                   | Description                                      | Default              | Required |
|----------------------------|--------------------------------------------------|----------------------|----------|
| `MODBUS_TYPE`              | `tcp`, `udp`, or `serial`                        | `tcp`                | —        |
| `MODBUS_HOST`              | Host for TCP/UDP                                | `127.0.0.1`          | For TCP/UDP |
| `MODBUS_PORT`              | Port for TCP/UDP (1-65535)                      | `502`                | For TCP/UDP |
| `MODBUS_DEFAULT_SLAVE_ID`  | Slave/Unit ID used when a tool call omits `slave_id` (0-255) | `1`  | —        |
| `MODBUS_SERIAL_PORT`       | Serial port (e.g., `/dev/ttyUSB0`, `COM1`)      | `/dev/ttyUSB0`       | For serial |
| `MODBUS_BAUDRATE`          | Serial baud rate                                | `9600`               | For serial |
| `MODBUS_PARITY`            | Serial parity: `N`, `E`, `O`                    | `N`                  | For serial |
| `MODBUS_STOPBITS`          | Serial stop bits: `1`, `2`                      | `1`                  | For serial |
| `MODBUS_BYTESIZE`          | Serial byte size: `5`-`8`                       | `8`                  | For serial |
| `MODBUS_TIMEOUT`           | Serial timeout (seconds, > 0); TCP/UDP wait up to 3 s for a reply (pymodbus' default) | `1` | For serial |
| `MODBUS_MAX_RETRIES`       | Retries after a failed read or a failed connection attempt (0-10); a write is never sent twice | `2` | — |
| `MODBUS_RETRY_BACKOFF_BASE`| Backoff base in seconds (doubled per retry)     | `0.2`                | —        |
| `MODBUS_TOOL_TIMEOUT`      | Per-request timeout (seconds; unset or `0` = none) | unset             | —        |
| `MODBUS_WRITES_ENABLED`    | Allow the write tools (`true`/`false`, also `1`/`0`, `yes`/`no`, `on`/`off`). Only from the process environment or `--env-file`; ignored in an automatically found `.env` | `false` | — |
| `REGISTER_MAP_FILE`        | Path to JSON tag map file                       | unset                | —        |

### Example `.env` File

For TCP:
```
MODBUS_TYPE=tcp
MODBUS_HOST=192.168.1.100
MODBUS_PORT=502
MODBUS_DEFAULT_SLAVE_ID=1
```

For Serial:
```
MODBUS_TYPE=serial
MODBUS_SERIAL_PORT=/dev/ttyUSB0
MODBUS_BAUDRATE=9600
MODBUS_PARITY=N
MODBUS_STOPBITS=1
MODBUS_BYTESIZE=8
MODBUS_TIMEOUT=1
```

Neither enables writes, and a `.env` found automatically could not: to allow writes, set `MODBUS_WRITES_ENABLED=true` in the MCP client's `env` block, or put it in a file you pass with `--env-file`:

```json
{
  "command": "uv",
  "args": [ "--directory", "/absolute/path/to/MODBUS-MCP/modbus-python", "run", "modbus-mcp", "--env-file", "/absolute/path/to/plant.env" ]
}
```

## Usage

### Installing for Claude Desktop

The configuration file:

```json
{
   "mcpServers": {
       "Modbus MCP Server": {
           "command": "uv",
           "args": [ "--directory", "/absolute/path/to/MODBUS-MCP/modbus-python", "run", "modbus-mcp" ],
           "env": { "MODBUS_TYPE": "tcp", "MODBUS_HOST": "127.0.0.1", "MODBUS_PORT": "1502" }
       }
   }
}
```

### Using Tools

All tools return a structured object: `{ "success": bool, "data": any, "error": string|null, "meta": object }`. Addresses are zero-based.

The write tools add `meta.outcome` and `meta.request_sent`: `written` (the device acknowledged the write), `rejected` (it answered with a Modbus exception), `not_sent` (refused before sending: writes disabled, invalid arguments, device unreachable; `request_sent: false`), or `unknown` (sent, but no reply: `"no reply; the write may have been applied; check before retrying"`). Nothing is sent twice, so read the value back before writing again after an `unknown`. The examples use addresses that exist on the [mock device](../modbus-mock-server/README.md#register-map-unitslave-id-any) (every table holds addresses 0-99; 10-99 are unused, free for experiments); the writes need `MODBUS_WRITES_ENABLED=true`.

1. Read one holding register (Valve Position on the mock)
   ```json
   { "tool": "read_register", "parameters": { "address": 0, "slave_id": 1 } }
   ```

2. Write one holding register (an unused register on the mock)
   ```json
   { "tool": "write_register", "parameters": { "address": 10, "value": 100, "slave_id": 1 } }
   ```

3. Read coils (chunked automatically when large)
   ```json
   { "tool": "read_coils", "parameters": { "address": 0, "count": 5, "slave_id": 1 } }
   ```

4. Write multiple coils
   ```json
   { "tool": "write_coils_bulk", "parameters": { "address": 10, "values": [true, false, true], "slave_id": 1 } }
   ```

5. Read input registers (Flow Rate, Tank Level, Vibration on the mock)
   ```json
   { "tool": "read_input_registers", "parameters": { "address": 2, "count": 3, "slave_id": 1 } }
   ```

6. Read discrete inputs
   ```json
   { "tool": "read_discrete_inputs", "parameters": { "address": 0, "count": 8, "slave_id": 1 } }
   ```

7. Read multiple holding registers
   ```json
   { "tool": "read_multiple_holding_registers", "parameters": { "address": 0, "count": 3, "slave_id": 1 } }
   ```

8. Mask write a register: register = (register AND and_mask) OR (or_mask AND NOT and_mask); this sets the two low bits of register 20
   ```json
   { "tool": "mask_write_register", "parameters": { "address": 20, "and_mask": 65532, "or_mask": 3, "slave_id": 1 } }
   ```

9. Device information
   ```json
   { "tool": "read_device_information", "parameters": { "slave_id": 1, "read_code": 3 } }
   ```

10. Typed reads (with endianness and scaling): two float32 values in registers 40-43, low word first
   ```json
   { "tool": "read_holding_typed", "parameters": { "address": 40, "dtype": "float32", "count": 2, "byteorder": "big", "wordorder": "little", "scale": 1.0, "offset": 0.0, "slave_id": 1 } }
   ```
   and the mock's pH (input register 5, stored x100) as a number:
   ```json
   { "tool": "read_input_typed", "parameters": { "address": 5, "dtype": "uint16", "scale": 0.01, "slave_id": 1 } }
   ```
   `data.values` holds `value * scale + offset`; with the default scale 1 and offset 0 the decoded values are returned unchanged (so 64-bit integers keep full precision). NaN and infinities come back as the strings `"NaN"`, `"Infinity"` and `"-Infinity"`, since JSON has no such numbers.

11. Write multiple holding registers (1-123 per call)
   ```json
   { "tool": "write_registers", "parameters": { "address": 50, "values": [1,2,3], "slave_id": 1 } }
   ```

12. Health
   ```json
   { "tool": "ping", "parameters": {} }
   ```
   `data` has `connected`, `device_reachable`, `probe_address`, `probe_slave_id`, `probe_response` (`"ok"` or the exception the device returned), `probe_latency_ms`, `probe_error`, and the configuration (`type`, `host`, `port`, `serial_port`, `writes_enabled`, `max_retries`, `tool_timeout`, `tag_count`). Pass `address` to probe a register other than 0.

### Tag Map (optional)

Point `REGISTER_MAP_FILE` to a JSON file that defines named tags. Example file (it matches the mock device):

```json
{
  "ValvePosition": { "table": "holding", "address": 0, "unit": "%" },
  "Temperature": { "table": "input", "address": 0, "dtype": "uint16", "scale": 0.1, "unit": "°C" },
  "PumpSpeed": { "table": "holding", "address": 30, "dtype": "float32", "count": 1, "byteorder": "big", "wordorder": "big", "slave_id": 1 },
  "PumpEnabled": { "table": "coil", "address": 0, "slave_id": 1 },
  "Status": { "table": "discrete", "address": 0, "count": 2, "slave_id": 1 }
}
```

Keys of a tag:

| Key | Meaning | Default |
|-----|---------|---------|
| `table` | `holding`, `input`, `coil` or `discrete` (aliases such as `hr`, `ir`, `coils`, `di` work) | `holding` |
| `address` | Zero-based start address (required) | — |
| `count` | Number of values (typed tags), registers (raw register tags) or bits | `1` |
| `slave_id` | Unit id | `MODBUS_DEFAULT_SLAVE_ID` |
| `dtype` | Holding/input only: one of the typed dtypes; without it a register tag is raw `uint16` | none |
| `byteorder`, `wordorder` | Holding/input only: `big` or `little` | `big` |
| `scale`, `offset` | Typed tags only: read returns `raw * scale + offset` (`scale` must not be 0) | `1`, `0` |
| `description`, `unit` | Free text, shown by `list_tags` | none |

The map is validated when the server starts: unknown keys or tables, a missing or out-of-range address, an unsupported dtype or order, typed keys on a coil/discrete tag, `scale`/`offset` without a `dtype`, or a tag that runs past address 65535 stop the server with a message naming the tag.

Tools:

- List tags
  ```json
  { "tool": "list_tags", "parameters": {} }
  ```

- Read a tag (typed and scaled when the tag has a `dtype`; raw registers or bits otherwise)
  ```json
  { "tool": "read_tag", "parameters": { "name": "Temperature" } }
  ```

- Write a tag
  - Coils: `true` or `false` (a coil tag with `count` > 1 is refused; write those coils with `write_coils_bulk`)
    ```json
    { "tool": "write_tag", "parameters": { "name": "PumpEnabled", "value": true } }
    ```
  - Holding registers: a value, or a list of `count` values, in the units `read_tag` returns. The tag's `scale` and `offset` are reversed and the result is encoded by `dtype` (default `uint16`) and endianness. For an integer dtype the raw value must come out whole: on a tag with scale 0.1, `25.3` is written as 253, while `25.35` is refused.
    ```json
    { "tool": "write_tag", "parameters": { "name": "PumpSpeed", "value": 42.5 } }
    ```
  - Input and discrete-input tags are read-only.

## Testing

```bash
uv sync
uv run pytest            # unit + integration tests
uv run pytest -m "not integration"   # unit tests only, no subprocesses
uv run ruff check && uv run ruff format --check
```

To check the lowest versions `pyproject.toml` allows, as CI does:

```bash
uv venv --python 3.10 .venv
uv pip install --resolution lowest-direct -e . "pytest>=8"
uv run --no-sync pytest
uv sync --locked   # back to the locked versions
```

The integration tests (`-m integration`) start the mock device from `../modbus-mock-server` with `uv run --locked` on a free port, and drive `python -m modbus_mcp` over stdio with the MCP client SDK. They need `uv` on `PATH` and are skipped without it.

## Known issues

- **With mcp 1.6-1.9 the server does not exit when stdin closes.** That is the mcp SDK's stdio transport; MCP clients stop it with SIGTERM afterwards, as the protocol specifies. The locked mcp 1.30.0 exits on its own.

## Testing, Security and Contributing

- Run it against the mock device: [`../modbus-mock-server`](../modbus-mock-server/README.md).
- Write tools are disabled unless `MODBUS_WRITES_ENABLED=true`; read [SECURITY.md](../SECURITY.md) before pointing the server at real equipment.
- Development setup, the CI checks and pull request conventions: [CONTRIBUTING.md](../CONTRIBUTING.md).

## Acknowledgements

This server is derived from [kukapay/modbus-mcp](https://github.com/kukapay/modbus-mcp) and still contains portions of it, notably in `src/modbus_mcp/cli.py`. Those portions are Copyright (c) 2025 kukapay and used under the MIT License. kukapay's license text is kept unchanged in [LICENSE-kukapay](LICENSE-kukapay).

## License

MIT — see [LICENSE](../LICENSE). It names both copyright holders: IndustriAgents, and kukapay for the portions derived from kukapay/modbus-mcp (see [Acknowledgements](#acknowledgements)).
