# Architecture and migration (0.3.0)

This repository is an independent protocol MCP server. It follows OPCUA-MCP's
instance-owned composition, explicit MCP contract and application/adapter split.
It has no runtime dependency on another IndustriAgents protocol repository or
on IndustriConnect-MCPs. The umbrella repository can pin this repository as a
submodule and launch its released command.

## Execution path

```mermaid
flowchart LR
  Client[MCP client] -->|stdio| Contract[Protocol contract and dispatcher]
  Contract --> Execution[Instance policy, admission and audit]
  Execution --> Application[Protocol application operations]
  Application --> Adapter[Native protocol adapter]
  Adapter --> Device[Configured endpoint or bus]
```

`server.py` composes one server instance and its resources. `protocol/` owns
registration, schema dispatch, SDK request context access and wire errors.
`application/` owns reads, writes, discovery, diagnostics and metadata operations.
`domain/` holds protocol types, error/outcome vocabulary, structural ports and
policy values. `adapters/` contains the native driver and owns its connection,
retry eligibility, send tracking and cancellation fences. `infrastructure/`
contains audit and execution-context support. Compatibility modules at the old
Python import paths forward to the implementation rather than owning another
connection. Some established config/codec/metadata helpers keep their root
module paths to avoid unnecessary public import breaks.

The supported deployment is local stdio with one caller context and one
configured endpoint/bus per process. Instance ownership is not a claim of
multi-tenant authorization or remote HTTP transport support. Modbus and MQTT
provide `create_server(...)` with explicit per-instance configuration; the
other protocols keep their existing server constructors with injected clients
or masters. The CLI default instance exists only for compatibility.

## Tool contract

`contract/tools.json` is the reviewed contract, including JSON schemas, routing,
legacy names and the result envelope. Its identical packaged resource is
`modbus-python/src/modbus_mcp/_contract/tools.json`.
Run `python scripts/verify_contract.py` to reject drift. There is no second
TypeScript implementation: npm carries the same wheel as the PyPI release.

| Advertised tool | Modes | Effect |
|---|---|---|
| `read_values` | `holding`, `input`, `coils`, `discrete`, `holding_typed`, `input_typed`, `tag` | Read/local |
| `write_values` | `holding`, `coils`, `tag` | Changes state or consumes events |
| `mask_write_register` | No mode selector | Changes state or consumes events |
| `read_device_information` | No mode selector | Read/local |
| `list_tags` | No mode selector | Read/local |
| `ping` | No mode selector | Device read |
| `get_connection_status` | No mode selector | Local configuration and capabilities |

A mode selects one protocol operation. Parameters from different modes cannot
be mixed; unknown parameters fail before dispatch. Alias/tag modes resolve
through the existing configured map and preserve its scaling and validation.
Legacy names remain callable but are omitted from `tools/list`. Names retained
in both catalogs accept the old address form without the new selector for
migration. Those legacy forms retain native argument validation and coercion.

| Legacy name | Advertised replacement |
|---|---|
| `read_multiple_holding_registers` | `read_values` |
| `read_input_registers` | `read_values` |
| `read_coils` | `read_values` |
| `read_discrete_inputs` | `read_values` |
| `read_holding_typed` | `read_values` |
| `read_input_typed` | `read_values` |
| `read_tag` | `read_values` |
| `write_registers` | `write_values` |
| `write_coils_bulk` | `write_values` |
| `write_tag` | `write_values` |
| `read_register` | `read_values` |
| `write_register` | `write_values` |
| `write_coil` | `write_values` |

Every tool returns `{success, data, error, meta}`. On the wire, `success:false`
sets MCP `isError:true`; structuredContent and the JSON text describe the same
result. Writes preserve `meta.outcome` and `meta.request_sent` from the native
adapter. A timeout/cancellation after possible dispatch is uncertain, never
converted to not_sent. There is no new generic retry loop. In particular,
retrying an unknown control outcome can apply a physical action twice.

## Deployment policy and audit

Set `MODBUS_POLICY_FILE` to an explicit JSON policy file. Unknown fields
and invalid types fail at server construction. Without a policy file the native
protocol write gates continue to apply; writes are disabled by default.

```json
{
  "read_only": true,
  "allowed_tools": null,
  "allowed_operations": null,
  "max_in_flight": 64,
  "max_request_bytes": 1048576,
  "audit_path": null
}
```

`allowed_tools` restricts canonical tool names; `allowed_operations` restricts
the concrete handler names in the contract's routes. Null means unrestricted;
an empty array denies everything. Legacy names map to the same canonical
policy, so they cannot bypass it. `read_only` also denies non-read-only
operations such as scans or event-consuming polls. This is tool/operation
policy, not resolved-address or engineering-value authorization. Existing
protocol maps, value limits and write/system gates remain authoritative for
those checks. No new OPC UA-style namespace or physical interlock semantics
are implied for these different protocols.

Admission rejects excess calls immediately, with one additional slot reserved
for a local read-only diagnostic. It does not create an unbounded wait queue.
The native adapter still owns ordering and pending native I/O. Cancellation
releases the MCP admission slot; a cancelled worker thread is not assumed to
have stopped, and native send fences remain responsible for that case.

When `audit_path` is set, a write intent is appended and fsynced before dispatch.
Failure to persist that intent refuses dispatch. The final observed outcome is
appended with the same call ID; a missing result after process failure means
unknown, not success. Failure to persist a result is logged and marked
`meta.audit_error`, without resending the operation or changing its outcome.
Audit records omit payloads and credentials. Manage file retention externally;
this append-only log is not a tamper-proof external audit service.

Policy documents are read only from regular files, with a 64 KiB read bound and
at most 16 JSON nesting levels. Duplicate fields are rejected. The size bound
still applies if a file grows during the read. Audit destinations must also be
regular files; devices and pipes are refused before any audit record is written.
A FIFO cannot block policy loading or audit admission while waiting for a peer.

## Protocol capability limits

TCP, UDP and serial/RTU use pymodbus. Four address tables remain distinct; addresses are zero-based. Typed read codecs preserve byte order, word order and scaling. Raw write_values accepts a list even for a single register/coil; mask_write_register stays distinct because its device operation is different.

## Packages

```sh
uvx --from industriagents-modbus-mcp==0.3.0 modbus-mcp
npx -y industriagents-modbus-mcp@0.3.0
```

The distribution is deliberately prefixed: several generic protocol names on
PyPI belong to other projects. Python import names and command names remain
`modbus_mcp` and `modbus-mcp`. npm requires Node 20+ and uv; uv provisions a
compatible Python on first run. npm bundles a version-checked wheel and passes
stdio, signals, environment and CLI arguments through to it. Network access is
needed on first use to resolve Python dependencies. Native drivers/permissions
are still required for live operation. The mock backend is not selected merely
because a native dependency or device is unavailable.

## Verification and release

The regression suite retains native-send counting, reply-loss, cancellation,
configuration-gate and stdio checks. New architecture tests exercise all
consolidated routes, unknown/mixed arguments, legacy-policy bypass, independent
admission state, durable intent failure and MCP error envelopes. npm tests
check the bundled version, argument boundaries, exit status and missing-uv
errors. See [release.md](release.md) for the registry workflow.

## Request and failure bounds

Every call is bounded before selector forwarding, JSON encoding, schema
validation, policy admission or device I/O. Contract limits are 32 nesting
levels, 10,000 entries per container and 50,000 total values; the deployment
policy additionally bounds serialized request bytes. Non-finite numbers,
non-JSON values, invalid Unicode and non-string object keys are rejected.
These checks bound work after the MCP SDK has decoded the transport frame.

Consolidated selector modes validate their complete contract schemas. Legacy
names and direct routes retain their established native validation and coercion,
including their error details; unknown top-level keys are always refused.
MCP input-model validation runs before the handler. A validation exception
raised inside a handler is an execution failure and cannot prove a control was
not sent. Unexpected execution failures preserve an uncertain outcome; native
send tracking remains authoritative when available. Neither case adds retries.

An audit-result failure adds `meta.audit_error` to the same envelope used for
both text content and structured content, including text-only native results.
Cancellation releases admission capacity and retains an unknown audit outcome
when no result was observed.

### Configuration and native invocation boundary

The CLI resolves `MODBUS_POLICY_FILE` together with connection settings after
`--env-file`, and reports invalid policies as configuration errors on stderr.
Importing the compatibility server does not load process policy. Independent
`create_server(configuration, policy)` instances may receive an explicit policy.
The implicit project `.env` lookup recognizes both the current distribution
name and its legacy name; it still never searches the caller's working directory.

Application modules receive `ModbusTransportPort` through `OperationContext`.
The composition root supplies `ModbusTransport`, whose invocation and unit-keyword
methods delegate to the existing native helper. Native retries, send fences and
reply correlation retain their ownership and tests; application modules import
neither the native adapter nor the SDK. Responses still follow the Modbus port's
structural field contract rather than claiming OPC UA value/status semantics.

## Retry evidence

Write retry eligibility uses the per-call send counter. A `ConnectionException`
can occur after dispatch, so its class alone never authorizes replay: if a
frame was sent, or send tracking is unavailable, the call returns an uncertain
outcome after one attempt. A proven pre-send connection failure can still
reconnect and retry. Reads retain their existing retry rules. Regression tests
count native sends after a write applies and then raises a connection error.

The application currently calls the established transport recovery helpers
directly. This coupling is retained to keep the existing send-fence and
cancellation pipeline intact; it is an explicit difference from OPCUA-MCP's
fully injected execution ports.

## Protocol assessment and capability reporting

See [protocol-assessment.md](protocol-assessment.md) for authoritative sources,
the capability/gap matrix, implementation decisions and hardware test boundaries.
`get_connection_status` returns typed `data.capabilities` without device I/O.
`scope: backend_support` and `device_verified: false` distinguish implemented
software from verified device support. `enabled` reports native configuration
gates only; policy and device permissions still apply.

Application operations now receive the transport invocation/compatibility port
through `OperationContext`; they no longer import native adapters. FC43/14 identity
reads follow bounded, validated continuation and preserve partial data on failure.
The local status tool is the seventh canonical tool; `ping` remains a device probe.

Edit named `resultShapes` in `contract/tools.json`, run `python3 scripts/sync_contract.py` to regenerate advertised data schemas and the packaged resource, then run `python3 scripts/verify_contract.py` to check drift.

The `write_values` mode `holding_readwrite` routes to `application.write.readwrite_registers` and the existing injected transport port. FC23 has one write-before-read native invocation with both ranges validated first; the transport treats the whole transaction as a write for send tracking, cancellation and no replay. Serial targets require unit IDs 1..247. No FC16/FC3 fallback is performed.
