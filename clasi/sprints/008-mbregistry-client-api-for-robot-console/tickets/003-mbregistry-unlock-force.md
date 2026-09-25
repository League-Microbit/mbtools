---
id: '003'
title: mbregistry unlock --force
status: done
use-cases:
- SUC-003
- SUC-005
depends-on:
- '002'
github-issue: ''
issue: mbregistry-api-for-robot-console-watch-lock-label-unlock-force-local-stream.md
completes_issue: false
---
<!-- CLASI: Before changing code or making plans, review the SE process in CLAUDE.md -->

# mbregistry unlock --force

## Description

Add `LockManager.force_release(uid) -> LockStatus | None`: a release
path that skips the holder-equality check `release()` enforces, and
returns the `LockStatus` that was released (so a caller can report what
it broke — see label/since from ticket 002). It funnels through the
same shared `_release` mechanics `release()`/`sweep()` already use
(flash-release callback, lock-display callback -> event bus/PUB), so a
forced release is indistinguishable, downstream, from an ordinary one.

Add a per-uid `{uid: connection}` map to `api.py`'s
`RegistryAPIServer`, populated on a successful `lock` and cleared on
release via any path (`unlock`, connection close, sweep, or this
ticket's new force path) — the same connection object whether that
holder later switches it into `stream` mode (ticket 004) or not, so no
separate bookkeeping is needed for a streaming holder.

Add a new `force_unlock` op, dispatched **only** in `api.py` (never
shared into `_api_base.py`, and never added to `remote_api.py` — the
remote TCP port gets no force option, per the design doc's Decisions
and sprint.md's Out of Scope). `force_unlock(uid)`:
1. Calls `LockManager.force_release(uid)`.
2. Looks up `uid`'s mapped connection (if any) and shuts it down from
   the server side, so that connection's blocking read (ordinary
   JSON-lines loop or a `stream` session's frame reader) unblocks with
   an error/EOF and unwinds through its own existing `finally`-block
   cleanup (a harmless no-op release, since the lock is already gone).

Add `mbregistry unlock --force UID|NAME` to `registry.cli` (a new
`cmd_unlock`, a local-socket client following `cmd_list`'s existing
connect-and-send pattern), printing what was released (uid, and
label/since if the ticket 002 fields were set).

Update `docs/design/registry-api.md` for the `force_unlock` op and the
new CLI subcommand.

## Acceptance Criteria

- [x] `LockManager.force_release(uid)` releases a lock regardless of
      who holds it, fires the same flash-release/lock-display callbacks
      as `release()`, and returns `None` (no-op, not an error) if `uid`
      was already unlocked.
- [x] `LockManager.release`'s own holder-equality check is untouched —
      `force_release` is a separate method, not a bypass parameter on
      `release`.
- [x] `{"op": "force_unlock", "uid": "..."}` on the local socket drops
      the lock and closes the holder's connection; the holder's own
      blocked read observes EOF (or a connection error), not a hang.
- [x] `force_unlock` is not dispatched on the remote TCP port
      (`remote_api.py`) — an attempt returns `unknown op` (or is simply
      absent from that server's dispatch table), matching "no
      equivalent on the remote TCP port."
- [x] `mbregistry unlock --force UID|NAME` resolves `UID|NAME` via
      `find`, issues `force_unlock`, and reports success/what was
      released; a `NAME`/`UID` with no active lock reports "not
      locked" rather than erroring.
- [x] A `lock_state` release event (via `watch`, ticket 001) fires for a
      forced release exactly as it would for an ordinary one.
- [x] `docs/design/registry-api.md` documents `force_unlock` and
      `mbregistry unlock --force`.

## Implementation Notes

- `LockManager.force_release(uid)` lives right next to `release`/`sweep`
  in `locks.py`, funnels through the same private `_release` helper
  those two already use, and returns the released `LockStatus` (or
  `None`). `release`'s own holder-equality check is byte-for-byte
  unchanged.
- The per-uid `{uid: connection}` map (`RegistryAPIServer
  ._uid_connections`, api.py only) is kept in sync at every point a
  uid's lock can change hands or be dropped: `_dispatch_line`'s own
  `lock`/`unlock` branches (diffed against `acquired_uids` before/after
  each call, so a successful `lock` records the entry and a successful
  `unlock` drops it), the periodic `_sweep_loop` (drops the entry for
  every uid `LockManager.sweep` actually released), `_op_flash`'s two
  release points, the connection-close `finally` block, and
  `_op_force_unlock` itself. This is what keeps a stale entry from ever
  causing `force_unlock` to shut down an unrelated connection that
  happens to still hold a *different* lock — covered by
  `test_force_unlock_does_not_close_an_unrelated_connection`.
- `_op_force_unlock` shuts the mapped connection down with
  `socket.shutdown(SHUT_RDWR)`, not `close()` — the holder's own
  connection-handler thread still runs its normal read-loop exit and
  `finally`-block cleanup (a no-op `LockManager.release` call, since the
  lock is already gone), exactly as the ticket description asks. Proven
  by `test_force_unlock_closes_the_holders_connection_so_its_read_observes_eof`,
  which asserts the holder's own blocked `readline()` raises rather than
  hangs.
- `force_unlock` is dispatched only in `api.py`'s `_dispatch_line` --
  neither `_api_base.py` nor `remote_api.py` was touched for it (beyond
  the new test proving the remote server has no dispatch entry for it).
- **Handoff from ticket 002 acted on**: `RegistryClient.force_unlock`
  and `cmd_unlock`'s printed output both echo the released lock's
  `label`/`since` (via `locks.format_lock_suffix`, the same helper
  `render.py`/`peering.py` already use), e.g. `released flash lock
  (alice-laptop, 12m)` -- cheap given `_holder_wire_dict` already carried
  both fields since ticket 002.
- **For ticket 004 (local-socket `stream`)**: `_uid_connections` stores
  the same raw `conn` object `_handle_connection` accepts, before any
  mode switch -- a holder that later moves this connection into `stream`
  mode needs no separate bookkeeping; `force_unlock`'s
  `shutdown(SHUT_RDWR)` will unblock a `stream` session's frame reader
  exactly the way it unblocks the ordinary JSON-lines loop today. Worth
  double-checking once ticket 004 lands that the stream frame reader's
  own error handling treats a mid-frame shutdown as a clean teardown
  (release the lock, stop the pump thread), not a crash -- not verified
  here since `stream` doesn't exist on the local socket yet.

## Testing

- **Existing tests to run**: `tests/registry/locks/`,
  `tests/registry/api/`, `tests/registry/cli/`.
- **New tests to write**: `LockManager.force_release` unit tests
  (releases regardless of holder, no-op on unlocked, callbacks fire);
  an `api.py` integration test asserting the held connection observes
  EOF after another connection's `force_unlock`; a CLI test for
  `mbregistry unlock --force` against both a held and an already-free
  lock; a test confirming `remote_api.py` has no `force_unlock` op.
- **Verification command**: `uv run pytest tests/registry/`
