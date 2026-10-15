# 0.3.0 local release verification

Verified on 2026-10-15. Package: `industriagents-modbus-mcp`.

This records local build verification, not registry publication. Check the
GitHub Actions runs and registry version pages for publication status. Registry
authentication/trusted publishers must be configured before release; GitHub
repository access alone does not authorize registry publication.

## Checks

- Full Python regression run: 338 passed, 1 skipped.
- Final architecture/contract tests: 16 passed (a separate run, not additional unique tests).
- Ruff lint and formatting checks passed for source and tests.
- `uv lock --check` and contract/package version parity passed.
- Three npm launcher tests passed.
- Installed wheel and installed npm tarball both initialized over stdio outside
  the checkout, reported version 0.3.0 and listed 6 canonical tools.
  Their schemas matched the contract; invalid mode calls returned MCP errors.
- `twine check --strict` passed for the wheel and source distribution.
- The npm tarball contains exactly the same wheel bytes as the PyPI artifact.
- `git diff --check` passed.

Skipped test: Legacy pymodbus payload module absent in pymodbus >= 3.10.

These are software and mock/loopback checks, not physical-device certification.
See [protocol capability limits](architecture.md#protocol-capability-limits).

## Artifact SHA-256

Artifacts are local build outputs in `dist/` and `npm/`, excluded from Git.
Rebuilding can change archive hashes; these identify the verified files.

| Artifact | SHA-256 |
|---|---|
| `industriagents_modbus_mcp-0.3.0-py3-none-any.whl` | `3ca9005e07bbed197400274fbed7d3bd7abc8e4ac92db938e46705facf678f77` |
| `industriagents_modbus_mcp-0.3.0.tar.gz` | `0611f150a372b25b1f4c80da3502f2f01744fac666ce531c3c8bd56ca5ab1d87` |
| `industriagents-modbus-mcp-0.3.0.tgz` | `59a9ea78ecb0bca1b57cce9e609142ba9dc99fd9f62d8ad8aa7af9f3dd4631b2` |

See [release instructions](release.md) for independent registry publication.
