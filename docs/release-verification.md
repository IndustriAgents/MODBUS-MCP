# 0.3.0 local release verification

Verified on 2026-10-24. Package: `industriagents-modbus-mcp`.

This records local build verification after the OPCUA architecture review and
boundary hardening. It does not record a registry publication. The current
package version is retained; no tag, push or publication was performed.

## Checks

- Full Python regression run: 367 passed, 1 skipped.
- The full suite includes request depth/size bounds before dispatch, legacy
  unknown-argument rejection, cancellation admission release, text/structured
  audit consistency, and unexpected post-dispatch control failures.
- Ruff lint and formatting checks passed for source and tests.
- `uv lock --check` and contract/package version parity passed.
- Three npm launcher tests passed.
- Installed wheel and installed npm tarball initialized over stdio outside the
  checkout, reported the manifest version 0.3.0 and listed 6 canonical tools.
  Schemas matched the reviewed contract and invalid modes returned MCP errors.
  Smoke tests select exact manifest-version filenames, so stale archives cannot
  accidentally satisfy a later version's checks.
- `twine check --strict` passed for the wheel and source distribution.
- Workspace artifact inspection verified the current Python sources and
  contract inside the wheel and identical wheel bytes in the npm tarball.
- `git diff --check` passed.

Skipped tests: One legacy codec comparison is skipped because pymodbus >= 3.10 removed its payload module.

These are software and mock/loopback checks, not physical-device certification.
See [protocol capability limits and architecture differences](architecture.md).

## Second architecture review

- Reproduced a write followed by `ConnectionException` being sent six times.
  Retry eligibility now requires proof from the send counter that no frame left;
  sent or untracked failures return uncertainty after one attempt.
- Regressions cover both available and missing send-counter evidence while
  preserving pre-send reconnect retries.
- Shared policy and audit checks reject FIFOs/special files without blocking,
  oversized/deep policy documents and duplicate keys.
- All verification commands, rebuilt wheel/source/npm artifacts, installed
  stdio smokes and strict package metadata checks passed again after these fixes.

## Artifact SHA-256

Artifacts are local build outputs in `dist/` and `npm/`, excluded from Git.
Rebuilding can change archive hashes; these identify the verified files.

| Artifact | SHA-256 |
|---|---|
| `industriagents_modbus_mcp-0.3.0-py3-none-any.whl` | `b27beb2bd857270dbb37babf2f4c105aa0e03985e10f86eede916e7bc2cbd28e` |
| `industriagents_modbus_mcp-0.3.0.tar.gz` | `54dc775291003d89d5799dbdf29d72b9173508e74164186fd9b48e574ed6a1c6` |
| `industriagents-modbus-mcp-0.3.0.tgz` | `60fc9aa67c2f8a213556fb0181e9663b0a73ed9a0953f7380fc59acb9d7bcba3` |

See [release instructions](release.md) for independent registry publication.
