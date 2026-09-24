---
id: '001'
title: 'registry.store: peer/host schema and name@host resolution'
status: done
use-cases:
- SUC-001
- SUC-002
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

- [x] `device` table gains three nullable columns: `host` (owning peer's
      hostname; `NULL` means local), `remote_lock_kind`, and
      `remote_lock_display` (the cached, display-only lock state for a
      remote-owned row — never consulted for a local row, which always
      reads live `LockManager` state instead).
- [x] A new `peer` table exists: `host` (PK), `endpoint` (`host:port`),
      `last_seen` (REAL), `reachable` (bool/int).
- [x] `Store.__init__` migrates an existing on-disk database created by
      sprint 001/002's schema in place: it checks `PRAGMA table_info` for
      each new `device` column before issuing `ALTER TABLE ... ADD
      COLUMN`, and uses `CREATE TABLE IF NOT EXISTS` for `peer` — never a
      destructive rebuild. A fresh database (no prior schema) also ends
      up with the full new schema via the same code path, not a separate
      branch.
- [x] New write methods: `upsert_remote_attached(uid, host, ...same
      identity fields as upsert_attached)`, `mark_remote_detached(uid)`
      (or equivalent detach-by-uid for a remote row), `apply_remote_probe
      (uid, ProbeResult | None)` (remote counterpart to
      `apply_probe_result`), `apply_remote_lock_state(uid, kind: str |
      None, display: str | None)` (sets or clears the cache columns —
      never touches `LockManager`), `record_peer_seen(host, endpoint)`,
      `mark_peer_unreachable(host)` / `mark_peer_reachable(host)`.
- [x] New read methods: `list_peers()`, `get_peer(host)`,
      `snapshot_local_devices()` (every `host IS NULL` row, shaped for
      `registry.peering`'s snapshot-exchange payload).
- [x] `Store.find(token)` accepts an optional `name@host` suffix (e.g.
      `"zavaz@loki"`) and resolves it to the device whose `device_name`
      matches case-insensitively **and** whose `host` matches (`host`
      compared case-insensitively; a bare hostname, not `host:port`).
- [x] `Store.find(token)` without an `@host` suffix, when the
      `device_name` match is ambiguous across more than one `host` value
      (including the local `NULL` host as one candidate), raises a new
      `AmbiguousNameError` (a plain Python exception in this module,
      exported from `__all__`) rather than silently returning one
      candidate. A `uid`/`short_uid` match is never ambiguous by
      definition and is unaffected.
- [x] Every new method follows this module's existing conventions:
      guarded by `self._lock`, raises `KeyError` for an unknown `uid`
      where `store`'s existing single-device methods already do, never
      deletes a row.
- [x] `docs/design/registry-api.md` gets a short new section (or an
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

## Implementation Notes

- **Module**: `mbtools.registry.store` only — no other module touched,
  per this ticket's own scope statement. Full new schema/method
  documentation is in `docs/design/registry-api.md`'s new "Store schema
  additions for peering (sprint 003, ticket 001)" section.
- **Migration mechanics**: `_SCHEMA` (the `device` table's
  `CREATE TABLE IF NOT EXISTS`) was left at its exact sprint-1/2 shape,
  and a new `Store._migrate_schema()` runs unconditionally right after
  it on every construction — checking `PRAGMA table_info(device)` and
  issuing `ALTER TABLE ... ADD COLUMN` for each of `host`/
  `remote_lock_kind`/`remote_lock_display` only if missing, then
  `CREATE TABLE IF NOT EXISTS peer`. This is what makes a fresh database
  and an upgraded sprint-1/2 database converge on the identical schema
  through one code path (both start with a `device` table in the old
  shape — one because it was just created that way, one because it
  already was — and both get the same ALTERs applied), rather than a
  separate "if fresh" branch.
- **`upsert_attached`/`upsert_remote_attached` share one private
  `_upsert_device(uid, host, port, vid_pid)`** — `upsert_attached`
  calls it with `host=None`. This keeps the reattach/probe-eligibility
  logic (new record, or `disconnected` -> reattach resets `last_probe`;
  otherwise announcement fields are untouched) in exactly one place
  instead of duplicating it, and guarantees `upsert_attached`'s
  externally observable behavior is byte-for-byte what it was before
  this ticket (proven by the existing `tests/registry/store/test_store.py`
  suite passing unchanged).
- **`mark_remote_detached`/`apply_remote_probe` are thin wrappers**
  around `mark_disconnected`/`apply_probe_result` respectively — the
  store-side effect is identical for a local or remote row (`state`
  transition, announcement-field preservation rules), so the acceptance
  criteria's "or equivalent" allowance for `mark_remote_detached` was
  taken literally rather than duplicating logic that has no local/remote
  distinction at this layer.
- **`find()`'s `@host` handling** short-circuits before the
  uid/short_uid/device_name precedence chain: any token containing `@`
  is treated entirely as `name@host` (split on the *last* `@`) and
  resolved with one query (`device_name` + `host`, both
  `COLLATE NOCASE`) — no uid/short_uid check is attempted for it, since
  neither ever contains `@`. The bare-name ambiguity check groups matches
  by `host.lower()` (so `"Loki"`/`"loki"` count as the same host) but
  reports the original casing in `AmbiguousNameError.hosts`.
  `AmbiguousNameError` is a plain `Exception` subclass (not
  `KeyError`-derived, since it is a different failure mode — multiple
  matches, not zero) carrying `token`/`hosts` for a future caller
  (`registry.remote_api`, ticket 006+) to build a wire-protocol error
  code from; no `CODE_*` constant was added in this ticket since nothing
  yet calls `find()` from the API layer with error-code translation.
- **`DeviceRecord` field order**: `host`/`remote_lock_kind`/
  `remote_lock_display` were inserted after `error_note`, before
  `flash_count`. `DeviceRecord` is the only place this dataclass is
  constructed (confirmed via a repo-wide grep before making the change),
  so the new fields are safe for every existing caller that only reads
  attributes or calls `dataclasses.asdict()` (`registry.api`'s
  `_device_dict`) — no test in `tests/registry/api/` asserts an exact,
  closed set of device-dict keys, so the extra fields don't break
  anything there; confirmed by the full suite run below.
- **Tests**: new file `tests/registry/store/test_store_peer_host.py`
  (35 tests) rather than growing `test_store.py` further — a distinct
  concern (peering/host schema) with its own fixtures, and keeps the
  existing file's diff clean. Basename checked unique across `tests/`
  before creating it, per this project's "no `__init__.py` packages"
  constraint.
- **Test run**: `uv run pytest tests/registry/store/ -q` → 65 passed (30
  existing + 35 new), unchanged existing tests included. Full suite
  `uv run pytest -q` → 354 passed, 2 skipped (the platform-gated
  `SO_PEERCRED`/`LOCAL_PEERPID` tests, skipped on this dev machine's
  platform as before) — no regressions anywhere else in the tree.
- **No deviations** from the ticket's acceptance criteria or method
  signatures as written.
