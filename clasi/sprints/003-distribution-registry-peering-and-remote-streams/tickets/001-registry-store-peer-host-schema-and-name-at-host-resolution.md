---
id: '001'
title: 'registry.store: peer/host schema and name@host resolution'
status: open
use-cases: [SUC-001, SUC-002]
depends-on: []
github-issue: ''
issue: mbregistry-peering-mdns-and-zeromq.md
completes_issue: true
---
<!-- CLASI: Before changing code or making plans, review the SE process in CLAUDE.md -->

# registry.store: peer/host schema and name@host resolution

## Description

Per sprint.md's Architecture (module `registry.store`, Step 5 "What
Changed", the ERD), extend `mbtools.registry.store` with the data model
this whole sprint depends on: which host owns a device, a `peer` table
tracking discovered registries, and disambiguated name resolution when
two hosts share a device name. This is foundation work — every later
ticket (peering, remote_api, render) reads or writes these fields.

No other module changes in this ticket. `registry.peering` (ticket 004)
and `registry.remote_api` (ticket 006) are this module's first real
callers; this ticket's own tests exercise the new methods directly
against a `Store` instance, the same way sprint 001's `store` tests do.

## Acceptance Criteria

- [ ] `device` table gains three nullable columns: `host` (owning peer's
      hostname; `NULL` means local), `remote_lock_kind`, and
      `remote_lock_display` (the cached, display-only lock state for a
      remote-owned row — never consulted for a local row, which always
      reads live `LockManager` state instead).
- [ ] A new `peer` table exists: `host` (PK), `endpoint` (`host:port`),
      `last_seen` (REAL), `reachable` (bool/int).
- [ ] `Store.__init__` migrates an existing on-disk database created by
      sprint 001/002's schema in place: it checks `PRAGMA table_info` for
      each new `device` column before issuing `ALTER TABLE ... ADD
      COLUMN`, and uses `CREATE TABLE IF NOT EXISTS` for `peer` — never a
      destructive rebuild. A fresh database (no prior schema) also ends
      up with the full new schema via the same code path, not a separate
      branch.
- [ ] New write methods: `upsert_remote_attached(uid, host, ...same
      identity fields as upsert_attached)`, `mark_remote_detached(uid)`
      (or equivalent detach-by-uid for a remote row), `apply_remote_probe
      (uid, ProbeResult | None)` (remote counterpart to
      `apply_probe_result`), `apply_remote_lock_state(uid, kind: str |
      None, display: str | None)` (sets or clears the cache columns —
      never touches `LockManager`), `record_peer_seen(host, endpoint)`,
      `mark_peer_unreachable(host)` / `mark_peer_reachable(host)`.
- [ ] New read methods: `list_peers()`, `get_peer(host)`,
      `snapshot_local_devices()` (every `host IS NULL` row, shaped for
      `registry.peering`'s snapshot-exchange payload).
- [ ] `Store.find(token)` accepts an optional `name@host` suffix (e.g.
      `"zavaz@loki"`) and resolves it to the device whose `device_name`
      matches case-insensitively **and** whose `host` matches (`host`
      compared case-insensitively; a bare hostname, not `host:port`).
- [ ] `Store.find(token)` without an `@host` suffix, when the
      `device_name` match is ambiguous across more than one `host` value
      (including the local `NULL` host as one candidate), raises a new
      `AmbiguousNameError` (a plain Python exception in this module,
      exported from `__all__`) rather than silently returning one
      candidate. A `uid`/`short_uid` match is never ambiguous by
      definition and is unaffected.
- [ ] Every new method follows this module's existing conventions:
      guarded by `self._lock`, raises `KeyError` for an unknown `uid`
      where `store`'s existing single-device methods already do, never
      deletes a row.
- [ ] `docs/design/registry-api.md` gets a short new section (or an
      addendum) documenting the `host`/`remote_lock_*` device fields and
      the `peer` table shape, since `registry.remote_api` (ticket 006+)
      will need to describe what its `list` response now includes.

## Testing

- **Existing tests to run**: the full `tests/registry/store/` suite —
  must pass unchanged, proving the migration and new columns don't
  disturb existing local-device behavior.
- **New tests to write**:
  - Migration test: construct a `Store` against a `tmp_path` db file
    pre-populated with the sprint-1/2 schema (no `host`/`peer`), then
    open it with the new `Store` and confirm the new columns/table exist
    and existing rows are untouched.
  - CRUD tests for every new write/read method above.
  - `find("name@host")` resolves correctly; `find("name")` raises
    `AmbiguousNameError` when two hosts share a name; `find("name")`
    still resolves cleanly when only one host has that name.
  - `apply_remote_lock_state` never calls into `LockManager` (it isn't
    even constructed in this module — confirm by not passing one to any
    test fixture that exercises this method).
- **Verification command**: `uv run pytest tests/registry/store/`
