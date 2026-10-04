---
name: Bug report
about: Report a problem with the Modbus MCP server or the mock device
title: "[Bug] "
labels: bug
assignees: ""
---

**Which component?**
- [ ] MCP server (`modbus-python`, `modbus-mcp`)
- [ ] Mock device (`modbus-mock-server`)

**Describe the bug**
What happens, and what you expected instead.

**To reproduce**
The MCP client you used (Claude Desktop / Claude Code / Cursor / Inspector /
IndustriConnect's mcp-manager-ui), the tool you called, and the arguments you
passed.

1.
2.
3.

**Tool output**
```json
paste the { success, data, error, meta } envelope, or the error
```

**Connection**
- `MODBUS_TYPE`: tcp / udp / serial
- Other `MODBUS_*` settings that differ from the defaults (no credentials):

**What was on the other end?**
- [ ] The mock device from this repo
- [ ] Real equipment — vendor and model:

**Environment**
- OS:
- Python version:
- Commit SHA:
- Installed with `uv sync` from the committed `uv.lock`? yes / no

**Additional context**
Register addresses, unit/slave IDs, tag map, stderr logs — whatever would let
someone else reproduce it against the mock.
