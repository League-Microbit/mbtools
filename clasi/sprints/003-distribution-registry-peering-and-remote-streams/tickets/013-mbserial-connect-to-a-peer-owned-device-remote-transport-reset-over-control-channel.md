---
id: '013'
title: 'mbserial: connect to a peer-owned device (remote transport, --reset over control
  channel)'
status: open
use-cases: [SUC-004]
depends-on: ['010', '011']
github-issue: ''
issue: mbserial-raw-serial-access-remote.md
completes_issue: true
---
<!-- CLASI: Before changing code or making plans, review the SE process in CLAUDE.md -->

# mbserial: connect to a peer-owned device (remote transport, --reset over control channel)

## Description

Per sprint.md's Architecture (module `serial.cli`/`serial.connect`,
Step 5/UC-011/SUC-004), extend `mbserial <name>` to reach a peer-owned
device. A new `serial.remote_connect` module supplies a `connect()`
returning a `Session`-shaped object (the same six duck-typed members
`serial.connect.Session` already exposes) backed by
`remote_client.open_stream()` instead of a local port, so
`serial.cli`/`interact()`/library callers work unchanged against either.

## Acceptance Criteria

- [ ] New module `mbtools/serial/remote_connect.py`:
      `connect(client: RemoteRegistryClient, target: str, *, reset: bool
      = False, ...) -> RemoteSession`, mirroring
      `serial.connect.connect()`'s own signature and flow (resolve, lock
      `kind=serial` — fail fast, no retry, per SUC-005 — open the
      stream). `RemoteSession` exposes the same
      `reset_input_buffer`/`write`/`flush`/`readline`/`read`/
      `in_waiting` members as `Session`, backed by
      `RemoteStream.write(DATA)`/`.read()` frames, plus `.interact()`/
      `.send_command()` reusing `serial.connect.interact`/
      `send_command` unchanged (both are already duck-typed against
      "anything with `.read`/`.write`" per that module's own docstring,
      so zero new logic is needed there).
- [ ] By default, no reset is sent on connect (same no-reboot default as
      local, SUC-004's "no reset unless asked" postcondition) — the
      stream opens with the owning host's port already held DTR/RTS low
      by ticket 007's server-side `_open_no_reboot` reuse.
- [ ] `--reset` sends the reset primitive appropriate to the *owning
      host's* platform (sprint.md's SUC-004: "the owning host's platform
      decides which") — the client doesn't need to know the remote
      platform itself; it sends a single `RESET` intent, and the server
      (ticket 007) picks BREAK vs. DTR-toggle based on its own
      `_current_platform()`, exactly mirroring the local
      `serial.connect`'s existing Linux/macOS branch, now evaluated
      server-side. (If ticket 007's frame types don't already include a
      single "reset, you pick how" frame vs. separate BREAK/SET_DTR
      primitives, this ticket composes `--reset` client-side by sending
      `BREAK` — matching the existing local Linux behavior — and treats
      "does the remote board actually reset on a plain BREAK regardless
      of the owning host's OS" as a question for ticket 014's hardware
      pass, since the *board's* reset mechanics, not the connecting
      client's OS, are what's platform-specific per `docs/brief.md` §8.)
- [ ] `serial.cli` branches on `device.get("host")` exactly like
      `deploy.cli` (ticket 012) — `None` keeps the untouched local flow;
      non-`None` uses `endpoint` to build a `RemoteRegistryClient` and
      calls `serial.remote_connect.connect()`.
- [ ] Busy-device error message includes host (same
      `DeviceLockedError.holder` extension ticket 012 makes to
      `deploy.cli`'s formatting — shared wording, not reimplemented per
      client tool).
- [ ] `RemoteSession.close()` is idempotent and is the one place the
      `serial`-kind lock is released, on every exit path (Ctrl-D, Ctrl-C,
      explicit library close) — same guarantee `Session.close()` already
      documents for the local path.

## Testing

- **Existing tests to run**: the full existing `mbserial connect` local
  test suite — must pass unchanged.
- **New tests to write**:
  - `serial.remote_connect.connect()` against a fake `RemoteRegistryClient`/
    `RemoteStream` (or a real loopback `RemoteAPIServer` from ticket 007):
    resolve, lock, open, no-reset-by-default, `--reset` sends the right
    frame, close releases the lock.
  - `interact()`/`send_command()` work unchanged against a
    `RemoteSession` (proving the duck-typing claim above rather than
    assuming it).
  - Busy-remote-device error message includes host.
- **Verification command**: `uv run pytest tests/serial/`
