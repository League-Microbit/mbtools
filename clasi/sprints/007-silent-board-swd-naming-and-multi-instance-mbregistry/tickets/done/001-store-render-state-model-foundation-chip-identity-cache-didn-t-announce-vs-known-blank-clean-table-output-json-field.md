---
id: '001'
title: 'Store/render state-model foundation: chip-identity cache, didn''t-announce
  vs known-blank, clean table output, --json field'
status: done
use-cases:
- SUC-002
- SUC-003
depends-on: []
github-issue: ''
issue: name-silent-boards-over-swd-and-keep-list-table-clean.md
completes_issue: true
---
<!-- CLASI: Before changing code or making plans, review the SE process in CLAUDE.md -->

# Store/render state-model foundation: chip-identity cache, didn't-announce vs known-blank, clean table output, --json field

## Description

Lay the data-layer and presentation-layer groundwork that ticket 003's
SWD read will populate. This ticket does **not** add the SWD read itself
(ticket 003 does, after this ticket and ticket 002 land) — it makes
`store.py`/`render.py` ready to hold and display a chip-identity value,
and fixes the `STATE`/`FIRMWARE`/table-output problems that are visible
today even without any SWD code, per sprint.md's Architecture (Step 3:
`registry.store`, `registry.render`) and Design Rationale ("repurpose
`STATE_CONNECTED_NO_FIRMWARE`...").

Two independent fixes land here together because they touch the same
two files and the same tests:

1. **Chip-identity cache + three-way state.** Add persisted columns to
   the `device` table (name + decimal serial derived from
   `FICR.DEVICEID[1]`, independent of the announcement-derived
   `device_name`), migrated in place the same way sprint 003's
   `_NEW_DEVICE_COLUMNS` extended an existing table
   (`store.py`'s `_migrate_schema`). Add a new `STATE` constant for
   "didn't announce" (exact name your call — see sprint.md Open Question
   1 — a `STATE_ATTACHED_NO_ANNOUNCE`-shaped name that fits a
   fixed-width `STATE` column is a reasonable default).
   `STATE_CONNECTED_NO_FIRMWARE` narrows to mean *only* "known blank"
   from this ticket forward. `apply_probe_result(uid, None)` moves to
   the new didn't-announce state; add a sibling method (mirroring the
   existing `apply_remote_probe`/`apply_probe_result` pattern) for the
   call site that needs "known blank" instead — ticket 003 wires the
   flash-triggered re-probe path to call it, but the method itself
   belongs here since it's store-layer, not daemon-layer.
2. **Render fixes.** `render.py`'s `NAME` cell falls back to the new
   chip-identity cache when `device_name` is blank (a no-op until ticket
   003 ever populates that cache — write this ticket's tests against a
   store row you set the cache column on directly). `_state_cell`/
   `_firmware_cell` read the new three-way state (`unknown` for
   didn't-announce, `no firmware` only for known-blank).
   `render_table` stops appending any line after the table.
   `render_json` carries the same detail as a structured field on each
   device's dict instead of relying on `error_note` prose.

## Acceptance Criteria

- [x] `device` table has new persisted column(s) for the chip-identity
      cache (name + decimal serial), added via the same in-place
      migration pattern as `_NEW_DEVICE_COLUMNS`; a pre-existing
      `devices.db` from before this ticket opens and migrates cleanly.
- [x] A new `STATE` constant exists for "didn't announce";
      `STATE_CONNECTED_NO_FIRMWARE` is documented (docstring) as meaning
      only "known blank" from this point forward.
- [x] `apply_probe_result(uid, None)` sets the new didn't-announce
      state (not `STATE_CONNECTED_NO_FIRMWARE`).
- [x] A new store method (or an added parameter) lets a caller record a
      known-blank outcome explicitly, distinct from
      `apply_probe_result`'s didn't-announce default.
- [x] Every existing test asserting `STATE_CONNECTED_NO_FIRMWARE` for a
      plain silent-probe case is updated to assert the new
      didn't-announce state instead — a full-repo grep for
      `STATE_CONNECTED_NO_FIRMWARE` turns up only genuinely-known-blank
      call sites and tests once this ticket is done.
- [x] `render.render_table`'s `NAME` column reads the chip-identity
      cache when `device_name` is blank and the cache is set.
- [x] `render._state_cell`/`_firmware_cell` produce distinct text for
      all three states (announced, didn't-announce, known-blank),
      exercised directly in `tests/registry/render/`.
- [x] `render_table`'s output never contains a line after the table,
      even for a device with a non-empty `error_note`/structured detail
      field — verified with a test asserting the returned string's line
      count equals `2 + len(devices)` (header + rule + one row each).
- [x] `render_json`'s structured form carries the same detail a removed
      `error_note` line used to convey, as a field on the device dict.
- [x] `mbdeploy list` (which imports `registry.render` unchanged) is
      covered by at least one test confirming it renders the same
      table shape with no code change on the `mbdeploy` side.

## Implementation Plan

**Approach**: Data layer first (`store.py`), then presentation
(`render.py`), each with its own focused tests, since `render.py`'s
tests can exercise the new state constants and cache column directly on
hand-built device dicts without needing `store.py`'s migration path to
be correct yet — but land both in one ticket since they're the same
conceptual change (the state-model split) viewed from two layers.

**Files to modify**:
- `src/mbtools/registry/store.py`: new column(s) in `_NEW_DEVICE_COLUMNS`
  (or a new list following that same pattern), `DeviceRecord` gains the
  new field(s), `_row_to_record` updated, new `STATE_*` constant added
  to `__all__`, `apply_probe_result` updated, new sibling method for the
  known-blank case added.
- `src/mbtools/registry/render.py`: `_state_cell`, `_firmware_cell`,
  `render_table`, `render_json` updated per Description above; the
  `NAME`-cell lookup in `render_table`'s row-building list comprehension
  gains the chip-identity fallback.
- `src/mbtools/registry/daemon.py`: only the minimal change needed so
  the module still imports/runs against the renamed state semantics
  (e.g. the flash-pending-timeout log message at `run_once`'s "marking
  no-firmware" line may need to route to the new sibling method instead
  of the plain `apply_probe_result` — coordinate with ticket 003, which
  owns the full flash-triggered-known-blank wiring; this ticket only
  needs `daemon.py` to keep passing its existing tests against the
  renamed constant, not to add ticket 003's new call sites).
- Any test file under `tests/registry/store/` and
  `tests/registry/render/` (and `tests/registry/daemon/` for the
  renamed-constant fallout) asserting the old
  `STATE_CONNECTED_NO_FIRMWARE`-for-silent-board behavior.

**Testing plan**:
- `tests/registry/store/test_store.py` (or wherever schema-migration
  tests live): a fresh-DB test and an upgrade-from-old-schema test for
  the new column(s), following the existing `_NEW_DEVICE_COLUMNS` test
  pattern.
- New tests for the chip-identity cache: set-once, read-back, and
  "never overwritten by a later announcement-only update" (i.e.
  `apply_probe_result` with a real `ProbeResult` must not clear the
  cache column).
- New tests for the three-way state: an ordinary `apply_probe_result(
  uid, None)` lands on didn't-announce; the new known-blank method lands
  on `STATE_CONNECTED_NO_FIRMWARE`.
- `tests/registry/render/test_render.py`: table/JSON output for all
  three states; the no-text-after-table invariant; the `NAME` fallback.
- Run: `uv run pytest tests/registry/store/ tests/registry/render/
  tests/registry/daemon/ tests/registry/cli/ -x` (scoped to the modules
  this ticket touches, per the project's per-ticket test-scoping rule —
  the full suite runs once at `close_sprint`).

**Documentation updates**: none in this ticket — ticket 006 covers
`docs/service.md`/`docs/design/registry-api.md` for the whole sprint,
once the final state-constant name (Open Question 1) and JSON field
shape are settled by this ticket and ticket 003.

## Implementation Notes

- **Open Question 1 resolved**: the new `STATE` constant is
  `STATE_ATTACHED_NO_ANNOUNCE = "attached_no_announce"`, matching
  sprint.md's own suggested-default shape. Rendered as `no-answer` in
  the `STATE` column and `unknown` in `FIRMWARE`.
- **New store columns**: `device.chip_identity_name` (TEXT),
  `device.chip_identity_serial` (INTEGER), added via a new
  `_CHIP_IDENTITY_COLUMNS` list (kept separate from `_NEW_DEVICE_COLUMNS`,
  which is documented as specifically "the three sprint-003 columns") but
  migrated through the same `_migrate_schema` loop/`ALTER TABLE ADD
  COLUMN` pattern. `Store.set_chip_identity(uid, name, serial)` is
  write-once per uid (a second call on an already-cached uid is a no-op,
  returning the existing record unchanged) — the store's own second line
  of defense behind ticket 003's daemon-side "don't call SWD twice" gate.
- **New sibling method**: `Store.apply_known_blank(uid)` — sets
  `STATE_CONNECTED_NO_FIRMWARE` with a distinct `error_note`
  ("board confirmed blank (no firmware) after re-probe"), preserving
  announcement fields exactly like `apply_probe_result(uid, None)` does.
  Not yet wired into `daemon.py`'s flash-reprobe-timeout give-up path —
  per this ticket's own Implementation Plan, that wiring is ticket 003's
  job. `daemon.py`'s existing `apply_probe_result(uid, None)` call there
  now lands on `STATE_ATTACHED_NO_ANNOUNCE` (updated in
  `tests/registry/daemon/test_daemon.py`); flagged here explicitly so
  ticket 003 knows to route that specific call site to
  `apply_known_blank` instead.
- **Full-repo grep verified**: every remaining
  `STATE_CONNECTED_NO_FIRMWARE`/`connected_no_firmware` occurrence after
  this ticket is either (a) the constant's own definition/docs, (b) a
  genuinely-known-blank call site/test (`apply_known_blank` and its
  tests, the render test built directly on that state), or (c)
  `registry.peering`'s existing wire-value branch match, which sprint.md's
  Architecture explicitly leaves unmodified this sprint — its own test
  (`test_apply_event_identity_no_firmware`) was updated to assert the
  correct *resulting* local state (`attached_no_announce`) while still
  feeding in the old wire value that triggers that unmodified branch.
- **render.py**: `render_table` no longer appends any line after the
  table (the per-device trailing `error_note` loop was removed
  entirely); `NAME` falls back to `chip_identity_name` when
  `device_name` is blank; `_state_cell`/`_firmware_cell` handle the new
  three-way state. `render_json` required no code change — `error_note`
  (and the new chip-identity fields) already flow through via
  `asdict(record)`; new tests confirm this explicitly per the ticket's
  acceptance criterion.
- Full test suite (`uv run pytest tests/ -q`, 1018 passed / 3 skipped)
  run as an extra verification pass beyond the ticket-scoped run, given
  how many files across the repo call `apply_probe_result`; no
  regressions found outside the files listed below.
