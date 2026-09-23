# `mbregistry` API — wire protocol

Implemented by `mbtools.registry.api.RegistryAPIServer` (sprint 001, ticket 008).
Written down here — not only in code — per sprint.md's Open Question #1: this
becomes a de facto contract sprint 002's client tools (`mbdeploy`, `mbserial`)
must speak.

## Transport

A Unix domain socket (`AF_UNIX`, `SOCK_STREAM`), default path
`/run/mbregistry/api.sock` (`mbtools.registry.api.DEFAULT_SOCKET_PATH`; tests
override it to a short `tmp_path`-style directory — `AF_UNIX` socket paths are
limited to ~104-108 bytes, shorter than pytest's own default `tmp_path`
nesting, so tests use their own short temp dir).

One socket connection is one client session. A client may send any number of
requests over one connection before closing it. Closing the connection (or
the process dying without closing it) releases every lock that connection's
peer PID holds — see "Locking and connection lifetime" below.

## Framing

Newline-delimited JSON, one JSON object per line, UTF-8. Every non-streaming
request gets exactly one JSON response line. `flash` is the one exception —
see below.

## Requests

Every request is a JSON object with an `"op"` field:

| op | fields | description |
|---|---|---|
| `list` | — | every device, including `disconnected` ones |
| `get` / `find` | `uid` | resolve a device by uid, short_uid, or device_name (`store.find`'s precedence) — `get` and `find` are aliases for the same op |
| `lock` | `uid`, `kind` | acquire an exclusive lock of `kind` (`serial`/`relay`/`flash`/`debug`) on the device, tied to this connection's peer PID |
| `unlock` | `uid` | release this connection's own lock on the device (a no-op, not an error, if this connection doesn't hold it) |
| `flash` | `uid`, `hex_path` | flash `hex_path` to the device — requires a `flash`-kind lock already held by this same connection (call `lock` first) |
| `mark_flashed` | `uid` | bookkeeping only: record that `uid` was flashed *outside* this op (sprint 002's `mbdeploy`, which flashes locally by running pyocd directly rather than through `flash`) — same `flash`-kind-lock-held-by-this-connection precondition as `flash`, no pyocd invocation |

`uid` accepts any of uid / short_uid / device_name for every op that takes
one, since all of them resolve through `store.find`.

## Responses

Every non-streaming response is one of:

```jsonc
{"ok": true, ...op-specific fields...}
{"ok": false, "code": "<CODE>", "error": "<human-readable message>", ...extra...}
```

`code` is one of the stable constants in `mbtools.common`:

| code | meaning |
|---|---|
| `not_found` | no device matches the given uid/short_uid/device_name |
| `locked` | `lock` failed — device already held by someone else. The response also carries `"holder": {"kind": ..., "pid": ...}` |
| `not_locked` | `flash` or `mark_flashed` was requested without this connection already holding a `flash`-kind lock on that device |
| `invalid_request` | malformed JSON, missing/bad fields, or an unknown `op` |
| `internal_error` | reserved for an unexpected server-side failure (not raised by normal dispatch paths as of this ticket) |

### `list`

```jsonc
// response
{"ok": true, "devices": [
  {"uid": "...", "short_uid": "...", "port": "...", "vid_pid": "...",
   "role": "...", "common_name": "...", "device_name": "...",
   "serial_payload": "...", "raw_announcement": "...",
   "state": "attached_unprobed|connected|connected_no_firmware|disconnected",
   "error_note": "...", "flash_count": 0,
   "first_seen": 0.0, "last_seen": 0.0, "last_probe": 0.0,
   "lock_kind": "serial|relay|flash|debug|null", "lock_pid": 1234}
]}
```

Every field on the device dict is `store.DeviceRecord`'s own columns
(`dataclasses.asdict`), plus two folded-in lock-status fields
(`lock_kind`/`lock_pid`, both `null` when unlocked, from `locks.status`) so a
client building a STATE column (ticket 009) never needs a second round-trip
per device.

### `get` / `find`

```jsonc
// request
{"op": "find", "uid": "<uid, short_uid, or device_name>"}
// response
{"ok": true, "device": {...same shape as one list entry...}}
// or
{"ok": false, "code": "not_found", "error": "no such device: '...'"}
```

### `lock`

```jsonc
// request
{"op": "lock", "uid": "...", "kind": "serial"}
// response
{"ok": true}
// or, already locked by someone else:
{"ok": false, "code": "locked", "error": "...",
 "holder": {"kind": "flash", "pid": 4821}}
// or, bad kind / missing fields:
{"ok": false, "code": "invalid_request", "error": "..."}
// or:
{"ok": false, "code": "not_found", "error": "..."}
```

The PID recorded as the holder is **never** taken from the request — it is
read from the connection's own kernel-verified peer credentials (see below),
so a client cannot lock, or unlock, on another process's behalf.

### `unlock`

```jsonc
// request
{"op": "unlock", "uid": "..."}
// response
{"ok": true, "released": true}   // or false if this connection didn't hold it -- still ok:true, not an error
```

### `flash`

```jsonc
// request
{"op": "flash", "uid": "...", "hex_path": "/path/to/firmware.hex"}
// zero or more, streamed as pyocd produces them:
{"type": "log", "line": "erasing..."}
{"type": "log", "line": "programming..."}
// exactly one, terminal:
{"type": "result", "ok": true, "success": true, "exit_code": 0, "error": null}
```

Every response to a `flash` request — including a precondition failure that
never streamed a single log line (`not_locked`, `not_found`,
`invalid_request`) — carries `"type": "result"` as its last (only, in the
failure case) line. A client's dispatch loop for this one op is always: read
lines; a `"type": "log"` line is progress output; any other line is the
final outcome, stop reading.

`flash` requires the calling connection to already hold a `flash`-kind lock
on the device (call `lock` first) — consistent with
`mbtools.registry.flash.FlashOp.flash_hex`'s own contract, checked again at
this layer because the API additionally requires it be *this connection's*
own PID, not merely *some* holder's. Because `flash_hex` itself never
releases the lock (its own module contract — the caller decides when), the
API releases it here, unconditionally, once the attempt concludes (pyocd
success, pyocd failure, or a hex-validation error) — this release is exactly
what `mbtools.registry.daemon.Daemon`'s flash-triggered re-probe hook is
watching for, so a client never needs a separate `unlock` call after `flash`.

### `mark_flashed`

```jsonc
// request
{"op": "mark_flashed", "uid": "..."}
// response
{"ok": true}
// or, no flash-kind lock held by this connection:
{"ok": false, "code": "not_locked", "error": "..."}
// or:
{"ok": false, "code": "not_found", "error": "..."}
```

Bookkeeping-only op (ticket 003) for a flash that ran *outside* this server's
own `flash` op — sprint 002's `mbdeploy` flashes locally by running pyocd
directly (ticket 007) rather than through `flash`, per sprint.md's Design
Rationale ("a new `mark_flashed` wire-protocol op, rather than reusing
`unlock` or extending `flash`"). Without this op, nothing calls
`store.increment_flash_count` for that path.

Same precondition as `flash`: a `flash`-kind lock already held by *this
connection's own* PID (checked via `LockManager.status`, same check
`_op_flash` makes) — a lock held by a different connection, or no lock at
all, is `not_locked`, same as `flash`. On success, `store.flash_count` for
`uid` is incremented by exactly one. Unlike `flash`, this op never invokes
pyocd and — because it does not touch the lock at all — never releases it,
so it triggers no re-probe of its own; the existing lock-release hook
(`LockManager`'s `flash_release_callback`, fired on any `flash`-kind release
regardless of what ran before it) is what does that, unaffected by this op.

## Locking and connection lifetime

Per sprint.md's Architecture ASSUMPTION #3 ("the lock releases when the PID
dies *or* the connection closes" — two separate triggers):

- **Connection close** (clean or abrupt): the API releases exactly the locks
  that connection itself acquired (tracked per-connection), by calling
  `locks.release(uid, pid)` for each — never a blanket sweep, so another
  connection's locks are untouched.
- **Periodic liveness sweep**: independently of any one connection, a
  background timer (`sweep_interval_s`, default 5s) calls
  `LockManager.sweep()` against a real `os.kill(pid, 0)`-based liveness
  check. This catches a holder process that died without its socket
  connection observably closing (e.g. a duplicated fd surviving a fork).
  Nothing in sprint 001 wired `LockManager.sweep` before this ticket —
  `daemon` (ticket 006) does not call it.

## Peer-PID identification (`SO_PEERCRED` / `LOCAL_PEERPID`)

The connecting process's PID is read from the kernel on each accepted
connection, never taken from the client:

- **Linux**: `SO_PEERCRED` (`socket.SOL_SOCKET`, `socket.SO_PEERCRED`) —
  `struct ucred { pid_t pid; uid_t uid; gid_t gid; }`.
- **macOS**: `LOCAL_PEERPID` (`SOL_LOCAL` = 0, `LOCAL_PEERPID` = 0x002, from
  `<sys/un.h>`) — not exposed as constants by Python's `socket` module, so
  `mbtools.registry.api` hardcodes the two stable integer values. Confirmed
  against the macOS SDK headers and tested against a real `AF_UNIX` socket
  pair during this ticket's implementation. macOS support matters here
  because braeburn, one of this project's own hardware-acceptance hosts
  (`CLAUDE.md`), is a Mac.
- Any other platform: `default_peer_pid` raises `OSError` rather than
  falling back to a client-supplied (spoofable) PID.

`RegistryAPIServer(peer_pid_fn=...)` is injectable so tests don't depend on
real OS behavior for anything except the one test that specifically asserts
the real mechanism (`pytest.mark.skipif` gated to the right platform, per
sprint.md's Test Strategy — not `xfail`, so it fails loudly if the platform
check is ever wrong).

## Store schema additions for peering (sprint 003, ticket 001)

`mbtools.registry.store.Store` gained the data model
`registry.remote_api` (ticket 006+) will need to describe in its own
`list`/`find` responses — see sprint.md's ERD. Written down here rather
than only in `store.py` so this document stays the one place that
describes what a device dict *can* contain, per this file's own reason
for existing (Open Question #1).

- **`device.host`** (`TEXT`, nullable): `NULL` means this row is a
  locally-owned device (attached to a port on this registry); otherwise
  it is the owning peer's hostname, and the row was written by
  `registry.peering` applying a remote snapshot or event.
- **`device.remote_lock_kind` / `device.remote_lock_display`** (`TEXT`,
  nullable): a display-only cache of a remote-owned row's lock state,
  replicated over the event bus (Decision 3) — never consulted for a
  local row, which always reads live `LockManager` state instead. Set
  only by `Store.apply_remote_lock_state`, never by anything that
  touches `LockManager`.
- **`peer` table** (new): one row per discovered registry —
  `host` (`TEXT PRIMARY KEY`), `endpoint` (`TEXT`, `host:port` of that
  peer's own `remote_api` listener), `last_seen` (`REAL`), `reachable`
  (`INTEGER`, boolean). A vanished peer is marked `reachable = 0`, never
  deleted (Decision 5) — `registry.render` (ticket 010) is expected to
  render that peer's devices as "peer unreachable" rather than their
  last-known live state.
- **`Store.find(token)`** now accepts an optional `name@host` suffix
  (e.g. `"zavaz@loki"`, case-insensitive on both parts) to resolve a
  device name that collides across hosts. Without a suffix, a
  `device_name` match that is ambiguous across more than one `host`
  value (the local `NULL` host counts as one candidate) raises
  `mbtools.registry.store.AmbiguousNameError` instead of silently
  picking one — a future `remote_api`/local-API error path will need a
  `CODE_*` for this (not assigned by this ticket; `registry.remote_api`,
  ticket 006+, is this module's first real caller of the ambiguity
  path).

An updated device dict (once `registry.remote_api`/`render` surface it)
is expected to add `host`/`remote_lock_kind`/`remote_lock_display` to
the shape shown above, plus a `reachable` flag derived from the owning
`peer` row for a remote-owned device — none of that wiring exists yet as
of this ticket, which only touches `store.py`.

## Exit codes

`mbtools.common` defines the stable process exit codes ticket 009's CLI (and
sprint 002+'s client tools) should use, so every client shares one set
instead of inventing its own — ported from
`microbit-radio-relay/server/src/mbrelay/errors.py`'s precedent shape
("stable, documented, small integer per failure category", never renumbered):

| constant | value | meaning |
|---|---|---|
| `EXIT_OK` | 0 | success |
| `EXIT_ERROR` | 1 | generic failure |
| `EXIT_USAGE` | 2 | bad CLI invocation |
| `EXIT_NO_DAEMON` | 3 | the api socket is not present/unreachable (UC-004's error flow) |
| `EXIT_NO_DEVICE` | 4 | `code: "not_found"` |
| `EXIT_LOCKED` | 5 | `code: "locked"` |
| `EXIT_HARDWARE` | 6 | a flash op ran and failed |

`mbtools.common` also carries the protocol-level `CODE_*` string constants
listed above, for the same one-place reason.

## Known limitations / forward notes for ticket 009 and sprint 002

- **Threading model.** One OS thread per accepted connection, plus one
  sweep thread, serialized by a single `threading.RLock` around every call
  into `store`/`locks`/`flash_op` — **except** the `flash` op's own pyocd
  run, resolved below.
- **`Store`'s sqlite3 connection** is opened with `check_same_thread=False`
  (ticket 008's one change to `store.py`) because the API introduces the
  first cross-thread callers of a `Store` instance — the daemon's own poll
  loop and this module's connection-handler threads. No additional
  app-level locking was added inside `store.py` itself, consistent with the
  architecture's already-accepted "no design here for multi-writer
  contention beyond SQLite's own file locking (WAL mode)".
- **Cross-module concurrency between `daemon` and `api` — resolved in
  ticket 009.** The gap flagged here through ticket 008 (`daemon.run_once()`'s
  own thread and the API's connection-handler threads touching the same
  `Store`/`LockManager` instances with nothing serializing *across* the two
  modules) is closed by `mbregistry run`'s assembly: it constructs one
  `threading.RLock` and passes it to both `Daemon(lock=...)` and
  `RegistryAPIServer(lock=...)`, so every store/locks access from either
  side goes through the same lock. `Daemon` holds it only around its
  in-memory/single-sqlite-statement bookkeeping (the attach/detach diff and
  the pre/post-probe store writes), never around the real port I/O in
  `identity.probe()` — see `daemon.py`'s own "Concurrency" docstring note.
- **`flash` no longer holds the shared lock for the whole pyocd run —
  resolved in ticket 009.** Holding it there was an acceptable sprint-1
  trade-off when the lock was scoped to `api` alone; it stopped being
  acceptable once the lock became shared with `daemon`'s own scan loop,
  since it would then also block the daemon from ever completing a cycle
  for the duration of any one flash. `_op_flash` now holds the shared lock
  only for the short bookkeeping before and after the run (resolving the
  record and confirming this connection's flash-kind lock is held; then,
  afterwards, releasing that lock) — the run itself happens with the shared
  lock released. The per-device `flash`-kind lock, already held as a
  verified precondition and not released until the attempt concludes, is
  what continues to protect the device for the whole run; the daemon's own
  "never probe a locked device" check (`LockManager.status`) still sees it
  as locked throughout, so this does not reopen the race the flash-kind
  lock exists to prevent.
