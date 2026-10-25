# Modbus protocol assessment and capability decisions

Reviewed 2026-10-25 against this repository, OPCUA-MCP's execution boundaries,
[Modbus Application Protocol V1.1b3](https://www.modbus.org/file/secure/modbusprotocolspecification.pdf),
and the locked PyModbus 3.15.0 implementation. The supported dependency range is
separately maintained in pyproject.toml and CI.

## Protocol evidence and scope

The application specification defines zero-based protocol addresses, distinct
bit/register tables, function-specific quantities and exception replies. Section
6.21 defines device-identification stream continuation, echoed read code and
individual-object access. We now follow next-object cursors, validate the reply,
and report complete versus partial results. An unknown initial stream object may
restart at zero as specified; individual access must return the requested object.
Section 7 distinguishes exception responses from lost replies. A control response
that cannot establish rejection remains unknown and is never replayed.

[PyModbus 3.15.0 client documentation](https://pymodbus.readthedocs.io/en/v3.15.0/source/client.html)
describes the client API, supported transports and serialization requirements.
The repository configures TCP, UDP or serial/RTU; it disables native resend and
retains request-specific send counting, reply checks and serial quiet time. A
library's support for other transports does not add them to this MCP server.

The [Modbus Organization specifications catalogue](https://www.modbus.org/modbus-specifications)
separately lists Modbus Security and serial guidance. This server does not provide
Modbus Security/TLS. Vendor register maps remain necessary for type, word order,
scaling and engineering meaning; native values are not self-describing telemetry.

## Capability and gap matrix

| Workflow / tools | Existing capability | Gap addressed / acceptance evidence | Remaining boundary |
|---|---|---|---|
| Inspect: `get_connection_status` | Previously only network `ping` and local tag listing | New local seventh tool exposes configured target, cached connection state and typed capabilities without I/O; `test_capabilities.py`, artifact smoke | Not a liveness probe or device authorization |
| Read: `read_values` | Four tables, typed reads and aliases; bounded chunks | Existing request/reply checks preserved; transport injected through a domain port | Chunked reads are not an atomic snapshot; scaling comes from a configured map |
| Identify: `read_device_information` | Previously returned only first FC43/14 page | Bounded continuation, matching MEI/read code, object count/IDs, progress and partial error reporting; `test_identity_continuation.py` | Device may not implement FC43 or optional/private objects |
| Controls: `write_values`, `mask_write_register` | Native gate, validation, send tracking, explicit uncertain outcomes, FC23 write-before-read | Native invocation kept behind injected transport; application logs omit values/masks; `test_application_ports.py` plus existing send-once/wire suites | Acknowledgement is not physical completion; no automatic replay after possible send |
| Metadata and liveness: `list_tags`, `ping` | Configured maps and an explicit holding-register probe | Preserved, offline status complements rather than changes ping | Even an exception can prove reachability without proving an address exists |
| Native subscriptions, TLS/security, automatic map discovery | Not implemented | Explicit limitations in capability report | Requires separate feature design and device support; no invented equivalents |

## Architecture and migration

`OperationContext` receives a `ModbusTransportPort` at composition. Application
modules no longer import native adapters. The adapter retains the tested timeout,
retry, unit-keyword compatibility and cancellation behavior. There is no new
resend path or cross-repository runtime dependency.

`get_connection_status` is additive; the catalogue grows from six to seven tools.
Capabilities distinguish backend implementation and native write gates from MCP
policy, device permission and reachability. Existing aliases and result envelopes
remain. Device-identification `data` is still the object-ID/text map; `meta.pages`,
`meta.complete` and optional `meta.restarted_at_zero` describe traversal. Failed
later pages preserve already validated objects with `success: false`.

## Verification boundary

The software suites cover injected ports, native PyModbus TCP/UDP behavior,
serial/RTU framing over a local socket, MCP stdio and installed wheel/npm launchers.
The RTU socket fixture is not electrical RS-485 testing. No physical device or
serial adapter was provided or accessed. Hardware compatibility requires an
authorized device, its register map, and the intended gateway/serial wiring.

All canonical tools now declare structural result shapes in `contract/tools.json`,
including typed outcome/completeness metadata and nullable failure data. Native
values retain explicit JSON value flexibility. CI validates live canonical MCP
responses against those schemas and checks text/structured result agreement;
legacy aliases retain their compatibility payload shapes.

## Integer precision migration

At the MCP wire boundary, integers outside `[-9007199254740991, 9007199254740991]`
are now decimal strings, including nested values. Safe integers remain JSON numbers
and booleans remain booleans. `meta.value_encoding: decimal_string` and
`meta.encoded_integer_paths` identify converted fields using JSON pointers. Internal
application arithmetic and native codecs retain exact integers before presentation.
Consumers should use an integer/decimal parser, not a JavaScript Number conversion,
for these strings. Write inputs retain their existing type requirements; do not
coerce a returned decimal string through a floating-point number before writing.
Modbus clients can use the preserved raw 16-bit register representation for exact
large-integer writes. This change prevents silent rounding in JSON consumers and
is an intentional output compatibility change for unsafe large integers.

## Second improvement pass: FC23 write-before-read transactions

`write_values(target="holding_readwrite", read_address=40, read_count=2,
write_address=40, values=[65535, 123])` sends one Read/Write Multiple Registers
request (function 23 / 0x17). It writes first, then reads the requested holding
range. Overlapping ranges are valid. `data.written` is the acknowledged count;
`data.registers` is the read range, not an echo of the requested write. This does
not establish a PLC scan snapshot or physical process completion.

[Modbus Application Protocol V1.1b3 section 6.17](https://www.modbus.org/file/secure/modbusprotocolspecification.pdf)
specifies one transaction, the operation order, read quantity 1..125 and write
quantity 1..121. Both address spans and every uint16 value are validated before
I/O. The operation is never split into multiple requests. The existing
`MODBUS_WRITES_ENABLED` gate, policy, serialized transport and send-once controls
apply; reads embedded in FC23 do not make it read-only or retryable. Exception 5
(Acknowledge/deferred completion), lost replies and malformed replies remain
`outcome="unknown"`, without replay. A response must have FC23 and exactly the
requested read count. Unsupported-function exceptions remain explicit failures.

[PyModbus's documented `readwrite_registers` API](https://pymodbus.readthedocs.io/en/v3.15.0/source/client.html)
is invoked directly with the existing version-compatible unit-ID adapter. The
local real Modbus TCP integration verifies overlap/write-before-read; focused
fixtures cover quantity/range/gate validation, reply mismatch, deferred completion
and a write applied despite a lost response. No physical device was accessed.

### Prioritized remaining improvements

1. Add bounded file-record/FIFO reads only with device-specific fixtures and
   explicit support reporting; not all servers implement optional functions.
2. Add a commissioned schema format for units/quality/sampling descriptions;
   register numbers alone cannot identify engineering semantics.
3. Modbus Security/TLS needs an explicitly selected and verified secure transport;
   physical acceptance should cover TCP, UDP and serial device behavior separately.

FC23 requires serial unit IDs 1..247, rejecting broadcast 0 and reserved 248..255 before any send: [Modbus Serial Guide V1.02 §2.1](https://www.modbus.org/file/secure/modbusoverserial.pdf) assigns it to broadcasts, which return no reply. TCP unit ID 0 remains permitted because direct TCP server routing differs.
