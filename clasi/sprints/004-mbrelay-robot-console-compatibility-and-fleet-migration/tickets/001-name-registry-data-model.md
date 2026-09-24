---
id: '001'
title: Name registry data model
status: open
use-cases: [SUC-001, SUC-003, SUC-004]
depends-on: []
github-issue: ''
issue: mbrelay-relay-protocol-client-over-mbregistry.md
completes_issue: true
---
<!-- CLASI: Before changing code or making plans, review the SE process in CLAUDE.md -->

# Name registry data model

## Description

Add the robot name → (channel, group) mapping as a new `name_registry`
table in `registry.store`, foundation work for `mbrelay`'s CLI (ticket
005) and the robot-console compatibility endpoints (tickets 006/007).
Conceptually ports `microbit-radio-relay/server/src/mbrelay/registry.py`
`NameRegistry`'s query/conflict-detection semantics, SQL-backed instead
of a `names.json` file, with the old TOML `pins` precedence tier dropped
(sprint architecture Decision 7 — only `derived`/`registry` sources
exist here).

**Approach**
- `CREATE TABLE IF NOT EXISTS name_registry (name TEXT PRIMARY KEY,
  channel INTEGER NOT NULL, radio_group INTEGER NOT NULL, source TEXT
  NOT NULL, updated REAL NOT NULL)` — new table, no `ALTER TABLE` needed
  (lower risk than sprint 003's device-column additions).
- Port `naming.py` verbatim (pure functions: `validate`, `name_to_radio`,
  `radio_to_name`, `canonical_form`) into a shared location `relay.naming`
  usable by both `registry.store` (address derivation) and `relay.protocol`
  (ticket 003) — check against the original repo's sha256 canonical-form
  test vector.
- Add `Store` methods: `resolve(name) -> Entry` (returns existing row, or
  derives via `naming.name_to_radio` and persists a `source="derived"`
  row on miss), `get(name) -> Entry | None` (non-creating), `set(name,
  channel, group) -> Entry` (persists `source="registry"`), `clear(name)`
  (deletes the row — a later `resolve` re-derives), `name_for(channel,
  group) -> str | None` (reverse lookup), `conflicts()` (same
  channel+group used by two names — error severity),
  `channel_conflicts()` (same channel, different group — warning
  severity), `listing()` (all rows, annotated with conflict flags).
- `Entry` dataclass: `name, channel, group, source, updated`.

**Files to create/modify**
- `src/mbtools/relay/naming.py` (new — ported pure functions).
- `src/mbtools/registry/store.py` (extended — schema + methods above).
- `tests/` — new unit tests for the ported `naming` functions and the new
  `Store` name-registry methods (resolve/get/set/clear/name_for/conflicts).

**Documentation updates**: none required by this ticket alone (the
schema is internal); ticket 007 documents the external `/names` HTTP
shape.

## Acceptance Criteria

- [ ] `name_registry` table is created idempotently on both a fresh
      database and an existing sprint-1/2/3-shape `devices.db`.
- [ ] `Store.resolve()` derives and persists a `source="derived"` entry
      on first ask for an unseen, well-formed name, and returns the
      existing entry unchanged on a repeat ask.
- [ ] `Store.set()`/`Store.clear()` correctly move an entry between
      `source="registry"` and re-derivable (post-clear, the next
      `resolve()` re-derives).
- [ ] `Store.conflicts()` flags two names sharing the same
      (channel, group) as an error-severity conflict;
      `Store.channel_conflicts()` flags two names sharing the same
      channel with different groups as a warning-severity conflict.
- [ ] `relay.naming`'s ported functions pass the original repo's
      canonical-form sha256 test vector unchanged.
- [ ] `Store.resolve()` is idempotent under concurrent/duplicate calls
      for the same unseen name (same deterministic derived value each
      time, no crash on a race to insert).

## Testing

- **Existing tests to run**: `uv run pytest tests/registry/test_store.py`
  (confirm no regression to `device`/`peer` table behavior).
- **New tests to write**: `tests/relay/test_naming.py` (ported test
  vectors), `tests/registry/test_store_name_registry.py` (resolve/get/
  set/clear/name_for/conflicts/channel_conflicts/listing, schema
  creation on a pre-existing sprint-3-shape database fixture).
- **Verification command**: `uv run pytest tests/relay/test_naming.py
  tests/registry/test_store_name_registry.py tests/registry/test_store.py`
