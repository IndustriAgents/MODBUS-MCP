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
│   └── tests/               # pytest: unit tests, plus integration tests against the mock
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
2. **Read-only by default.** Tools that write a coil or a register change
   physical equipment. Here they refuse to send anything unless the server was
   started with `MODBUS_WRITES_ENABLED=true` (the default is `false`). A new
   write tool must check the same switch.
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
[Testing a change](#testing-a-change) does that. The server connects to the
device at startup but starts either way; until the device answers, the device
tools return `"cannot reach device at <host>:<port>"`, and every call tries to
connect, so the mock can come up before or after the server.

### Lockfiles

Both projects commit `uv.lock`, and `uv sync` installs exactly what it pins.
The ranges in the two `pyproject.toml` files are the versions the tests pass
on: the server takes `mcp>=1.6.0,<2`, `pymodbus[serial]>=3.9.2,<3.16` and
`python-dotenv>=1.1.0`, the mock `pymodbus>=3.11.1,<3.13` (see their READMEs
for why). CI runs the server's tests on the locked versions and again with
`uv pip install --resolution lowest-direct`, i.e. the lowest version of every
direct dependency, and starts the mock on its lowest pymodbus. If you change a
lower bound, run the suite on it first (see "To check the lowest versions" in
[modbus-python/README.md](modbus-python/README.md#testing)); to try another
version, `uv pip install "pymodbus==X"` in the synced environment, then
`uv run --no-sync pytest`, then `uv sync --locked` to go back. If you change
dependencies, run `uv lock` in that project, commit the lockfile, and check
that `uv sync --locked` still passes.

## Testing a change

CI ([`.github/workflows/ci.yml`](.github/workflows/ci.yml)) runs ruff and the
test suite. Run the same steps locally, from the repository root, before
opening a pull request:

```bash
cd modbus-python
uv sync --locked
uv run --locked ruff check
uv run --locked ruff format --check
uv run --locked pytest

cd ../modbus-mock-server
uv sync --locked
uv run --locked ruff check
uv run --locked ruff format --check
uv run --locked python -c "import modbus_local_server as m; m.build_context(); print('datastore ok')"
```

`modbus-python/tests` holds unit tests (an in-memory fake device; no network)
and integration tests marked `integration`, which start the mock from
`modbus-mock-server` with `uv run --locked` on a free port and drive the real
server over stdio with the MCP client SDK. `uv run pytest -m "not integration"`
runs only the unit tests. A fix should come with a test that fails without it.

Then drive the server the way a user would — through the
[MCP Inspector](https://modelcontextprotocol.io/docs/tools/inspector) or a real
client — against the mock. From the repository root:

```bash
npx @modelcontextprotocol/inspector \
  -e MODBUS_HOST=127.0.0.1 -e MODBUS_PORT=1502 \
  uv --directory modbus-python run modbus-mcp
```

Add `-e MODBUS_WRITES_ENABLED=true` to exercise the write tools. Call the tool
you changed, and check the envelope, not just the value.

**Do not test against production equipment.** If a change genuinely cannot be
verified against the mock, say so in the pull request and describe the bench
you used.

## House rules for the code

- MCP speaks over **stdio**, so `stdout` belongs to the protocol. All logging
  goes to `stderr`. A stray `print()` in the server corrupts the session; ruff's
  `T20` rules reject `print` in the server code, and
  `tests/test_stdio_integration.py` runs the server unbuffered, calls every
  tool and checks that stdout carries only JSON-RPC. A new tool must be added
  to its `EVERY_TOOL` list (the test fails until it is).
- Read configuration in `main()`, never at import time, and fail at startup,
  not at the first tool call, when it is unusable. A setting that lets the
  server change the device (`MODBUS_WRITES_ENABLED`, `GATE_VARIABLES` in
  `cli.py`) comes only from the process environment or `--env-file`, never
  from a `.env` found automatically.
- A write reaches the device at most once. Only (re)connecting may be
  retried; once a request may have gone out, a lost reply is reported
  (`meta.outcome: "unknown"`, "may have been applied; check before retrying"),
  never retried. Write tools report `meta.outcome` and `meta.request_sent`.
- Tool arguments are strict: integers are JSON integers (`Field(strict=True)`),
  booleans are `true`/`false`.
- Device tools must work, or fail cleanly with `success: false`, when the
  device is unreachable; the server must start without it.
- An error is a returned `{ success: false, error }`, not an exception that
  kills the server. The model needs to be told what went wrong. Invalid
  arguments and unexpected exceptions are turned into that envelope too.
- Never report success for something that did not happen, and never return
  values the device did not send (no padding, truncating or rounding to make a
  call succeed).
- `Context.info()`/`Context.error()` are coroutines: `await` them.
- Every tool needs a docstring and parameter descriptions; they are what the
  model sees in `tools/list`.
- A safety test must be able to fail: check it by removing the protection it
  covers and watching it fail.
- Keep tool names and argument names aligned with the other IndustriConnect
  servers.

## Questions and security reports

Ask questions in [Discussions](https://github.com/IndustriAgents/MODBUS-MCP/discussions).
Report vulnerabilities privately through
[a security advisory](https://github.com/IndustriAgents/MODBUS-MCP/security/advisories/new),
never in a public issue (see [SECURITY.md](SECURITY.md)).

## Pull requests

Branch off `main` and keep commits focused. In the pull request, say how you
tested, and whether the change touches anything that can write to a device. If
a change was AI-assisted, keep the `Co-Authored-By:` trailer. For pull requests
from forks, enable **"Allow edits by maintainers"**.

## Code of Conduct

By taking part you agree to the [Code of Conduct](CODE_OF_CONDUCT.md).
