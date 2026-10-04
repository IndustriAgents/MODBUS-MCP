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
- [ ] Yes — describe the new capability and how it is gated:

## Checklist

- [ ] Tools still return the shared `{ success, data, error, meta }` envelope.
- [ ] The mock device can exercise the change, and I tested against it.
- [ ] Nothing was tested against production equipment.
- [ ] `stdout` is still clean — all logging goes to `stderr`.
- [ ] If I changed dependencies, I ran `uv lock` and committed the `uv.lock`; `uv sync --locked` passes.
- [ ] I updated the README(s) where relevant.

## How to test

<!-- The exact commands and tool calls a reviewer can run against the mock. -->
