---
id: 008
title: 'Query/control API: Unix socket protocol, SO_PEERCRED wiring'
status: done
use-cases:
- SUC-004
- SUC-005
- SUC-006
depends-on:
- '004'
- '005'
- '006'
- '007'
github-issue: ''
issue: mbregistry-device-registry-daemon.md
completes_issue: false
---
<!-- CLASI: Before changing code or making plans, review the SE process in CLAUDE.md -->

# Query/control API: Unix socket protocol, SO_PEERCRED wiring

## Description

Build `mbtools.registry.api`: the local Unix-socket protocol server that
exposes `store`, `locks`, `daemon`, and `flash` to local clients — per
spec §3.5, "other programs consult it through a service," which is why
the CLI (ticket 009) is a client of this module rather than reading
`store` directly.

**Wire protocol**: choose a concrete shape (e.g. newline-delimited JSON
requests/responses over the socket) and document it in this ticket's
implementation notes or a short protocol doc — per sprint.md's Open
Questions #1, this becomes a de facto contract sprint 002's client tools
(`mbdeploy`, `mbserial`) will need to speak, so it must be written down
somewhere a later sprint can find, not left only in code. A minimal
request/response shape (`{"op": "list"}` → `{"devices": [...]}`, etc.)
is sufficient for sprint 001 — no need to over-design a general RPC
framework.

**Operations** (spec §3.5, §3.6, §3.8 minimum set): `list` (all devices,
per UC-004), `get`/`find` (by uid/short_uid/name), `lock` (uid, kind),
`unlock` (uid), `flash` (uid, hex bytes or path, streamed log back over
the same connection).

**`SO_PEERCRED` wiring** (per sprint.md's Architecture ASSUMPTION #3):
on each accepted connection, read the peer's credentials via
`socket.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, ...)` (Linux-
specific) to get the connecting process's PID, and use that PID for
every `lock`/`unlock` operation on that connection — never a
client-supplied PID (spoofable, per the Design Rationale's stated
alternative-rejected reasoning). When a connection closes, treat it as
an implicit unlock trigger for any lock that connection's PID holds
(the "or the connection closes" half of the ASSUMPTION) — call into
`locks` accordingly (`locks.release` for lock(s) matching that PID, not
a blanket sweep, since other connections' locks must be untouched).

**Registry-unavailable error** (UC-004's error flow): this ticket only
builds the server side; the "socket not present" client-side error
message belongs to ticket 009 (CLI), but the *stable exit codes* this
error flow needs should be defined here (or in `mbtools.common`) so both
the CLI and future sprint-002 clients use the same set rather than each
inventing their own — reference `microbit-radio-relay/server/src/
mbrelay/errors.py`'s `EXIT_OK=0, EXIT_ERROR=1, EXIT_USAGE=2,
EXIT_NO_DAEMON=3, EXIT_NO_DEVICE=4, EXIT_NO_FREE_DEVICE=5,
EXIT_HARDWARE=6` as the precedent to follow (adapt names/values to
mbtools's needs, but keep the "stable, documented, small integer per
failure category" shape).

## Acceptance Criteria

- [x] The server listens on a Unix socket at a configurable path
      (defaulting to `/run/mbregistry/api.sock` in production, a
      `tmp_path` in tests).
- [x] `list` returns every device from `store.list_devices()`,
      including `disconnected` ones (UC-004's "gone, not silently
      dropped"), with each device's lock status (kind + pid, from
      `locks.status`) folded in for the STATE column ticket 009 needs.
- [x] `get`/`find` supports lookup by uid, short_uid, and device_name
      (delegating to `store.find`), returning a distinct "no such
      device" error from "locked" (UC-006's error-flow requirement).
- [x] `lock(uid, kind)` extracts the caller's PID via `SO_PEERCRED` on
      that connection and calls `locks.acquire`; on failure, the
      response names the current holder's kind and pid (UC-006:
      "locked for flash by pid 4821").
- [x] `unlock(uid)` calls `locks.release` with the connection's own
      `SO_PEERCRED` PID (a client cannot unlock another connection's
      lock by naming a different uid/pid).
- [x] When a connection closes (cleanly or abruptly), any lock(s) held
      by that connection's PID are released — verified by a test that
      opens a connection, locks a device, closes the connection without
      an explicit `unlock`, and confirms the device is lockable again.
- [x] `flash(uid, hex_bytes)` requires a `flash`-kind lock already held
      by the calling connection's PID (consistent with ticket 007's own
      lock-check), streams `flash.flash_hex`'s log callback back over
      the connection as it happens, and returns a final success/failure
      result.
- [x] Exit codes (or their protocol-level equivalent — an error `code`
      field on responses) are defined in one place and documented in
      this ticket's implementation notes for sprint 002 to reuse.
- [x] Protocol/dispatch tests run against a real Unix socket in
      `tmp_path`; the `SO_PEERCRED`-specific assertion is Linux-only and
      is `pytest.mark.skipif` (not `xfail`) on macOS, with the rest of
      the protocol (dispatch, close-triggers-release using an injected
      PID) still exercised there.

## Implementation Notes

- **Module**: `mbtools.registry.api.RegistryAPIServer`. Full wire
  protocol (ops, response shapes, error/exit codes, SO_PEERCRED/
  LOCAL_PEERPID mechanics, threading model and known limitations) is
  written up in `docs/design/registry-api.md` — the de facto contract
  sprint 002's `mbdeploy`/`mbserial` will speak, per Open Questions #1.
- **`flash` takes `hex_path`, not raw bytes.** The acceptance criterion
  above says `flash(uid, hex_bytes)`; the concrete request field is
  `hex_path` (a filesystem path the daemon reads directly), matching
  `flash.FlashOp.flash_hex(uid, hex_path, log)`'s own signature (ticket
  007) and avoiding pointless bytes-over-a-local-socket transfer when
  client and daemon share a filesystem. Documented in
  `docs/design/registry-api.md`.
- **Macos peer-PID**: `SO_PEERCRED` is Linux-only, so
  `default_peer_pid` dispatches to `LOCAL_PEERPID` (`SOL_LOCAL=0`,
  `LOCAL_PEERPID=0x002`, confirmed against the macOS SDK headers and a
  real `AF_UNIX` socket pair) on macOS — braeburn, one of this
  project's own hardware-acceptance hosts, is a Mac, so "fall back
  sensibly" means a real, working mechanism, not merely skipping.
  `peer_pid_fn` is injectable so only the one Linux-specific assertion
  (`SO_PEERCRED`'s exact struct) is platform-skipped; the rest of the
  protocol suite runs identically on both platforms via an injected pid.
- **Periodic sweep wiring**: `LockManager.sweep` was not wired anywhere
  before this ticket (`daemon` only reads `locks.status`) — this module
  wires it on both triggers ASSUMPTION #3 calls out: per-connection
  close (explicit `locks.release` for that connection's own acquired
  uids, never a blanket sweep) and a periodic background timer
  (`sweep_interval_s`, default 5s) using a real `os.kill(pid, 0)`-based
  `is_pid_alive_fn`.
- **`store.py` change**: `Store`'s sqlite3 connection now opens with
  `check_same_thread=False` — the API is the first thing that calls
  into a `Store` instance from a thread other than the one that
  constructed it (each accepted connection gets its own handler
  thread). No other locking was added inside `store.py`, consistent
  with the architecture's already-accepted "no design here for
  multi-writer contention beyond SQLite's own file locking (WAL mode)".
  See `docs/design/registry-api.md`'s "Known limitations" section for
  the cross-module concurrency note this surfaces for ticket 009.
- **`pyocd` added to `pyproject.toml` dependencies** (`>=0.44.1`,
  matching `mbdeploy`'s own pin) — `flash.py` (ticket 007) already
  shells out to it via `[sys.executable, "-m", "pyocd"]`, but it was
  never a declared dependency, so installing `mbtools` alone did not
  give a working flash path.

## Testing

- **Existing tests to run**: `uv run pytest tests/registry/store/
  tests/registry/locks/ tests/registry/daemon/ tests/registry/flash/`
  — confirm the four modules this ticket dispatches to still pass.
- **New tests to write**: list/get/find happy paths and not-found
  cases; lock/unlock happy path and already-locked failure with correct
  holder info; connection-close releases that connection's locks; flash
  end-to-end against `flash.flash_hex` with an injected pyOCD runner,
  confirming streamed log lines arrive over the socket in order; the
  Linux-only `SO_PEERCRED` correctness test.
- **Verification command**: `uv run pytest tests/registry/api/`
