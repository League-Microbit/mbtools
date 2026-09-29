---
id: '001'
title: 'Store: purge primitives for gone devices and unreachable peers'
status: done
use-cases:
- SUC-001
- SUC-002
depends-on: []
github-issue: ''
issue: mbregistry-rescan-command.md
completes_issue: true
---
<!-- CLASI: Before changing code or making plans, review the SE process in CLAUDE.md -->

# Store: purge primitives for gone devices and unreachable peers

## Description

`store.py`'s "never delete, always update in place" precedent is
correct for every existing write path but leaves `gone` device rows
and `peer unreachable` rows (plus the device rows they own) to
accumulate forever on a long-running daemon. This ticket adds the two
new, explicitly-scoped `Store` primitives `rescan` (sprint 010) needs
to purge exactly the stale rows and nothing else, race-safely. This is
the data layer only — no API op, no CLI, no lock-awareness (see
sprint.md's Step 3: `store` doesn't know about `locks`; the lock-skip
decision is ticket 002's job, one layer up, using these primitives'
own "only delete a uid I tell you to" contract to exclude locked uids
before calling `purge`).

See sprint.md's Architecture (Step 3 "store", Step 5 "What Changed")
and the issue (`clasi/issues/mbregistry-rescan-command.md`) for the
full behavioral spec this ticket implements the storage half of.

## Acceptance Criteria

- [x] `Store.candidates_for_purge()` returns, without mutating
      anything: every local device row in `STATE_DISCONNECTED`; every
      device row whose `host` names a peer currently in the `peer`
      table with `reachable = 0` (regardless of that device row's own
      `state`); every remote-owned device row in `STATE_DISCONNECTED`
      whose owning peer is *reachable* (stale since sprint 005 ticket
      011 — a reachable peer no longer advertises disconnected rows in
      its snapshot); every `peer` row with `reachable = 0`. Shape:
      something a caller can both act on (uids/hosts) and render
      directly for `--dry-run` (e.g. a small dataclass grouping device
      uids by reason and peer hosts).
- [x] `Store.purge(device_uids, peer_hosts)` deletes exactly the given
      uids/hosts, each under a conditional `DELETE ... WHERE uid = ?
      AND state = ?` (or the peer-ownership/reachability equivalent)
      that re-checks the row is *still* in a purge-eligible state at
      delete time — a uid that reattached, or a peer that reconnected,
      between `candidates_for_purge()` and `purge()` is left untouched
      and reported back as not-actually-removed (return the uids/hosts
      actually deleted, distinct from what was asked for).
- [x] `name_registry` is never read or written by either method.
- [x] Deleting a `peer` row does not cascade-delete its device rows by
      itself (no foreign key) — `purge`'s two arguments are
      independent; a caller passes both the peer host and its device
      uids explicitly (ticket 002 gets both from
      `candidates_for_purge()`'s own grouping).
- [x] Both methods acquire `Store`'s own internal lock
      (`self._lock`) for their whole body, per the class's existing
      "every public method serializes its own use of `self._conn`"
      convention — no new locking primitive.
- [x] `store.py`'s module docstring, the `Store` class docstring, and
      the `peer` table's Decision 5 docstring reference gain a short
      paragraph documenting `rescan`/`purge` as the one explicit,
      operator-invoked exception to "never delete, always update in
      place" — precedent language, not a rule change to any other
      method.
- [x] A device uid or peer host not present in the store is a no-op
      in `purge` (not an error) — mirrors `Store.clear`'s existing
      "deleting something already absent is fine" convention.

## Implementation Plan

**Approach**: Add `candidates_for_purge()` and `purge()` next to
`Store`'s existing peer/device read methods, following the file's
existing dataclass + docstring conventions (see `DeviceRecord`/
`PeerRecord`'s own docstrings for the tone/detail level expected).
`candidates_for_purge()` is a straight read (three `SELECT`s: local
disconnected devices, unreachable-peer-owned devices joined against
`peer.reachable = 0`, reachable-peer-owned disconnected devices joined
against `peer.reachable = 1`) plus a fourth `SELECT` for unreachable
peer rows. `purge()` loops its two argument collections, each a
conditional `DELETE` (`WHERE uid = ? AND state = ?` for local
disconnected and reachable-peer-owned-disconnected; `WHERE uid = ? AND
host = ? AND host IN (SELECT host FROM peer WHERE reachable = 0)` for
unreachable-peer-owned rows — recompute the "unreachable" join inside
the same conditional delete rather than trusting a host list computed
earlier, so a peer that reconnected in the gap is respected the same
way a reattached uid is), checking `cursor.rowcount` to know what
actually deleted, and a conditional `DELETE FROM peer WHERE host = ?
AND reachable = 0`.

**Files to modify**:
- `src/mbtools/registry/store.py` — the two new methods, their
  dataclass return shape(s), and the docstring updates described
  above.

**Testing plan**:
- New unit tests in `tests/registry/store/` (follow that directory's
  existing per-scenario test-file convention) covering: a local
  disconnected row is a purge candidate and is purged;  a
  locally-owned, still-attached row is never a candidate; an
  unreachable peer's device rows (in any state, including
  `attached_unprobed`/`connected`) are candidates and are purged along
  with the peer row itself; a reachable peer's disconnected device row
  is a candidate and is purged; a reachable peer's *non*-disconnected
  device row is never a candidate; `name_registry` rows are untouched
  by either method (assert via `Store.listing()` before/after); the
  mid-reattach race — call `upsert_attached` (or
  `record_peer_seen`/`mark_peer_reachable`) on a uid/host between
  `candidates_for_purge()` and `purge()` and assert it survives and is
  reported as not-removed; purging an absent uid/host is a no-op, not
  an error.
- **Existing tests to run**: `uv run pytest tests/registry/store/`.
- **Verification command**: `uv run pytest tests/registry/store/`.
