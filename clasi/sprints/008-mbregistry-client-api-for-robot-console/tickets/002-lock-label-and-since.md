---
id: '002'
title: Lock label and since
status: open
use-cases: [SUC-002, SUC-005]
depends-on: ['001']
github-issue: ''
issue: mbregistry-api-for-robot-console-watch-lock-label-unlock-force-local-stream.md
completes_issue: false
---
<!-- CLASI: Before changing code or making plans, review the SE process in CLAUDE.md -->

# Lock label and since

## Description

Add an optional, display-only `label` on `lock` and a `since`
acquisition timestamp, both surfaced in `locked.holder`, `list`, and the
`lock_state` event carried over the event bus (ticket 001) and, when
peering is on, over the PUB bus.

Per sprint.md's Design Rationale (Decision 1), both fields live on
`LockStatus`, **not** `HolderRef`: `HolderRef` is frozen and
equality-matched by `LockManager.release` to decide who may release a
lock, and is constructed once per connection and reused across every
lock that connection acquires — `label`/`since` are per-*acquisition*,
so they belong on the record `LockManager.acquire` creates fresh each
time (`LockStatus`), not on the reused holder identity. `HolderRef`
itself is unchanged.

- `LockManager.acquire(uid, kind, holder, *, label=None)` sets
  `LockStatus.label` from the caller-supplied value and
  `LockStatus.since` from an injected `now_fn` (mirroring `store`/
  `identity`/`usbwatch`'s existing `now_fn` convention), defaulting to
  `time.time`.
- `_api_base._op_lock` reads an optional `req["label"]` and passes it
  through.
- `_holder_wire_dict` (the `locked.holder` shape) and `_device_dict`
  (the `list` `lock_kind`/`lock_pid` fields) fold in `label`/`since`
  from the resolved `LockStatus` — both `null`/absent-safe for an
  unlocked device or a lock acquired with no label.
- `peering.py`'s `publish_lock_event`'s `display` string gains
  label/since text inline when present (e.g. append `" (alice-laptop,
  12m)"`) — per Design Rationale Decision 2, this is *not* a new
  replicated field; a peer's cached `remote_lock_display` cell picks it
  up automatically with zero schema/wire growth, since showing a
  *peer's* label/since is not a stated acceptance criterion for this
  sprint (robot-console always locks/streams against the owning host
  directly).
- `render.py`'s local-row "locked by `<kind>` pid `<n>`" cell appends
  the label/since text when present.

Update `docs/design/registry-api.md`'s `lock`/`list`/`lock_state`
sections for the new fields.

## Acceptance Criteria

- [ ] `lock` accepts an optional `label` string; omitting it behaves
      exactly as before this ticket.
- [ ] A `locked` reply's `holder` includes `label` (when set, else
      absent/`null`) and `since` (always, once acquired).
- [ ] `list`'s per-device dict includes `lock_label`/`lock_since`
      (`null` when unlocked), alongside the existing `lock_kind`/
      `lock_pid`.
- [ ] A `lock_state` event (via `watch`, ticket 001) carries `label`/
      `since` on acquire and `null`/`null` on release, alongside the
      existing `kind`/`display`.
- [ ] `LockManager.release`'s holder-equality check is unaffected —
      `label`/`since` never participate in matching who may release a
      lock.
- [ ] A peer's `mbregistry list` shows label/since text (via the
      existing `remote_lock_display` string) for a remote-owned,
      labeled lock, with no new SQLite column and no new PUB field.
- [ ] `docs/design/registry-api.md` documents `label`/`since` in the
      `lock`, `list`, and `lock_state` sections.

## Testing

- **Existing tests to run**: `tests/registry/locks/`,
  `tests/registry/api/`, `tests/registry/remote_api/`,
  `tests/registry/render/`, `tests/registry/peering/`.
- **New tests to write**: a `label`/`since` round-trip through
  `lock` -> `list` -> `locked`; a `LockManager.acquire`/`_release` test
  with an injected `now_fn` proving `since` is per-acquisition, not
  per-connection; a `lock_state` watch-event test (building on ticket
  001's harness) asserting label/since on acquire and their absence on
  release; a `render.py` test for the label/since-annotated lock cell,
  local and peer-owned.
- **Verification command**: `uv run pytest tests/registry/`
