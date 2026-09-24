---
id: '006'
title: 'registry.remote_api: TCP control-plane server (list/find/lock/unlock/mark_flashed)'
status: done
use-cases:
- SUC-003
- SUC-004
depends-on:
- '001'
- '002'
github-issue: ''
issue: mbregistry-peering-mdns-and-zeromq.md
completes_issue: true
---
<!-- CLASI: Before changing code or making plans, review the SE process in CLAUDE.md -->

# registry.remote_api: TCP control-plane server (list/find/lock/unlock/mark_flashed)

## Description

Per sprint.md's Architecture (module `registry.remote_api`, Step 5), add
a TCP listener parallel to `api.RegistryAPIServer`'s existing Unix
socket, giving a client on another host the same `list`/`get`/`find`/
`lock`/`unlock`/`mark_flashed` ops — same newline-delimited-JSON framing
as `docs/design/registry-api.md` already documents — but scoped to
*this* registry's own (`host IS NULL`) devices, and using a per-connection
session id (not `SO_PEERCRED`) as the `HolderRef` (ticket 002) for
`lock`/`unlock`. This ticket covers the JSON-op sub-protocol only;
`stream` (ticket 007) and remote `flash` (ticket 008) are separate
tickets since they change for different reasons (sprint.md Step 1-2,
responsibilities 3/4/5).

Refactor `api.RegistryAPIServer`'s per-op dispatch methods (`_op_list`,
`_op_find`, `_op_lock`, `_op_unlock`, `_op_mark_flashed`) into a shared
base both the existing Unix-socket server and this new TCP server use,
rather than duplicating that logic — the two differ only in transport,
peer-identity extraction, and (tickets 007/008) which extra ops they
support.

## Acceptance Criteria

- [x] A shared base (e.g. `registry._api_base.BaseAPIServer` or a mixin —
      implementer's judgment on the exact shape) holds `_op_list`,
      `_op_find`, `_op_lock`, `_op_unlock`, `_op_mark_flashed`, and the
      `_write`/`_device_dict`/`_error` helpers, parametrized on how a
      concrete subclass turns a raw connection into a `HolderRef`
      (`_holder_for_connection(conn) -> HolderRef`, replacing the direct
      `peer_pid_fn` call inline in today's dispatch). `api.RegistryAPIServer`
      is refactored to extend/use this base with **zero observable
      behavior change** for any existing Unix-socket caller or test.
- [x] New module `mbtools/registry/remote_api.py` with a
      `RemoteAPIServer` class: binds a TCP socket (`--remote-port`,
      default `7440`), one thread per accepted connection (same
      threading model as the Unix socket, per sprint.md's component
      diagram), constructs `HolderRef(origin="remote", ref=str(uuid4()),
      host=<this connecting client's source IP or a client-declared
      display host, whichever the implementer finds a cleaner v1 — see
      Open Questions>)` per connection.
- [x] `list`/`find` responses, for a remote client, only ever include
      devices where `host IS NULL` on this registry (a remote client
      resolving a name this registry doesn't own is `not_found`, exactly
      as if the device didn't exist here — it's the *client's own local*
      registry that knows which peer to route to; this remote API never
      forwards a request to a third host).
- [x] `lock`/`unlock` use the same `LockManager` instance `daemon`/the
      local `api` already share (ticket 002's `HolderRef` generalization
      is what makes this safe — a local and a remote request for the
      same uid now correctly conflict through one table).
- [x] Connection close (clean or abrupt) releases exactly the locks that
      connection's session acquired — same pattern as the Unix socket's
      existing `acquired_uids` tracking, generalized to `HolderRef`.
- [x] A periodic liveness sweep for remote sessions: `remote_api` tracks
      its own live-connection set and supplies an `is_alive` check for
      `origin == "remote"` holders (ticket 002's dispatch point) so a
      remote session that vanished without a clean close (network
      partition, not just a graceful disconnect) still gets its locks
      released — mirroring, not duplicating, the Unix socket's existing
      sweep-thread pattern.
- [x] If `--auth-token`/`$MBREGISTRY_TOKEN` (Decision 6) is set, every
      connection must send a matching token as its first message or is
      rejected before any op is dispatched; unset (default) means no
      auth check at all, matching today's Unix-socket trust model.
- [x] `docs/design/registry-api.md` gets a new section documenting the
      remote TCP protocol's JSON-op shape (identical to the existing
      table for the ops it shares) and the session-based `holder` field
      shape (`{"kind":..., "origin":"remote", "host":..., "pid":null}`)
      for a `locked` response involving a remote holder — noted as an
      additive superset of the existing local `{"kind","pid"}` shape, not
      a breaking change to it.

## Testing

- **Existing tests to run**: `tests/registry/api/` — must pass unchanged
  after the shared-base refactor, proving zero behavior change to the
  Unix-socket path.
- **New tests to write**:
  - Every op (`list`/`find`/`lock`/`unlock`/`mark_flashed`) exercised
    over a real loopback TCP connection to `RemoteAPIServer`.
  - A local (Unix-socket) lock and a remote (TCP) lock on the same uid
    correctly conflict (`locked` response), proving the shared
    `LockManager` table is genuinely shared.
  - Connection drop (socket closed without an `unlock`) releases the
    session's locks, both via clean close and via the liveness sweep for
    an abrupt drop.
  - Auth token: a connection without the right token is rejected when
    `--auth-token` is set; every op works normally when it isn't.
- **Verification command**: `uv run pytest tests/registry/`

## Implementation Notes

- **Shared base**: `mbtools/registry/_api_base.py`, a plain mixin
  (`BaseAPIServer`) rather than an `abc.ABC` — every hook has a sensible
  default except `_holder_for_connection`, which raises
  `NotImplementedError` loudly if a subclass forgets it. Holds
  `_op_list`/`_op_find`/`_op_lock`/`_op_unlock`/`_op_mark_flashed` and
  the `_write`/`_device_dict`/`_error` helpers, plus a new
  `_resolve_visible(token)` shared by all four ops that call
  `store.find` — it translates `AmbiguousNameError` to the new
  `ambiguous_name` wire code and applies the subclass's visibility hook
  in one place, so `_op_find`/`_op_lock`/`_op_unlock`/`_op_mark_flashed`
  each do one `record, err = self._resolve_visible(token)` instead of
  repeating the find/None-check/visibility-check sequence four times.
- **`api.RegistryAPIServer`** now extends `BaseAPIServer` and supplies
  `_holder_for_connection` (wraps `self._peer_pid_fn(conn)`'s pid in a
  local `HolderRef`, replacing the old inline `_local_holder(pid)` calls
  scattered across `_op_lock`/`_op_unlock`/the connection-close path)
  and leaves `_list_visible_devices`/`_device_visible` at their base
  defaults (every device visible, unchanged). `_op_flash` (ticket 008's
  remote-flash territory) was deliberately left untouched line-for-line
  in `api.py` — it still takes a raw `pid: int` and calls
  `_local_holder(pid)` internally, exactly as before — since it isn't
  part of this ticket's shared-base scope and any refactor of it risked
  the "zero observable behavior change" acceptance criterion for no
  benefit this ticket needed. `tests/registry/api/test_api.py` (30
  tests, unmodified) passes unchanged, proving that.
- **`_holder_wire_dict`** (in `_api_base.py`) builds a `locked`
  response's `"holder"` field: exactly the pre-existing 2-key
  `{"kind","pid"}` shape for a local holder (asserted verbatim by
  `test_lock_already_locked_returns_holder_kind_and_pid`, which still
  passes), and that same 2-key dict plus `"origin"`/`"host"` for a
  remote holder — an additive superset, never renaming or dropping the
  local shape's keys. Exercised from both directions in the new test
  suite: a remote contender against a local holder gets the plain 2-key
  shape; a local contender against a remote holder gets the 4-key one.
- **`mbtools/registry/remote_api.py`** (`RemoteAPIServer`): TCP
  (`AF_INET`), default port `7440`
  (`DEFAULT_REMOTE_PORT`), one thread per connection plus one sweep
  thread — same shape as `api.RegistryAPIServer`. `_holder_for_connection`
  mints `HolderRef(origin="remote", ref=str(uuid4()),
  host=conn.getpeername()[0])` — chose the kernel-verified source IP
  over a client-declared display host (the ticket's own open choice)
  for the same reason the local API never trusts a client-supplied PID:
  an identity that arbitrates a lock conflict shouldn't be something the
  client gets to assert. `_list_visible_devices`/`_device_visible`
  override the base's defaults to scope everything to
  `store.snapshot_local_devices()`/`record.host is None`.
- **Liveness sweep for remote sessions**: `RemoteAPIServer` keeps its own
  `_live_sessions: set[str]` (added on accept, discarded in the
  connection handler's `finally`, guarded by a small dedicated
  `_sessions_lock` — deliberately not the shared `self._lock`, since
  membership bookkeeping never touches `store`/`locks`) and its own
  `_sweep_loop`/`_is_holder_alive`, mirroring (not duplicating)
  `api.py`'s. Each server's `_is_holder_alive` is symmetric: it is
  authoritative only over holders of its own origin and reports the
  other origin always-alive, so the two sweep threads never
  second-guess each other even though both run against the one shared
  `LockManager` table. `api.py`'s own `_is_holder_alive` docstring was
  updated to describe this symmetry (it previously described the
  "remote" branch as literally unreachable, which stopped being true
  once this ticket's `remote_api` sweep exists) — a comment-only change,
  no behavior change to that method's local branch.
- **TCP keepalive** (`RemoteAPIServer._configure_keepalive`, a
  `staticmethod` so the new test can call it directly against a real
  loopback socket pair without spinning up a whole server): enables
  `SO_KEEPALIVE` plus, where the platform exposes them,
  `TCP_KEEPIDLE`/`TCP_KEEPINTVL`/`TCP_KEEPCNT` (Linux) or
  `TCP_KEEPALIVE` (macOS's idle-time-only knob) — each looked up via
  `getattr(socket, name, None)` and set in its own `try/except OSError`,
  matching this project's existing best-effort/platform-gated
  precedent (`api.py`'s own `SO_PEERCRED`/`LOCAL_PEERPID` dispatch).
  This is what lets a genuinely network-partitioned connection's blocked
  read eventually error out and join the normal close-triggered release
  path, rather than leaving its session in `_live_sessions` forever
  (which would otherwise make the periodic sweep blind to it too, since
  the sweep's `is_alive` check is exactly "is this session id still in
  `_live_sessions`").
- **Testing the sweep without a real network partition**: real OS-level
  keepalive timeouts are far too slow (and inherently non-deterministic
  across CI/dev machines) for a fast unit-test suite to wait on, so
  `test_periodic_sweep_releases_a_vanished_remote_sessions_lock`
  constructs a `HolderRef(origin="remote", ref="ghost-session", ...)`
  directly against the shared `LockManager` — a session id that was
  never added to the server's own `_live_sessions` set — and asserts the
  short-interval sweep releases it. This mirrors ticket 002's own
  precedent for testing `LockManager.sweep`'s dispatch logic with a
  directly-constructed `HolderRef` rather than real OS/network fault
  injection; it exercises the actual production `_is_holder_alive`
  dispatch and `_live_sessions` membership check, just supplies the
  "session already isn't live" precondition directly instead of via a
  real dropped connection (which the *other* new test,
  `test_connection_close_releases_that_sessions_locks`, already covers
  for the clean/abrupt-but-observable-close case).
- **Auth handshake wire shape** (not fully specified by sprint.md,
  implementer's choice per the ticket): when `auth_token` is configured,
  a connection's first non-empty line must be `{"token": "<value>"}`;
  success gets `{"ok": true}`, failure gets
  `{"ok": false, "code": "unauthorized", ...}` and the connection is
  torn down without ever reaching `_dispatch_line`. Verified directly:
  `test_no_op_is_dispatched_before_a_bad_token_is_rejected` sends a
  first line carrying both a wrong token *and* a `lock` op in the same
  JSON object and asserts the lock was never taken.
- **New wire codes** (`mbtools/common/__init__.py`): `CODE_AMBIGUOUS_NAME
  = "ambiguous_name"` and `CODE_UNAUTHORIZED = "unauthorized"`. The
  former is reachable from *both* APIs (it lives in the shared base) —
  `store.find`'s `AmbiguousNameError` (ticket 001) was left untranslated
  pending "this module's first real API-layer caller", which this
  ticket is; documented as a general addition to the `code` table in
  `docs/design/registry-api.md`, not scoped only to the remote section.
  `CODE_UNAUTHORIZED` is remote-only (the local Unix socket has no auth
  concept).
- **Docs**: `docs/design/registry-api.md` gained a "Remote TCP control
  plane" section (transport, scope, session identity, locking/session
  lifetime, auth handshake), a "Lock holder wire shape" subsection under
  "Responses" (the 2-key vs. 4-key shapes), two new `code` table rows,
  and a "Shared dispatch base" bullet under "Known limitations". The
  top-of-file intro line now credits both servers.
- **Tests**: new file `tests/registry/remote_api/test_remote_api.py` (20
  tests) — basename checked unique across `tests/` first. Existing
  `tests/registry/api/test_api.py` (30 tests) was not modified and
  passes unchanged.
- **Test run**: `uv run pytest tests/registry/remote_api/ -q` → 20
  passed. `uv run pytest tests/registry/ -q` → 307 passed, 1 skipped
  (the platform-gated `SO_PEERCRED` test, as before). Full suite
  `uv run pytest -q` → 432 passed, 2 skipped — no regressions anywhere
  in the tree.
- **Deviations from the ticket**: none in substance. Two implementer
  judgment calls the ticket explicitly left open were made as described
  above: the shared base's exact shape (a plain mixin, `_api_base.py`)
  and the remote `HolderRef.host` source (kernel-verified source IP, not
  a client-declared display host).
