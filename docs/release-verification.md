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
| `industriagents_modbus_mcp-0.3.0-py3-none-any.whl` | `e9f5073bf480bfe7db7d3d43e752bba45842d2096a49e5e2742b9f73a03ab8ff` |
| `industriagents_modbus_mcp-0.3.0.tar.gz` | `8516defd94d47b1c369179f670f5a5017bc77e7c15491920e7f16cd8a8d83416` |
| `industriagents-modbus-mcp-0.3.0.tgz` | `25a352758833b4a43b9a165fb4f7a7639206cd1f566d833a2a81f471ca2675a3` |

See [release instructions](release.md) for independent registry publication.
