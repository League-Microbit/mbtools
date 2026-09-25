---
id: '003'
title: mbregistry unlock --force
status: open
use-cases: [SUC-003, SUC-005]
depends-on: ['002']
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

- [ ] `LockManager.force_release(uid)` releases a lock regardless of
      who holds it, fires the same flash-release/lock-display callbacks
      as `release()`, and returns `None` (no-op, not an error) if `uid`
      was already unlocked.
- [ ] `LockManager.release`'s own holder-equality check is untouched —
      `force_release` is a separate method, not a bypass parameter on
      `release`.
- [ ] `{"op": "force_unlock", "uid": "..."}` on the local socket drops
      the lock and closes the holder's connection; the holder's own
      blocked read observes EOF (or a connection error), not a hang.
- [ ] `force_unlock` is not dispatched on the remote TCP port
      (`remote_api.py`) — an attempt returns `unknown op` (or is simply
      absent from that server's dispatch table), matching "no
      equivalent on the remote TCP port."
- [ ] `mbregistry unlock --force UID|NAME` resolves `UID|NAME` via
      `find`, issues `force_unlock`, and reports success/what was
      released; a `NAME`/`UID` with no active lock reports "not
      locked" rather than erroring.
- [ ] A `lock_state` release event (via `watch`, ticket 001) fires for a
      forced release exactly as it would for an ordinary one.
- [ ] `docs/design/registry-api.md` documents `force_unlock` and
      `mbregistry unlock --force`.

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
