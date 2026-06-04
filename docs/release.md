# Release Checklist

Use this checklist before tagging or publishing peerpost.

## Local Verification

Run:

```sh
PYTHONPATH=src python -m compileall -q src tests
PYTHONPATH=src python -m unittest discover -v
tmpdir="$(mktemp -d)" && python -m pip wheel . --no-deps -w "$tmpdir" && rm -rf "$tmpdir"
```

Confirm:

- All tests pass.
- The wheel builds successfully.
- No generated files are left in the worktree.

## Runtime Smoke Test

Use an isolated state directory:

```sh
export PEERPOST_HOME="$(mktemp -d)"
export PEERPOST_SOCKET="/tmp/peerpost-release-$USER.sock"

peerpost quickstart --team dev
peerpost doctor --self-test
peerpost send --from claude --to codex --team dev "release smoke test"
peerpost status --team dev
peerpost drain --agent codex --team dev
peerpost backup
peerpost daemon stop
```

Confirm:

- `doctor --self-test` reports `status: ok`.
- `status --team dev` shows a pending delivery for `codex`.
- The drain output contains the smoke-test message once.
- A backup SQLite file is created.
- `peerpost daemon stop` shuts down cleanly.

## Release Notes

Before tagging:

- Confirm `pyproject.toml` and `src/peerpost/__init__.py` use the same version.
- Confirm README examples still match CLI behavior.
- Note any schema, path, hook-format, or daemon behavior changes.
- Note any data-loss risk and recommend `peerpost backup` before maintenance commands when relevant.

## CI

GitHub Actions runs compile, unittest, and wheel build on push and pull requests. Do not release from a commit whose CI result is failing or unknown.

The Pages workflow publishes the user guide from `docs/` to <https://s-hiraoku.github.io/peerpost/> on pushes to `main`. Confirm the latest Pages deployment is successful after release documentation changes.
