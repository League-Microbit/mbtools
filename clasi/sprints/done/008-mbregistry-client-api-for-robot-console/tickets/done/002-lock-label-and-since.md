---
id: '002'
title: Lock label and since
status: done
use-cases:
- SUC-002
- SUC-005
depends-on:
- '001'
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

- [x] `lock` accepts an optional `label` string; omitting it behaves
      exactly as before this ticket.
- [x] A `locked` reply's `holder` includes `label` (when set, else
      absent/`null`) and `since` (always, once acquired).
- [x] `list`'s per-device dict includes `lock_label`/`lock_since`
      (`null` when unlocked), alongside the existing `lock_kind`/
      `lock_pid`.
- [x] A `lock_state` event (via `watch`, ticket 001) carries `label`/
      `since` on acquire and `null`/`null` on release, alongside the
      existing `kind`/`display`.
- [x] `LockManager.release`'s holder-equality check is unaffected —
      `label`/`since` never participate in matching who may release a
      lock.
- [x] A peer's `mbregistry list` shows label/since text (via the
      existing `remote_lock_display` string) for a remote-owned,
      labeled lock, with no new SQLite column and no new PUB field.
- [x] `docs/design/registry-api.md` documents `label`/`since` in the
      `lock`, `list`, and `lock_state` sections.

## Implementation Notes

- `lock_event_payload`/`LockManager`'s `lock_display_callback` were both
  extended to `(uid, kind, display, label, since)` (rather than adding a
  second payload builder, per ticket 001's own handoff note). This means
  `label`/`since` ride as their own keys on *every* `lock_state` event
  this project publishes, including the one sent over the ZeroMQ PUB
  bus to a peer — a receiving peer's own `_apply_event`
  (`registry.peering`) still only ever reads `kind`/`display` back off
  that event into `store.apply_remote_lock_state` (unchanged, 3-arg),
  so no new SQLite column or queryable field results on the replicated
  side; the extra keys on the wire message are inert as far as any
  receiver is concerned. Read Decision 2's "no new PUB field" as being
  about that replicated store shape, not about literally zero extra
  JSON keys on the wire — flagging this reading explicitly in case a
  later ticket's stricter interpretation disagrees.
- Added `mbtools.registry.locks.format_lock_suffix(label, since, *,
  now)`, a small shared pure formatter (exported from `locks.py`, no
  new module) that both `registry.render` (local row) and
  `registry.peering.publish_lock_event` (baking text into the
  replicated `display` string) call — one place owns the "(label,
  age)"/"(age)" wording so the two never drift apart. `render.py`'s
  `render_table`/`_state_cell` gained an optional `now` parameter
  (defaults to `time.time()`) threaded down to this formatter, since
  the module's own contract is "no I/O outside its own arguments."
- `LockManager` gained a `now_fn` constructor parameter (default
  `time.time`), mirroring `store`/`identity`/`usbwatch`'s existing
  injectable-clock convention — used by `acquire` to stamp
  `LockStatus.since` fresh on every call (proven per-acquisition, not
  per-holder, by a new test with a scripted two-value clock).
- Every holder's wire shape (`_holder_wire_dict`) gained `label`/`since`
  unconditionally, local and remote alike — several existing tests
  asserted an *exact* 2-key (or 4-key, for a remote holder) dict and
  needed updating to either check individual fields or the new key set;
  see the diff in `tests/registry/api/test_api.py`,
  `tests/registry/api_windows/test_api_windows.py`,
  `tests/registry/remote_api/test_remote_api.py`,
  `tests/registry/client/test_client.py`, and
  `tests/serial/test_mbserial_connect.py`.
- **For ticket 003 (`unlock --force`)**: `LockManager`'s planned
  `force_release(uid)` will return the `LockStatus` it released, which
  now carries `label`/`since` — worth having `cmd_unlock --force`'s CLI
  output echo them ("released alice-laptop's lock, held for 12m"),
  answering sprint.md's own Open Question in the affirmative if the
  stakeholder wants it; not implemented here, out of this ticket's
  scope.
- **For ticket 004 (local-socket `stream`)**: no interaction expected —
  `_op_lock`/`LockStatus` changes here are orthogonal to the stream
  sub-protocol relocation. One thing to note: a `flash`-kind lock
  acquired with a `label` (e.g. from a future robot-console UI) will
  carry that label through to whatever `mark_flashed`/`flash`
  already read off `LockManager.status` — no extra wiring needed there,
  since both already resolve the full `LockStatus`, not just `kind`.

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
