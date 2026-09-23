---
id: '004'
title: 'Store: SQLite device database and re-probe-eligibility fields'
status: open
use-cases: [SUC-001, SUC-002, SUC-003, SUC-004]
depends-on: ['001']
github-issue: ''
issue: mbregistry-device-registry-daemon.md
completes_issue: false
---
<!-- CLASI: Before changing code or making plans, review the SE process in CLAUDE.md -->

# Store: SQLite device database and re-probe-eligibility fields

## Description

Build `mbtools.registry.store`: the persistent device database. Per
sprint.md's Architecture (module "store", the ERD, and Design Rationale
"SQLite for the store (ASSUMPTION)"), this is a SQLite-backed, single-
table (`device`) store, machine-level (not per-user), living under
`/var/lib/mbregistry/` in production (overridable to a `tmp_path` in
tests — see Design Rationale "file layout (ASSUMPTION)").

One record per device, keyed by `uid`. Fields, per the ERD in sprint.md's
Architecture §4: `uid` (PK), `short_uid`, `port` (nullable), `vid_pid`,
`role`/`common_name`/`device_name`/`serial_payload` (all nullable —
unset until a successful probe), `raw_announcement`, `state`
(`attached_unprobed` | `connected` | `connected_no_firmware` |
`disconnected`), `error_note` (nullable), `flash_count`, `first_seen`,
`last_seen`, `last_probe` (0.0 = never probed).

`store` is deliberately the *only* place that knows the schema and the
*only* place `daemon` (ticket 006) consults to decide "does this device
need probing" — the re-probe rule ("never reopen unless reattached or
flashed") is enforced by `daemon` reading `state`/`last_probe` from
`store`, not by `store` itself refusing writes. `store`'s job is
correct persistence, not policy.

Precedent for "never delete, always update in place" comes from both
predecessor tools: `mbdeploy/devices.py`'s registry-layer docstring
("Entries are never deleted... Prior announcement fields are preserved
when `probe_type` returns None") and `mbrelay/inventory.py`'s module
docstring ("identity is cached to disk, a FREE board already in the
cache is not re-probed"). `store` follows the same convention: a
`disconnected` record is updated, never dropped, and a failed probe
never clobbers an existing announcement field it didn't get fresh data
for.

## Acceptance Criteria

- [ ] SQLite schema created on first use (`CREATE TABLE IF NOT EXISTS`,
      no migration framework needed for a v1 single-table schema); the
      database path is a constructor parameter, defaulting to
      `/var/lib/mbregistry/devices.db` but overridable (tests always
      override to `tmp_path`).
- [ ] `store.upsert_attached(uid, port, vid_pid) -> DeviceRecord` creates
      a new `attached_unprobed` record, or updates `port`/`last_seen` on
      an existing one, without touching announcement fields.
- [ ] `store.apply_probe_result(uid, result: ProbeResult | None)`:
      on a successful probe, updates
      `role`/`common_name`/`device_name`/`serial_payload`/
      `raw_announcement`, sets `state="connected"`, updates
      `last_probe`/`last_seen`; on `None` (no announcement), sets
      `state="connected_no_firmware"` and an `error_note`, but leaves
      any previously-known announcement fields untouched (mirrors
      `mbdeploy`'s "preserve existing announcement fields unchanged"
      rule).
- [ ] `store.mark_disconnected(uid)` sets `state="disconnected"` and
      updates `last_seen`; the record is never deleted from the table.
- [ ] `store.needs_probe(uid) -> bool` reflects the re-probe rule's data
      side: true for a never-probed device (`last_probe == 0.0`), false
      for an already-probed, still-attached, not-flash-marked device.
      (The "was it flashed" trigger itself is ticket 006/007's job —
      this method only answers the store-data half of the rule.)
- [ ] `store.increment_flash_count(uid)` bumps `flash_count` — called by
      ticket 007's flash op on completion.
- [ ] `store.list_devices() -> list[DeviceRecord]` returns every record
      (including `disconnected` ones, per UC-004's "gone, not silently
      dropped" requirement) — iteration is not required to be
      performant beyond "all records fit in memory," per spec §3.4.
- [ ] `store.get(uid)` / `store.find(token)` (matching by uid, short_uid,
      or device_name — same precedence family as `mbdeploy`'s
      `resolve_target`, minus its enum/port-path cases which don't apply
      to the store layer) support the API's list/get/find (ticket 008).
- [ ] All tests run against a real SQLite file in `tmp_path` — no mock
      database layer, since SQLite itself is the thing being tested.

## Testing

- **Existing tests to run**: ticket 001's fakes tests (no direct
  dependency, but confirms no scaffold regression).
- **New tests to write**: create/update/preserve-on-failed-probe
  round-trips; disconnect-then-reattach preserves history (`flash_count`,
  `first_seen`); `needs_probe` true/false cases; `find` by uid,
  short_uid, and device_name; a "never delete" test that disconnects a
  device and confirms `list_devices()` still returns it.
- **Verification command**: `uv run pytest tests/registry/store/`
