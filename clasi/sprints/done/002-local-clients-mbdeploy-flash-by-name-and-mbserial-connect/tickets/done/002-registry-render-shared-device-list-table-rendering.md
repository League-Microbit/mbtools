---
id: '002'
title: 'registry.render: shared device-list table rendering'
status: done
use-cases:
- UC-004
depends-on: []
github-issue: ''
issue: mbdeploy-flash-by-name-via-mbregistry.md
completes_issue: true
---
<!-- CLASI: Before changing code or making plans, review the SE process in CLAUDE.md -->

# registry.render: shared device-list table rendering

## Description

Create `mbtools.registry.render`, extracting `registry.cli`'s
`_table`/`_state_cell`/`_firmware_cell` (sprint 001, ticket 009) into a
standalone, pure-presentation module — no socket calls, no argparse, just
"list of device dicts in, table string or JSON-ready structure out".

Per spec §4.3 ("`mbdeploy list` is a thin view over the registry; it can
be shared with `mbregistry list`") and sprint.md's Architecture, this is
the module both `mbregistry list` (refactored) and `mbdeploy list`
(ticket 008, new) call — one rendering implementation, not two.

Independent of ticket 001 (no shared code between them — this ticket
touches only presentation, ticket 001 only transport), so it can be
built in either order; listed second here because ticket 008 (`mbdeploy
list`) needs both.

## Acceptance Criteria

- [x] `mbtools.registry.render` exposes a function that takes the list of
      device dicts `registry.client.list()` (ticket 001) returns and
      produces the same STATE/NAME/UID/FIRMWARE/PORT table
      `mbregistry list` produces today, plus a `--json`-equivalent
      structured form.
- [x] `_table`/`_state_cell`/`_firmware_cell` are removed from
      `registry.cli` (not duplicated) — `registry.cli`'s `cmd_list` calls
      the new module.
- [x] `mbregistry list`'s table and `--json` output are byte-for-byte
      unchanged from sprint 001's behavior — verified by re-running
      sprint 001's existing `cmd_list` tests unmodified.
- [x] The extracted function takes plain device dicts as input (not a
      live socket or a `Store` instance), so `mbdeploy list` (ticket 008)
      can call it against `registry.client.list()`'s return value with no
      adapter code in between.

## Testing

- **Existing tests to run**: `uv run pytest tests/registry/cli/` (sprint
  001's `cmd_list` table/`--json` tests, must pass unmodified against the
  refactored code).
- **New tests to write**: pure unit tests over hand-built device-dict
  lists (no socket, no daemon) covering every STATE value (free / locked
  by kind+pid / no-firmware / gone) and both table and JSON output
  shapes.
- **Verification command**: `uv run pytest tests/registry/`
