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
| `not_locked` | `flash` was requested without this connection already holding a `flash`-kind lock on that device |
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
  into `store`/`locks`/`flash_op`. `flash` holds that lock for its *entire*
  duration (including the streamed pyocd run) — at this sprint's device-count
  scale ("not a lot of devices", per the brief) a `list`/`get` from another
  connection blocking for the duration of one flash is an accepted,
  documented trade-off, not an oversight. Revisit only if that stops being
  true.
- **`Store`'s sqlite3 connection** is opened with `check_same_thread=False`
  (this ticket's one change to `store.py`) because the API introduces the
  first cross-thread callers of a `Store` instance — the daemon's own poll
  loop and this module's connection-handler threads. No additional
  app-level locking was added inside `store.py` itself, consistent with the
  architecture's already-accepted "no design here for multi-writer
  contention beyond SQLite's own file locking (WAL mode)".
- **Cross-module concurrency** between `daemon.run_once()`'s own thread and
  the API's connection-handler threads touching the same `LockManager`
  instance is not separately locked by this ticket (`LockManager`'s
  individual methods are short, GIL-atomic-in-practice dict operations, but
  a compound check-then-act sequence spanning *both* `daemon` and `api`
  simultaneously is not guarded by anything this ticket added). Worth
  flagging for ticket 009's assembly (`mbregistry run` is what actually
  starts running `Daemon.run()` and `RegistryAPIServer.serve_forever()`
  concurrently) rather than silently assumed safe.
