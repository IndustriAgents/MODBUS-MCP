## Summary

<!-- What does this PR do, and why? -->

## Component(s) affected

- [ ] MCP server (`modbus-python`)
- [ ] Mock device (`modbus-mock-server`)
- [ ] Docs / CI only

## Type of change

- [ ] Bug fix
- [ ] New tool
- [ ] Documentation
- [ ] Refactor / chore

## Does this change what can be written to a device?

- [ ] No
- [ ] Yes — describe the new capability. It must refuse to send anything unless `MODBUS_WRITES_ENABLED=true` (off by default):

## Checklist

- [ ] Tools still return the shared `{ success, data, error, meta }` envelope, also for invalid arguments and device errors.
- [ ] The mock device can exercise the change, and I tested against it.
- [ ] `uv run pytest`, `uv run ruff check` and `uv run ruff format --check` pass (in each project I touched), and the change has a test that fails without it.
- [ ] Nothing was tested against production equipment.
- [ ] `stdout` is still clean — all logging goes to `stderr`.
- [ ] If I changed dependencies, I ran `uv lock` and committed the `uv.lock`; `uv sync --locked` passes.
- [ ] I updated the README(s) where relevant.

## How to test

<!-- The exact commands and tool calls a reviewer can run against the mock. -->
