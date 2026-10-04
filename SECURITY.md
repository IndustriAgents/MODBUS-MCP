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

## Writes are enabled by default

Six tools change device state: `write_register`, `write_registers`,
`write_coil`, `write_coils_bulk`, `mask_write_register` and `write_tag`.
They are allowed unless you set:

```bash
MODBUS_WRITES_ENABLED=false
```

With writes disabled, those tools are still listed to the client but return
`{ "success": false, "error": "Writes are disabled by configuration" }` without
sending anything to the device. There is no per-register allowlist and no value
range check — when writes are enabled, any address the device accepts can be
written with any value Modbus can carry.

## Running it safely

- **Start against the mock.** This repository ships a mock Modbus TCP device
  ([`modbus-mock-server/`](modbus-mock-server/)) so that an agent can be
  exercised end to end without touching real plant. Do that first, every time.
- **Set `MODBUS_WRITES_ENABLED=false` until you mean otherwise.** Write tools
  move physical equipment. There is no undo, and a language model will call a
  tool it has been given.
- **Do not expose the server beyond the host running the client.** It is a
  stdio process meant to run beside the MCP client, not a network service.
- **Segment the network.** The server should sit where a PLC engineering
  workstation would sit, behind whatever separates your control network from
  everything else.
- **Never point it at a safety system.** Safety-instrumented functions are out
  of scope for anything in this repository.

## Reporting a vulnerability

Please report privately, by email to hi@industriagents.com or through GitHub
private vulnerability reporting: the **Report a vulnerability** button on this
repository's [Security tab](https://github.com/IndustriAgents/MODBUS-MCP/security).

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
