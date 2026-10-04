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
from a file passed explicitly with `modbus-mcp --env-file PATH`.

The server also loads one `.env` file by itself, and only one:
`modbus-python/.env` of a source checkout, found relative to the package
source. A `.env` in the working directory (wherever the MCP client launched the
server, which may be an untrusted project) or in any parent directory is never
read, and an installed package has no automatic `.env`. Even that file is
limited, because settings such as the host and the tag map decide where a write
goes: it may set only `MODBUS_*` and `REGISTER_MAP_FILE`, never
`MODBUS_WRITES_ENABLED` and nothing else (`PATH`, `LD_PRELOAD`, `PYTHON*`, …).
Keys are compared case-insensitively, with `export` prefixes and quotes
removed, so `modbus_writes_enabled=true` counts. Every ignored key gets a
warning on stderr naming it and the file. See
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
acts on every write, a second write is a second command.

The outcome is only `rejected` when the device refused the write with a Modbus
exception that, by the protocol's definition, comes before execution (1, 2, 3,
6 or 10), and only `written` when its reply echoes the request. Anything else
after the write went out, including a garbled or mismatched reply, exception 4
(Server Device Failure) or the call's deadline (`MODBUS_CALL_TIMEOUT`), is
`unknown`. Whether a frame went out is known exactly (a hook in pymodbus sees
each frame right before it is handed to the transport, and refuses it when the
connection is gone), so a failure before sending is `not_sent`.

A reply is only used if it belongs to its request: an exception reply must
carry the request's function code, a write reply must echo the request (a coil
write's byte for byte). Modbus RTU replies carry no transaction id, so a late
reply to an earlier request could otherwise make a later write look
`rejected`, or be returned as a read. After any request goes unanswered, the
server closes the connection before another request can use it and reconnects
on the next call (on a serial line after a quiet period of `MODBUS_TIMEOUT`), so
a late reply is not taken for the next request's. On a serial line, a reply
later than twice `MODBUS_TIMEOUT` can still be mistaken for the next request's
when it fits that request (an exception with the same function code, for
example); set `MODBUS_TIMEOUT` above the device's worst-case response time.

A write that has not gone out is never sent after its call was abandoned: when
the client cancels it (also in the same instant the write is granted its turn:
the last check reads the cancel scope mcp sets synchronously), when the client
disconnects (the server stops every
pending call as soon as stdin closes), or when the call's deadline passes. The
deadline (`MODBUS_CALL_TIMEOUT`, 30 s by default) starts when the call starts
and includes time spent queued behind other calls: an MCP client whose own
timeout fires sends no cancellation, so a write queued behind slow calls is
dropped by this deadline rather than sent late. The
last check runs right before the request is handed to the transport, with no
await in between, so a write that was queued, reconnecting or waiting cannot
be sent late.

The tools also tell MCP clients not to retry writes: they are annotated
`destructiveHint: true`, `idempotentHint: false`.

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
