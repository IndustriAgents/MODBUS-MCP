# Security Policy

## What this server is, in security terms

The Modbus MCP server is a bridge between a language model and industrial
equipment. It speaks MCP over stdio to its client and Modbus (TCP, UDP or
serial RTU) to a device, which means the trust boundary sits *inside* the
server: whatever the model decides to call, the device is asked to do.

Modbus was designed for a physically isolated network and carries no
authentication or encryption of its own. Anyone who can reach the TCP/UDP port
or the serial line can read every register and write every coil the device
exposes. Nothing in this server changes that; it can only limit what the *model*
is offered.

So treat this server as equipment on the control network, not as an
application on the office network.

## Writes are disabled by default

Six tools change device state: `write_register`, `write_registers`,
`write_coil`, `write_coils_bulk`, `mask_write_register` and `write_tag`.
They refuse to send anything unless the server is started with:

```bash
MODBUS_WRITES_ENABLED=true
```

Without it, those tools are still listed to the client but return
`{ "success": false, "error": "Writes are disabled by configuration (set
MODBUS_WRITES_ENABLED=true to allow them)" }` without sending anything to the
device. Only a recognised true/false value is accepted; anything else stops the
server at startup instead of being read as one or the other.

### Where the switch may come from

`MODBUS_WRITES_ENABLED` is honoured only from the server's real process
environment (for an MCP client, the `env` block of its server configuration) or
from a file passed explicitly with `modbus-mcp --env-file PATH`. A `.env` file
the server finds by itself (searching up from its package directory) may set
connection settings, but not this switch: when an MCP client launches the
server straight from a virtualenv, that search can reach the client's own
project, which may not be trusted. If such a `.env` sets
`MODBUS_WRITES_ENABLED`, the server ignores it and prints a warning on stderr
naming the variable and the file. See
[`modbus-python/.env.example`](modbus-python/.env.example).

What the server checks before a write, when writes are enabled:

- Arguments must fit Modbus: addresses and register values 0-65535, unit ids
  0-255, at most 123 registers or 1968 coils per request. A value that does not
  fit is refused, never truncated or wrapped. Types are strict: `"5"` or `5.0`
  is not an address and `1` or `"on"` is not a coil state.
- `write_tag` only writes the registers or coils its tag defines (exactly
  `count` values), only to holding-register and coil tags, and only values the
  tag's dtype can hold exactly after reversing its scale and offset.

What it does not check: there is no per-register allowlist and no
engineering-range check. When writes are enabled, any address the device
accepts can be written with any value that fits the register.

### A write is sent at most once

pymodbus re-sends a request on a timeout unless told not to; this server
creates its client with `retries=0`, and its own retries
(`MODBUS_MAX_RETRIES`) apply to reads and to connection attempts only. A write
is retried only when nothing was sent (the connection could not be opened).
Once it has gone out, a lost or late reply is reported, not retried:
`success: false`, `meta.outcome: "unknown"`, `meta.request_sent: true`,
`"no reply; the write may have been applied; check before retrying"`. Read
the value back before deciding to write again; for a command register that
acts on every write, a second write is a second command. The other outcomes are
`written`, `rejected` (the device answered with a Modbus exception) and
`not_sent`.

## Running it safely

- **Start against the mock.** This repository ships a mock Modbus TCP device
  ([`modbus-mock-server/`](modbus-mock-server/)) so that an agent can be
  exercised end to end without touching real plant. Do that first, every time.
- **Leave `MODBUS_WRITES_ENABLED` unset until you mean otherwise.** Write
  tools move physical equipment. There is no undo, and a language model will
  call a tool it has been given. `ping` reports `writes_enabled`, so you can
  check what a running server allows.
- **Do not expose the server beyond the host running the client.** It is a
  stdio process meant to run beside the MCP client, not a network service.
- **Segment the network.** The server should sit where a PLC engineering
  workstation would sit, behind whatever separates your control network from
  everything else.
- **Never point it at a safety system.** Safety-instrumented functions are out
  of scope for anything in this repository.

## Reporting a vulnerability

Please report privately, by email to hi@industriagents.com or through GitHub
private vulnerability reporting:
[report a vulnerability](https://github.com/IndustriAgents/MODBUS-MCP/security/advisories/new).

Please do not open a public issue for a vulnerability.

Include the component affected (`modbus-python` or `modbus-mock-server`), the
commit, what an attacker would gain, and a reproduction if you have one. We will
acknowledge within a week and keep you updated as we work on a fix.

## Scope

In scope: the MCP server (`modbus-python/`) and the mock device
(`modbus-mock-server/`) in this repository. The same code ships inside the
[IndustriConnect](https://github.com/IndustriAgents/IndustriConnect) suite as
the `MODBUS-Project` submodule; report it here either way.

Out of scope: vulnerabilities in the Modbus protocol itself (the lack of
authentication is a property of Modbus), in third-party libraries such as
`pymodbus` or the `mcp` SDK — report those upstream — and in vendor PLC
firmware.
