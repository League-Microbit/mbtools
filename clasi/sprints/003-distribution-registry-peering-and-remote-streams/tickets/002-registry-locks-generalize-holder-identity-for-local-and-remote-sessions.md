---
id: '002'
title: 'registry.locks: generalize holder identity for local and remote sessions'
status: open
use-cases: [SUC-003, SUC-004]
depends-on: []
github-issue: ''
issue: mbregistry-peering-mdns-and-zeromq.md
completes_issue: true
---
<!-- CLASI: Before changing code or making plans, review the SE process in CLAUDE.md -->

# registry.locks: generalize holder identity for local and remote sessions

## Description

Per sprint.md's Architecture (Decision 2), generalize
`mbtools.registry.locks.LockStatus`/`LockManager` from a bare `pid: int`
holder to a `HolderRef` that can name either a local (PID-tied) or remote
(session-tied) holder — a single lock table stays the sole authority over
a device, whether the acquiring client connected over the Unix socket or
the new remote TCP API (ticket 006+ depends on this). This is flagged in
sprint.md as the sprint's highest-regression-risk ticket and is sequenced
before anything else depends on it.

This ticket touches `locks.py` and its two existing call sites
(`daemon.py`'s `LockManager` construction/usage, `api.py`'s pid-based
acquire/release/sweep calls) to keep local behavior byte-for-byte
identical while making the remote case representable. It does **not**
add a remote caller yet — `registry.remote_api` (ticket 006) is this
new capability's first real consumer.

## Acceptance Criteria

- [ ] New frozen dataclass `HolderRef(origin: str, ref: str, pid: int |
      None = None, host: str | None = None)` in `locks.py`, exported from
      `__all__`. `origin` is `"local"` or `"remote"`.
- [ ] `LockStatus.pid: int` is replaced by `LockStatus.holder: HolderRef`.
      A convenience `LockStatus.pid` property may be kept, returning
      `holder.pid` (may be `None` for a remote holder), so the smallest
      possible set of downstream call sites need updating beyond what
      this ticket touches directly — but the true source of truth is
      `holder`.
- [ ] `LockManager.acquire(uid, kind, holder: HolderRef)`,
      `.release(uid, holder: HolderRef)` (matches on full `HolderRef`
      equality, not just `pid`), and `.sweep(is_alive: Callable[[HolderRef],
      bool])` are updated accordingly. `LockHeldError` carries the
      existing holder's `LockStatus` unchanged in shape (still exposes
      `.kind`/`.pid` for today's callers, plus the new `.holder`).
- [ ] `daemon.py`: the one place that force-releases a detached device's
      lock (`run_once`) and the flash-release-callback wiring are updated
      to read/pass `HolderRef` instead of a bare pid — no behavior
      change, since every local caller still only ever holds local
      (PID-based) locks in this ticket's scope.
- [ ] `api.py`: every `lock`/`unlock`/`flash`/`mark_flashed` op
      constructs `HolderRef(origin="local", ref=str(pid), pid=pid)` from
      the existing `SO_PEERCRED`/`LOCAL_PEERPID`-derived pid, and the
      sweep loop's `is_pid_alive_fn` is adapted into an `is_alive`
      callable that only ever receives local holders in this ticket's
      scope (dispatches on `holder.origin == "local"`, calls the
      existing `is_pid_alive_fn(holder.pid)`; a `"remote"` origin is
      unreachable code in this ticket, since nothing constructs one yet
      — added defensively for ticket 006, not exercised until then).
- [ ] The wire-protocol `holder` dict in a `locked`/`not_locked` response
      (`api.py`'s `_error(..., holder={"kind":..., "pid":...})`) is
      unchanged in shape for a local holder — this is a purely internal
      refactor, not a protocol change, for every existing Unix-socket
      caller.
- [ ] Every existing test in `tests/registry/locks/` and every test in
      `tests/registry/api/`/`tests/registry/daemon/` that touches locking
      passes unchanged (adjusted only for the `LockStatus.pid` ->
      `.holder.pid` internals where a test reaches into that field
      directly, not for any behavior change).

## Testing

- **Existing tests to run**: `tests/registry/locks/`,
  `tests/registry/api/`, `tests/registry/daemon/` — full local-lock
  regression, must pass with no observed behavior change from a local
  client's perspective.
- **New tests to write**:
  - Construct a `HolderRef(origin="remote", ref="session-abc", host="loki")`
    directly against `LockManager` (no real network involved) and prove
    acquire/release/sweep/conflict-reporting all work for it exactly as
    they do for a local holder, proving the generalization is sound
    ahead of ticket 006 actually wiring a real remote caller.
  - A conflict between a local and a remote holder on the same uid is
    refused (`LockHeldError`), proving the single-table exclusivity
    property Decision 2 exists to guarantee.
- **Verification command**: `uv run pytest tests/registry/`
