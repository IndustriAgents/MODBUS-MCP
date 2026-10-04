---
name: Feature request
about: Suggest a new tool or an improvement
title: "[Feature] "
labels: enhancement
assignees: ""
---

**What problem does this solve?**
The use case, from the point of view of someone asking an agent to do something.

**Which component?**
- [ ] MCP server (`modbus-python`)
- [ ] Mock device (`modbus-mock-server`)

**Proposed solution**
If it is a new tool: its name, arguments, and what it returns. Tool names and
arguments are kept consistent across the
[IndustriConnect](https://github.com/IndustriAgents/IndustriConnect) servers, so
say how it lines up with the equivalent tool elsewhere in the suite.

**Does it write to a device?**
- [ ] No — read-only
- [ ] Yes — it changes physical equipment

**Can the mock answer it?**
What the mock device would need to be able to simulate for this to be testable
without real plant.

**Additional context**
Links to the Modbus specification, example devices, or related issues.
