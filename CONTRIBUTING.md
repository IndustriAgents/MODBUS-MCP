# Contributing to Modbus MCP

Thanks for taking an interest. This repository is the Modbus member of the
[IndustriConnect](https://github.com/IndustriAgents/IndustriConnect) suite: an
MCP server that lets AI agents read and write Modbus devices, plus a mock
Modbus TCP device so nothing has to be rehearsed on live plant.

## Where changes land

IndustriConnect includes this repository as its `MODBUS-Project` git submodule,
tracking `main`. **Changes to the Modbus server or mock land here, in
[IndustriAgents/MODBUS-MCP](https://github.com/IndustriAgents/MODBUS-MCP), not
in IndustriConnect.** When `main` moves, IndustriConnect bumps its submodule
pointer (see [`.github/workflows/notify-industriconnect.yml`](.github/workflows/notify-industriconnect.yml)).
Changes that span several protocols, or the suite's web UI, belong in
IndustriConnect.

## Repository layout

```text
MODBUS-MCP/                  # mounted as MODBUS-Project/ inside IndustriConnect
├── modbus-python/           # the MCP server: uv project, src/modbus_mcp/, `modbus-mcp` entry point
├── modbus-mock-server/      # simulated Modbus TCP device: uv project, `modbus-mock-server` entry point
└── .github/                 # CI, issue and pull request templates
```

Each directory is its own [`uv`](https://docs.astral.sh/uv/) project with its own
`uv.lock`; there is no workspace at the root.

## What holds the suite together

The value of IndustriConnect is that its servers behave the same way, so a
change that breaks one of these needs a reason in the pull request:

1. **One response envelope.** Every tool returns `{ success, data, error, meta }`.
   A client that can read one IndustriConnect server's output can read them all.
2. **Writes are gated.** Tools that write a coil or a register change physical
   equipment. Here they are gated by `MODBUS_WRITES_ENABLED`, which currently
   defaults to `true`; the suite's direction is read-only by default, and
   changes that move this server toward it are welcome.
3. **A mock for every tool.** If you add a tool, the mock has to be able to
   answer it, or nobody can test it without a plant.

## Getting set up

You need Python 3.10+ and `uv`. Work inside the project you are changing:

```bash
git clone https://github.com/IndustriAgents/MODBUS-MCP.git
cd MODBUS-MCP

# Terminal 1: the mock device the server talks to (127.0.0.1:1502)
cd modbus-mock-server
uv sync
MODBUS_HOST=127.0.0.1 uv run modbus-mock-server

# Terminal 2 (a new terminal, from the MODBUS-MCP root): the MCP server
cd modbus-python
uv sync
```

`modbus-mcp` is a stdio server. Run by hand, it prints nothing and waits for
JSON-RPC on stdin, so let an MCP client launch it: the Inspector command under
[Testing a change](#testing-a-change) does that. The server tries to connect to
the device at startup. If the device is unreachable, it exits with
`RuntimeError: Failed to connect to Modbus tcp device` once a client connects,
so start the mock first.

### Lockfiles

Both projects commit `uv.lock`, and `uv sync` installs exactly what it pins.
That matters more than usual here: the dependency ranges in
`modbus-python/pyproject.toml` admit releases the server does not work with
(`mcp` 2.x removed `mcp.server.fastmcp`; `pymodbus` 3.10+ removed
`pymodbus.payload`), and the mock only starts on `pymodbus` 3.10–3.12. If you
change dependencies, run `uv lock` in that project, commit the lockfile, and
check that `uv sync --locked` still passes.

## Testing a change

There is no automated test suite yet. CI
([`.github/workflows/ci.yml`](.github/workflows/ci.yml)) runs cheap checks that
need no device; run the same ones locally, from the repository root, before
opening a pull request:

```bash
cd modbus-python
uv sync --locked
uv run --locked python -m compileall -q src
uv run --locked python -c "import asyncio; from modbus_mcp.cli import mcp; print(len(asyncio.run(mcp.list_tools())), 'tools')"

cd ../modbus-mock-server
uv sync --locked
uv run --locked python -m compileall -q modbus_local_server.py
uv run --locked python -c "import modbus_local_server as m; m.build_context(); print('datastore ok')"
```

Then drive the server the way a user would — through the
[MCP Inspector](https://modelcontextprotocol.io/docs/tools/inspector) or a real
client — against the mock. From the repository root:

```bash
npx @modelcontextprotocol/inspector \
  -e MODBUS_HOST=127.0.0.1 -e MODBUS_PORT=1502 \
  uv --directory modbus-python run modbus-mcp
```

Call the tool you changed, and check the envelope, not just the value.

> [!NOTE]
> The mock currently answers every register, coil and discrete-input request
> with a Modbus exception — see [Known issues](modbus-mock-server/README.md#known-issues).
> Until that is fixed, only `ping`, `list_tags` and `read_device_information`
> succeed against it. Separately, the typed tools fail with `"Big"` on any
> device — see the server's [Known issues](modbus-python/README.md#known-issues).

**Do not test against production equipment.** If a change genuinely cannot be
verified against the mock, say so in the pull request and describe the bench
you used.

## House rules for the code

- MCP speaks over **stdio**, so `stdout` belongs to the protocol. All logging
  goes to `stderr`. A stray `print()` in the server corrupts the session.
- Fail at startup, not at the first tool call, when configuration is unusable.
- An error is a returned `{ success: false, error }`, not an exception that
  kills the server. The model needs to be told what went wrong.
- Keep tool names and argument names aligned with the other IndustriConnect
  servers.

## Pull requests

Branch off `main` and keep commits focused. In the pull request, say how you
tested, and whether the change touches anything that can write to a device. If
a change was AI-assisted, keep the `Co-Authored-By:` trailer. For pull requests
from forks, enable **"Allow edits by maintainers"**.

## Code of Conduct

By taking part you agree to the [Code of Conduct](CODE_OF_CONDUCT.md).
