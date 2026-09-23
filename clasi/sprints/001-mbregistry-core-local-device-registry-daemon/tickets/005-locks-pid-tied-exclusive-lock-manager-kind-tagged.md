---
id: '005'
title: 'Locks: PID-tied exclusive lock manager, kind-tagged'
status: in-progress
use-cases:
- SUC-005
- SUC-006
depends-on:
- '001'
github-issue: ''
issue: mbregistry-device-registry-daemon.md
completes_issue: false
---
<!-- CLASI: Before changing code or making plans, review the SE process in CLAUDE.md -->

# Locks: PID-tied exclusive lock manager, kind-tagged

## Description

Build `mbtools.registry.locks`: the exclusive per-device lock manager.
Per sprint.md's Architecture (module "locks") and Design Rationale
("locks are in-memory only, never persisted to the SQLite store"), this
is a pure, in-memory module — an in-memory table of `uid -> (holder_pid,
kind, ...)` plus a liveness sweep. It does **not** touch a Unix socket
directly: it takes a PID as a parameter and exposes a `release_dead()`
sweep driven by an injectable `is_pid_alive` callable, so it's fully
unit-testable without any real process or socket. The real `SO_PEERCRED`
extraction that turns a socket connection into a PID lives in the API
module (ticket 008), which calls into this module with the PID it
extracted.

Lock kinds: `serial`, `relay`, `flash`, `debug` (brief §3.5/spec §3.6,
cross-cutting §3). A lock is exclusive per device uid — at most one
holder at a time, regardless of kind (the *kind* is metadata for
listings, not a separate lock namespace: a `serial` lock and a `flash`
lock cannot coexist on the same device).

Release happens two ways, and both must be supported (per the
ASSUMPTION in sprint.md: "the lock releases when the PID dies *or* the
connection closes" — treated as separate triggers because a leaked fd
across a fork could keep a connection open after its original process
exits, or vice versa):
1. **Explicit release** — the API calls `locks.release(uid, pid)`
   (checked against the current holder).
2. **Liveness sweep** — a periodic (or on-demand) `locks.sweep()` call
   that checks every held lock's PID via the injected `is_pid_alive`
   and releases any whose holder is gone. `daemon` (ticket 006) or `api`
   (ticket 008) is responsible for calling `sweep()` on an interval or
   on connection-close — this ticket only builds the sweep mechanism
   itself, not its trigger's wiring.

This module is also where `daemon`'s flash-triggered re-probe hook
attaches (per sprint.md's Design Rationale, "a `flash`-kind lock's
release is the re-probe trigger") — expose a way for a caller
(`daemon`) to be notified when a `flash`-kind lock is released (a
callback registered at construction, or a small event/subscription
API — implementer's choice, keep it narrow).

## Acceptance Criteria

- [x] `locks.acquire(uid, kind, pid) -> bool` grants a lock if the
      device is currently unlocked, and fails (returns `False` or
      raises a distinguishable exception — pick one and use it
      consistently) if already locked, without mutating state on
      failure.
- [x] A failed acquire's failure carries the current holder's kind and
      pid, for the API (ticket 008) to build UC-006's "locked for flash
      by pid 4821" message.
- [x] `locks.release(uid, pid)` releases only if `pid` matches the
      current holder; releasing with a non-matching or absent pid is a
      no-op (not an error that could be used to steal a lock).
- [x] `locks.sweep(is_pid_alive: Callable[[int], bool])` releases every
      lock whose holder PID `is_pid_alive` reports as dead, and leaves
      live-holder locks untouched.
- [x] Releasing a `flash`-kind lock (via either `release()` or `sweep()`)
      fires the registered flash-release callback/notification exactly
      once, naming the uid; releasing a non-`flash`-kind lock does not.
- [x] `locks.status(uid) -> LockStatus | None` reports the current
      holder's kind and pid (or `None` if unlocked), for `store`/`api`
      listings to compose "locked by kind+pid" (UC-004's STATE column).
- [x] A device with no lock ever taken behaves identically to one that
      was locked and fully released (idempotent unlocked state) — no
      special-cased "never touched" vs. "released" distinction leaks
      out.
- [x] One integration-style test spawns a real short-lived subprocess,
      acquires a lock with its real PID, kills the subprocess, and
      confirms `sweep()` with the *real* liveness check (e.g.
      `os.kill(pid, 0)`-based) releases it — proving the injectable
      abstraction matches real OS behavior, not just its own fakes.

## Testing

- **Existing tests to run**: ticket 001's fakes tests (no direct
  dependency).
- **New tests to write**: acquire/release happy path; double-acquire
  failure with correct holder info surfaced; release with wrong pid is
  a no-op; sweep with an injected `is_pid_alive` releases only dead
  holders; flash-release callback fires only for `flash`-kind releases;
  the one real-subprocess liveness integration test described above.
- **Verification command**: `uv run pytest tests/registry/locks/`
