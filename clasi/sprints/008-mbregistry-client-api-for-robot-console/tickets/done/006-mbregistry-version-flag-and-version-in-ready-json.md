---
id: '006'
title: mbregistry --version flag and version in --ready-json
status: done
use-cases: []
depends-on: []
github-issue: ''
issue: mbregistry-api-for-robot-console-watch-lock-label-unlock-force-local-stream.md
completes_issue: false
---
<!-- CLASI: Before changing code or making plans, review the SE process in CLAUDE.md -->

# mbregistry --version flag and version in --ready-json

## Description

robot-console checks a minimum `mbregistry` version before spawning or
using it, and fails closed if it can't determine the version. There is
currently no way to query the version from the CLI or from
`--ready-json`.

Add a top-level `mbregistry --version` argparse flag (works without a
subcommand) that prints `mbregistry <version>` to stdout — where
`version = importlib.metadata.version("mbtools")` (e.g.
`mbregistry 0.20260924.6`) — and exits 0.

Also add a top-level `"version": "<same string>"` key to the
`--ready-json` line emitted by `src/mbtools/registry/cli.py` (sprint
007 ticket 005).

## Acceptance Criteria

- [x] `mbregistry --version` works without a subcommand, prints
      `mbregistry <version>` to stdout using
      `importlib.metadata.version("mbtools")`, and exits 0.
- [x] The `--ready-json` line gains a top-level `"version"` key equal to
      the same version string.
- [x] Unit tests cover both the `--version` flag and the new
      `--ready-json` key.
- [x] `docs/service.md` documents the new `--version` flag.
- [x] `docs/design/registry-api.md` documents the new `version` key in
      the `--ready-json` shape.

## Testing

- **Existing tests to run**: `uv run pytest tests/registry/test_cli.py -q`
  (and any other existing `--ready-json` tests)
- **New tests to write**: a test asserting `mbregistry --version` prints
  `mbregistry <version>` and exits 0; a test asserting the
  `--ready-json` output includes the matching `"version"` key.
- **Verification command**: `uv run pytest tests/registry -q`
