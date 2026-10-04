# Modbus MCP Server (Python)

The MCP server of [MODBUS-MCP](../README.md). It is a [uv](https://docs.astral.sh/uv/) project with the `modbus-mcp` entry point; the mock device to test it against lives in [`../modbus-mock-server`](../modbus-mock-server/README.md).

## Features

- **Core tools**:
  - Read/write holding registers: `read_register`, `write_register`, `read_multiple_holding_registers`.
  - Read/write coils: `read_coils`, `write_coil`, `write_coils_bulk`.
  - Read input registers: `read_input_registers`.
  - Read discrete inputs: `read_discrete_inputs`.
  - Bitwise masked write: `mask_write_register`.
  - Device information (MEI 0x2B/0x0E): `read_device_information`.
- **Typed reads/writes** (currently broken, see [Known issues](#known-issues)):
  - `read_holding_typed`, `read_input_typed` for `int16/uint16/int32/uint32/int64/uint64/float32/float64` with byte/word endianness, scale, and offset.
  - Encode/Decode uses pymodbus BinaryPayload utilities.
- **Tag map (optional)**:
  - Load JSON register map via `REGISTER_MAP_FILE` and use `list_tags`, `read_tag`, `write_tag` (typed tags and holding-register writes are affected by the same [known issue](#known-issues)).
- **Reliability**:
  - Built-in retries with exponential backoff, per-tool timeouts, and automatic chunking to protocol limits.
- **Structured results**:
  - Every tool returns `{ success, data, error, meta }` for predictable parsing.
- **Health**:
  - `ping` returns connection and configuration status.
- **Prompt**:
  - Analyze register values with a simple prompt: `analyze_register`.

> [!WARNING]
> Typed values currently fail on every call: `read_holding_typed`, `read_input_typed`, `read_tag` on a tag with a `dtype`, and `write_tag` on a holding-register tag return `{"success": false, "error": "Big"}`. See [Known issues](#known-issues).

## Requirements

- **Python**: 3.10
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
   This installs the versions pinned in the committed `uv.lock` (`mcp` 1.6.0, `pymodbus` 3.9.2). Install from the lockfile: the ranges in `pyproject.toml` also admit `mcp` 2.x and `pymodbus` 3.10+, which this server does not work with.

## Configuration

The server connects to a Modbus device using parameters specified via environment variables. Set these variables in a `.env` file (for example in `modbus-python/`) or your shell environment; variables already set in the environment take precedence.

`modbus-mcp` is a stdio server, meant to be launched by an MCP client (see [Installing for Claude Desktop](#installing-for-claude-desktop), or the MCP Inspector command in [CONTRIBUTING.md](../CONTRIBUTING.md#testing-a-change)). Run by hand in a terminal, it prints nothing and waits for JSON-RPC on stdin. It tries to connect to the device at startup. If the device is unreachable, it logs a `Failed to connect` warning, and it exits with `RuntimeError: Failed to connect to Modbus <type> device` once a client connects.

### Environment Variables

| Variable                   | Description                                      | Default              | Required |
|----------------------------|--------------------------------------------------|----------------------|----------|
| `MODBUS_TYPE`              | `tcp`, `udp`, or `serial`                        | `tcp`                | —        |
| `MODBUS_HOST`              | Host for TCP/UDP                                | `127.0.0.1`          | For TCP/UDP |
| `MODBUS_PORT`              | Port for TCP/UDP                                | `502`                | For TCP/UDP |
| `MODBUS_DEFAULT_SLAVE_ID`  | Slave/Unit ID                                   | `1`                  | —        |
| `MODBUS_SERIAL_PORT`       | Serial port (e.g., `/dev/ttyUSB0`, `COM1`)      | `/dev/ttyUSB0`       | For serial |
| `MODBUS_BAUDRATE`          | Serial baud rate                                | `9600`               | For serial |
| `MODBUS_PARITY`            | Serial parity: `N`, `E`, `O`                    | `N`                  | For serial |
| `MODBUS_STOPBITS`          | Serial stop bits                                | `1`                  | For serial |
| `MODBUS_BYTESIZE`          | Serial byte size                                | `8`                  | For serial |
| `MODBUS_TIMEOUT`           | Serial timeout (seconds)                        | `1`                  | For serial |
| `MODBUS_MAX_RETRIES`       | Max retry attempts for operations               | `2`                  | —        |
| `MODBUS_RETRY_BACKOFF_BASE`| Backoff base in seconds (exponential)           | `0.2`                | —        |
| `MODBUS_TOOL_TIMEOUT`      | Per-operation timeout (seconds)                 | unset (no timeout)   | —        |
| `MODBUS_WRITES_ENABLED`    | Enable write operations (`true`/`false`)        | `true`               | —        |
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

All tools return a structured object: `{ "success": bool, "data": any, "error": string|null, "meta": object }`.

1. Read one holding register
   ```json
   { "tool": "read_register", "parameters": { "address": 0, "slave_id": 1 } }
   ```

2. Write one holding register
   ```json
   { "tool": "write_register", "parameters": { "address": 10, "value": 100, "slave_id": 1 } }
   ```

3. Read coils (chunked automatically when large)
   ```json
   { "tool": "read_coils", "parameters": { "address": 0, "count": 5, "slave_id": 1 } }
   ```

4. Write multiple coils
   ```json
   { "tool": "write_coils_bulk", "parameters": { "address": 0, "values": [true, false, true], "slave_id": 1 } }
   ```

5. Read input registers
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

8. Mask write a register
   ```json
   { "tool": "mask_write_register", "parameters": { "address": 100, "and_mask": 65535, "or_mask": 3, "slave_id": 1 } }
   ```

9. Device information
   ```json
   { "tool": "read_device_information", "parameters": { "slave_id": 1, "read_code": 3 } }
   ```

10. Typed reads (with endianness and scaling); currently fails with `"Big"`, see [Known issues](#known-issues)
   ```json
   { "tool": "read_holding_typed", "parameters": { "address": 200, "dtype": "float32", "count": 2, "byteorder": "little", "wordorder": "big", "scale": 1.0, "offset": 0.0, "slave_id": 1 } }
   ```

11. Write multiple holding registers
   ```json
   { "tool": "write_registers", "parameters": { "address": 50, "values": [1,2,3], "slave_id": 1 } }
   ```

12. Health
   ```json
   { "tool": "ping", "parameters": {} }
   ```

### Tag Map (optional)

Point `REGISTER_MAP_FILE` to a JSON file that defines named tags. Example file:

```json
{
  "PumpSpeed": { "table": "holding", "address": 100, "dtype": "float32", "count": 1, "byteorder": "big", "wordorder": "big", "slave_id": 1 },
  "ValveOpen": { "table": "coil", "address": 5, "slave_id": 1 },
  "Alarm": { "table": "discrete", "address": 10, "count": 4, "slave_id": 1 }
}
```

Tools:

- List tags
  ```json
  { "tool": "list_tags", "parameters": {} }
  ```

- Read a tag (typed when `dtype` supplied; a typed read such as this `PumpSpeed` one currently fails, see [Known issues](#known-issues))
  ```json
  { "tool": "read_tag", "parameters": { "name": "PumpSpeed" } }
  ```

- Write a tag
  - Coils: boolean
    ```json
    { "tool": "write_tag", "parameters": { "name": "ValveOpen", "value": true } }
    ```
  - Holding registers: value or list of values, encoded by `dtype` (default `uint16`) and endianness. This currently fails for every holding-register tag, see [Known issues](#known-issues).
    ```json
    { "tool": "write_tag", "parameters": { "name": "PumpSpeed", "value": 42.5 } }
    ```

## Known issues

These are bugs in `src/modbus_mcp/cli.py`, documented here until they are fixed:

- **Typed values fail on every call.** `_to_endian()` returns `Endian.Big` / `Endian.Little`, but pymodbus 3.9.2, the version `uv.lock` pins, only defines `Endian.BIG`, `Endian.LITTLE` and `Endian.AUTO`. Every decode and encode therefore raises an `AttributeError`, which the tools return as `{"success": false, "error": "Big"}`. That breaks:
  - `read_holding_typed` and `read_input_typed`, once the registers have been read;
  - `read_tag` on a tag with a `dtype`;
  - `write_tag` on a holding-register tag, even one without a `dtype`, because it always encodes (default `uint16`). Nothing is written to the device.

  Not affected: the raw register, coil and discrete-input tools, `read_tag` on tags without a `dtype`, and `write_tag` on coil tags. The fix is to use `Endian.BIG` / `Endian.LITTLE` in `_to_endian()`.

## Testing, Security and Contributing

- Run it against the mock device: [`../modbus-mock-server`](../modbus-mock-server/README.md) (note its [known issues](../modbus-mock-server/README.md#known-issues)).
- Write tools are allowed unless `MODBUS_WRITES_ENABLED=false`; read [SECURITY.md](../SECURITY.md) before pointing the server at real equipment.
- Development setup, the CI checks and pull request conventions: [CONTRIBUTING.md](../CONTRIBUTING.md).

## Acknowledgements

This server is derived from [kukapay/modbus-mcp](https://github.com/kukapay/modbus-mcp) and still contains substantial portions of it: much of `src/modbus_mcp/cli.py`, and `pyproject.toml` and `uv.lock` as they were upstream. Those portions are Copyright (c) 2025 kukapay and used under the MIT License. kukapay's license text is kept unchanged in [LICENSE-kukapay](LICENSE-kukapay).

## License

MIT — see [LICENSE](../LICENSE). It names both copyright holders: IndustriAgents, and kukapay for the portions derived from kukapay/modbus-mcp (see [Acknowledgements](#acknowledgements)).
