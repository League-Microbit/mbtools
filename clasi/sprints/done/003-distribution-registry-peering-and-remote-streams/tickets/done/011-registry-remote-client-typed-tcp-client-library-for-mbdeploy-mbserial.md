---
id: '011'
title: 'registry.remote_client: typed TCP client library for mbdeploy/mbserial'
status: done
use-cases:
- SUC-003
- SUC-004
depends-on:
- '006'
- '007'
- 008
github-issue: ''
issue: mbregistry-peering-mdns-and-zeromq.md
completes_issue: true
---
<!-- CLASI: Before changing code or making plans, review the SE process in CLAUDE.md -->

# registry.remote_client: typed TCP client library for mbdeploy/mbserial

## Description

Per sprint.md's Architecture (module `registry.remote_client`), add the
TCP-protocol counterpart to `registry.client.RegistryClient` — same
method names and exception shapes so `deploy.cli`/`serial.cli` (tickets
012/013) can switch between local and remote with one branch, not two
mental models. This is the shared foundation both client tools' remote
paths (tickets 012, 013) build on; it has no CLI/argparse of its own.

## Acceptance Criteria

- [x] New module `mbtools/registry/remote_client.py`,
      `RemoteRegistryClient(host, port)`: same session model as
      `RegistryClient` (one persistent connection, opened lazily or via
      `connect()`/`with`, closed via `close()` — module docstring's
      "Session model" convention carried over verbatim, since the same
      "a lock taken on this connection must be released, or reported, on
      this same connection" reasoning applies).
- [x] Methods: `find(uid)`, `lock(uid, kind)`, `unlock(uid)`, `flash(uid,
      hex_path, log_callback)` (streamed, same log/result framing as
      `RegistryClient`'s local counterpart would need if it had one —
      this is the first client-side streaming-flash consumer in the
      codebase), `open_stream(uid) -> RemoteStream` (the binary
      sub-protocol client side: `write(data)`, `read()`, `send_break()`,
      `set_dtr(bool)`, `set_rts(bool)`, `close()`).
- [x] Same exception hierarchy as `registry.client`
      (`RegistryUnavailable`, `RegistryClientError` and its subclasses,
      including `DeviceLockedError` with `holder`) — reused directly by
      importing from `registry.client`, not redefined, so a caller's
      `except` clauses work identically against either client class.
      `DeviceLockedError.holder`, for a remote-owned device's lock
      conflict, now correctly carries `{"kind":..., "origin":"remote",
      "host":..., "pid":null}` per ticket 006's response shape — no
      client-side change needed beyond passing the dict through
      unchanged, since `holder` was always typed as a free-form dict.
- [x] If `--auth-token` is in play (Decision 6), the client sends it as
      the connection's first message, matching ticket 006's server-side
      expectation.
- [x] `mtools.registry.render` (ticket 010) is not a dependency of this
      module — this is a pure protocol client, same boundary
      `registry.client` already has.

## Testing

- **Existing tests to run**: none affected (new module).
- **New tests to write**: every method exercised against a real
  loopback `RemoteAPIServer` (tickets 006/007/008) — `find`/`lock`/
  `unlock`/`flash`/`open_stream` each round-tripped, plus the exception-
  mapping tests `registry.client`'s own suite already has, repeated
  against this class to prove behavioral parity.
- **Verification command**: `uv run pytest tests/registry/remote_client/`
