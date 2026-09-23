---
id: '006'
title: 'registry.remote_api: TCP control-plane server (list/find/lock/unlock/mark_flashed)'
status: open
use-cases: [SUC-003, SUC-004]
depends-on: ['001', '002']
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

- [ ] A shared base (e.g. `registry._api_base.BaseAPIServer` or a mixin —
      implementer's judgment on the exact shape) holds `_op_list`,
      `_op_find`, `_op_lock`, `_op_unlock`, `_op_mark_flashed`, and the
      `_write`/`_device_dict`/`_error` helpers, parametrized on how a
      concrete subclass turns a raw connection into a `HolderRef`
      (`_holder_for_connection(conn) -> HolderRef`, replacing the direct
      `peer_pid_fn` call inline in today's dispatch). `api.RegistryAPIServer`
      is refactored to extend/use this base with **zero observable
      behavior change** for any existing Unix-socket caller or test.
- [ ] New module `mbtools/registry/remote_api.py` with a
      `RemoteAPIServer` class: binds a TCP socket (`--remote-port`,
      default `7440`), one thread per accepted connection (same
      threading model as the Unix socket, per sprint.md's component
      diagram), constructs `HolderRef(origin="remote", ref=str(uuid4()),
      host=<this connecting client's source IP or a client-declared
      display host, whichever the implementer finds a cleaner v1 — see
      Open Questions>)` per connection.
- [ ] `list`/`find` responses, for a remote client, only ever include
      devices where `host IS NULL` on this registry (a remote client
      resolving a name this registry doesn't own is `not_found`, exactly
      as if the device didn't exist here — it's the *client's own local*
      registry that knows which peer to route to; this remote API never
      forwards a request to a third host).
- [ ] `lock`/`unlock` use the same `LockManager` instance `daemon`/the
      local `api` already share (ticket 002's `HolderRef` generalization
      is what makes this safe — a local and a remote request for the
      same uid now correctly conflict through one table).
- [ ] Connection close (clean or abrupt) releases exactly the locks that
      connection's session acquired — same pattern as the Unix socket's
      existing `acquired_uids` tracking, generalized to `HolderRef`.
- [ ] A periodic liveness sweep for remote sessions: `remote_api` tracks
      its own live-connection set and supplies an `is_alive` check for
      `origin == "remote"` holders (ticket 002's dispatch point) so a
      remote session that vanished without a clean close (network
      partition, not just a graceful disconnect) still gets its locks
      released — mirroring, not duplicating, the Unix socket's existing
      sweep-thread pattern.
- [ ] If `--auth-token`/`$MBREGISTRY_TOKEN` (Decision 6) is set, every
      connection must send a matching token as its first message or is
      rejected before any op is dispatched; unset (default) means no
      auth check at all, matching today's Unix-socket trust model.
- [ ] `docs/design/registry-api.md` gets a new section documenting the
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
