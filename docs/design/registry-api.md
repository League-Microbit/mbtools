# `mbregistry` API — wire protocol

Implemented by `mbtools.registry.api.RegistryAPIServer` (sprint 001, ticket 008)
over a local Unix socket, and, since sprint 003 ticket 006, by
`mbtools.registry.remote_api.RemoteAPIServer` over TCP for the same
`list`/`find`/`lock`/`unlock`/`mark_flashed` ops (see "Remote TCP control
plane" below) — the two share one dispatch implementation
(`mbtools.registry._api_base.BaseAPIServer`) — plus, TCP-only, a `stream`
op (sprint 003, ticket 007) that switches the connection into a separate
binary framed sub-protocol (`mbtools.registry.stream_frame`) for serial
data and out-of-band control. Written down here — not only in code — per
sprint.md's Open Question #1: this becomes a de facto contract sprint
002's client tools (`mbdeploy`, `mbserial`) must speak. Except where
noted, everything below describes the local Unix socket; "Remote TCP
control plane" covers only where the TCP transport differs.

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
| `not_found` | no device matches the given uid/short_uid/device_name (or, on the remote TCP API, the device exists in this registry's cache but is peer-owned — see "Remote TCP control plane" below) |
| `locked` | `lock` failed — device already held by someone else. The response also carries a `"holder"` object — see "Lock holder wire shape" below |
| `not_locked` | `flash` or `mark_flashed` was requested without this connection already holding a `flash`-kind lock on that device; or, sprint 003 ticket 007, remote TCP API only, `stream` was requested without this connection already holding a `serial`-kind lock on that device |
| `invalid_request` | malformed JSON, missing/bad fields, or an unknown `op` |
| `internal_error` | reserved for an unexpected server-side failure (not raised by normal dispatch paths as of this ticket) |
| `ambiguous_name` | sprint 003, ticket 006: a bare device-name token (no `@host` suffix) matches devices on more than one host — `store.find`'s `AmbiguousNameError` (ticket 001), translated here since `list`/`find`/`lock`/`unlock`/`mark_flashed` (both the local Unix API and the remote TCP API — they share one dispatch implementation, `registry._api_base`) are `store.find`'s first real API-layer callers. The response also carries `"hosts": [...]` (`null` for the local/`NULL` host), for a client to build a `name@host` suggestion from. |
| `unauthorized` | sprint 003, ticket 006, remote TCP API only: `--auth-token`/`$MBREGISTRY_TOKEN` is configured and this connection's first message didn't carry a matching token |

### Lock holder wire shape

```jsonc
// a local holder (unchanged since sprint 001):
"holder": {"kind": "flash", "pid": 4821}
// a remote (session-tied) holder (sprint 003, ticket 006) -- an additive
// superset, never a breaking change to the local shape above:
"holder": {"kind": "flash", "pid": null, "origin": "remote", "host": "loki"}
```

Since sprint 003 (ticket 002), a device's lock can be held by either a
local (PID-tied) or a remote (session-tied) client — the local Unix API
and the remote TCP API share one `LockManager` table (Decision 2), so a
`locked` response on *either* API can name a holder of *either* origin.
A local holder's wire shape is exactly the 2-key form shown above,
unchanged since before this sprint. A remote holder adds `origin`/`host`
and reports `pid: null` ("a PID means nothing across hosts") — a client
that only ever checked `holder.kind`/`holder.pid` keeps working
unmodified; only a client that wants to show "locked by loki" instead of
"locked by pid 4821" needs to look at the new keys.

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
  picking one — translated to the `ambiguous_name` wire code by
  `registry._api_base` (ticket 006), this module's first real caller of
  the ambiguity path. See "Responses" above and "Remote TCP control
  plane" below.

An updated device dict (once `registry.remote_api`/`render` surface it)
is expected to add `host`/`remote_lock_kind`/`remote_lock_display` to
the shape shown above, plus a `reachable` flag derived from the owning
`peer` row for a remote-owned device — none of that wiring exists yet as
of this ticket, which only touches `store.py`.

## Remote TCP control plane (sprint 003, ticket 006)

Implemented by `mbtools.registry.remote_api.RemoteAPIServer`, sharing the
`list`/`find`/`lock`/`unlock`/`mark_flashed` dispatch implementation
above with the local Unix-socket `api.RegistryAPIServer`
(`mbtools.registry._api_base.BaseAPIServer`) — everything under
"Framing"/"Requests"/"Responses" above applies unchanged to this
transport too, for the five ops it supports, plus `stream` (sprint 003,
ticket 007 — see "Stream sub-protocol" below). `flash` is **not yet
supported here** — that is ticket 008; requesting it on this server today
gets `invalid_request`, same as any unrecognized op.

### Transport

A TCP socket (`AF_INET`, `SOCK_STREAM`), default port `7440`
(`mbtools.registry.remote_api.DEFAULT_REMOTE_PORT`, sprint.md Decision
7 — configurable). Framing is identical to the Unix socket: one
newline-delimited JSON object per line, UTF-8, one response line per
non-streaming request.

### Scope: this registry's own devices only

`list` and `find` (and, transitively, `lock`/`unlock`/`mark_flashed`,
which resolve through the same visibility check) only ever see rows
where `device.host IS NULL` on *this* registry (`Store.
snapshot_local_devices`) — a token that resolves to a peer-owned row
(this registry's own cached copy of some other host's device, written
by `registry.peering`) gets `not_found`, exactly as if the device didn't
exist here. This server never forwards a request to a third host
(sprint.md Decision 8: "a remote client connects directly to the owning
host's registry") — a client that wants peer B's device must connect to
peer B's own `remote_api`, using the `endpoint` its own local registry's
`find`/`list` response supplies (once `registry.peering`/`render`, later
tickets, surface it).

### Session identity, not PID identity

Each accepted connection is issued a fresh, random session id
(`uuid.uuid4`) rather than a PID — "a PID means nothing across hosts"
(sprint.md brief open decision #3) — wrapped in a remote-origin
`HolderRef(origin="remote", ref=<session id>, host=<client's source IP>)`
(`mbtools.registry.locks`, ticket 002). `host` is the connecting client's
own source IP, read from the kernel (`socket.getpeername()`) — never a
client-declared value, mirroring the local API's own "never a
client-supplied PID" precedent for the same reason: an identity used to
grant or refuse a lock should not be something the client gets to assert
for itself.

`lock`/`unlock` acquire against the *same* `LockManager` instance the
local Unix API and `daemon` already share (ticket 002's `HolderRef`
generalization is what makes this safe) — a local and a remote request
for the same uid correctly conflict through one table, regardless of
which API either client used.

### Locking and session lifetime

Mirrors, without duplicating, the local API's own "PID dies *or* the
connection closes" pattern (ASSUMPTION #3), generalized to sessions:

- **Connection close** (clean or abrupt): this server releases exactly
  the locks that session acquired (tracked per-connection), same as the
  local API.
- **Periodic liveness sweep**: independently, a background timer
  (`sweep_interval_s`, default 5s, same default as the local API's own)
  releases any remote-origin lock whose session id is no longer in this
  server's own live-session set. This is what catches a session that
  vanished without an observable connection close — a network
  partition, not a graceful disconnect.
- **TCP keepalive**: every accepted connection gets `SO_KEEPALIVE`
  enabled, plus a shortened idle/interval/count where the platform
  exposes the knobs (Linux: `TCP_KEEPIDLE`/`TCP_KEEPINTVL`/
  `TCP_KEEPCNT`; macOS: `TCP_KEEPALIVE` for idle time only) — so a
  half-dead session's blocked read eventually errors out and joins the
  normal connection-close release path, instead of hanging forever and
  never reaching the liveness sweep's "session id vanished" check either
  (its connection would otherwise still look "live" from this server's
  own point of view).

### Auth (`--auth-token` / `$MBREGISTRY_TOKEN`, sprint.md Decision 6)

Off by default, matching the local Unix socket's own trust model (no
authentication beyond, there, filesystem permissions). When configured,
a connection's first non-empty line must be:

```jsonc
// request
{"token": "<the configured token>"}
// response, on success:
{"ok": true}
// response, on a missing/mismatched token -- connection is then closed:
{"ok": false, "code": "unauthorized", "error": "..."}
```

No op is ever dispatched for a connection that hasn't sent a matching
token first — a request that packs both `"token"` and `"op"` into that
first line is still treated purely as the auth check; the op field is
ignored (and never acted on) until authentication succeeds on a
*subsequent* line. When `auth_token` is unset (the default), this
handshake is skipped entirely and a connection's first line is
dispatched as a normal op, exactly like the local Unix socket.

### Stream sub-protocol (sprint 003, ticket 007)

`{"op": "stream", "uid": "..."}` is the one JSON-op request that does
**not** get an ordinary JSON response and go on to the next line. It
requires the calling connection to already hold a `serial`-kind lock on
`uid` — acquired by *this same connection's* own `lock` call, checked
the same "this connection's own holder, not merely some holder" way
`flash` checks its own `flash`-kind precondition:

```jsonc
// request
{"op": "stream", "uid": "..."}
// response, success:
{"ok": true}
// or, no serial-kind lock held by this connection (or a different kind is held):
{"ok": false, "code": "not_locked", "error": "..."}
// or:
{"ok": false, "code": "not_found", "error": "..."}
// or, missing uid:
{"ok": false, "code": "invalid_request", "error": "..."}
```

On `{"ok": true}`, the connection **permanently** leaves newline-JSON
framing — there is no op after `stream`, and no way back to JSON mode on
this connection. From this point on it speaks the length-prefixed binary
frame format below, for as long as the connection stays open.

**Client synchronization requirement**: the client must not send any
frame bytes before it has received `stream`'s own `{"ok": true}` line.
The server stops reading JSON lines and switches to reading raw bytes off
the socket the moment it dispatches `stream`; bytes sent any earlier than
the ack could already be sitting in the server's line-buffered JSON
reader's own internal decode buffer and would never reach the frame
reader. A client that (like every op above) always waits for a response
before sending its next thing satisfies this automatically.

**Wire format** — sprint.md's Architecture, Decision 1:

```
+----------+------------------+-----------------+
| 1 byte   | 4 bytes          | length bytes    |
| type     | length (BE u32)  | payload         |
+----------+------------------+-----------------+
```

| type | value | direction | payload |
|---|---|---|---|
| `DATA` | `0x01` | either | raw bytes to write to (or just read from) the serial port |
| `BREAK` | `0x02` | client → server | none — triggers `ser.send_break(duration)` on the server's local port, `duration` fixed server-side (`mbtools.serial.connect.BREAK_DURATION`, matching local `mbserial --reset`'s own Linux BREAK duration) |
| `SET_DTR` | `0x03` | client → server | one byte, `0x00`/`0x01` — sets DTR directly |
| `SET_RTS` | `0x04` | client → server | one byte, `0x00`/`0x01` — sets RTS directly |
| `CLOSE` | `0x05` | either | none — ends the stream (and the connection with it — see below) |

A frame with an empty payload (e.g. a zero-length `DATA` frame) is valid
and is simply a no-op write to the port — not an error. A declared length
over `mbtools.registry.stream_frame.MAX_FRAME_PAYLOAD` (1 MiB), or a
frame truncated by the connection ending mid-header or mid-payload, is
rejected without a wire-level error response (there is no framing left
to carry one) — the server logs it and tears the connection down, the
same way a plain connection drop is handled. An unrecognized type byte is
handled the same way. None of these crash the connection's handler
thread or leak the device's lock.

**Server-side mechanics** (`RemoteAPIServer._handle_stream`): the local
port is opened with DTR/RTS held low — no reboot — by calling
`mbtools.serial.connect.open_no_reboot` (the same function
`serial.connect.connect`'s own local path calls; ticket 007 factored it
out of that module rather than duplicating it), and this happens, along
with every subsequent frame read/write, entirely outside the shared
`store`/`locks` lock — that lock is only ever held for the short
`stream`-request precondition check above, never for any serial port
I/O. A background thread pumps whatever the port produces back to the
client as `DATA` frames (mirrors `serial.connect.interact`'s own
reader-thread pattern); the connection's own thread reads frames from
the client and applies them to the port. `CLOSE`, a malformed frame, a
port failure, or the connection simply going away — any of these closes
the local port and releases `uid`'s `serial`-kind lock exactly once, the
same "always release on session end" guarantee
`serial.connect.Session.close` gives a local session.

**Why exposing `SET_DTR`/`SET_RTS` as their own frames, not just
`BREAK`**: local `mbserial --reset` picks BREAK (Linux) or a port reopen
(macOS) depending on the *connecting* platform's DAPLink reset semantics
(see "Ported unchanged..." / "Reset semantics" in
`mbtools/serial/connect.py`'s own module docstring). Over the remote
stream, it is the **owning host's** platform that decides which reset
actually works (sprint.md SUC-004) — so the server exposes both
primitives (`BREAK` and `SET_DTR`/`SET_RTS`), and ticket 013's
`serial.remote_connect` composes whichever one the owning host's
platform needs into its own `--reset`, without this module needing a
third, composed RPC.

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

- **Shared dispatch base (sprint 003, ticket 006).** `list`/`find`/
  `lock`/`unlock`/`mark_flashed` moved into `registry._api_base.
  BaseAPIServer`, a mixin both `api.RegistryAPIServer` (Unix) and
  `remote_api.RemoteAPIServer` (TCP) extend, parametrized on
  `_holder_for_connection`/`_list_visible_devices`/`_device_visible`.
  `flash` (ticket 008's remote-flash territory) stays out of the shared
  base — the local Unix socket's own `flash` op is untouched local-only
  territory (see its own note above), and the remote API doesn't have a
  `flash` op yet. The `stream` op and its binary frame sub-protocol
  (ticket 007) live entirely in `remote_api.py` — the Unix socket has no
  equivalent op (a local `mbserial` opens its port directly, never
  through this API), so there is nothing to share into the base for it.
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
