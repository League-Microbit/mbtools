---
id: '013'
title: 'mbserial: connect to a peer-owned device (remote transport, --reset over control
  channel)'
status: done
use-cases:
- SUC-004
depends-on:
- '010'
- '011'
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

- [x] New module `mbtools/serial/remote_connect.py`:
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
- [x] By default, no reset is sent on connect (same no-reboot default as
      local, SUC-004's "no reset unless asked" postcondition) — the
      stream opens with the owning host's port already held DTR/RTS low
      by ticket 007's server-side `_open_no_reboot` reuse.
- [x] `--reset` sends the reset primitive appropriate to the *owning
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
- [x] `serial.cli` branches on `device.get("host")` exactly like
      `deploy.cli` (ticket 012) — `None` keeps the untouched local flow;
      non-`None` uses `endpoint` to build a `RemoteRegistryClient` and
      calls `serial.remote_connect.connect()`.
- [x] Busy-device error message includes host (same
      `DeviceLockedError.holder` extension ticket 012 makes to
      `deploy.cli`'s formatting — shared wording, not reimplemented per
      client tool).
- [x] `RemoteSession.close()` is idempotent and is the one place the
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

## Implementation Notes

- **`serial.remote_connect.connect()`/`RemoteSession`**: mirrors
  `serial.connect.connect()`'s own flow exactly — resolve
  (`client.find(target)`), lock (`kind=serial`, fail fast, no retry), then
  `client.open_stream(uid)` on the *same* connection (`open_stream`
  requires this connection's own `serial`-kind lock, per ticket 011's own
  precondition). If `open_stream` itself fails, the lock is released on
  this same still-usable connection before the exception propagates
  (mirrors `serial.connect.connect`'s "unlock on any failure after
  locking" guarantee); once `open_stream` succeeds, the connection is
  spent (ticket 011's "no way back to JSON framing" rule) and there is no
  explicit `unlock` call at all from here on — `RemoteSession.close()` is
  the only release path, via `RemoteStream.close()` ending the TCP
  session, which is what `RemoteAPIServer`'s own connection-close release
  triggers server-side.
- **`RemoteSession`'s six duck-typed members**: a background thread
  (`_pump`) continuously calls the blocking, per-frame
  `RemoteStream.read()` and appends payloads to an internal
  `bytearray` buffer guarded by a `threading.Condition`. `read`/`readline`
  wait on that buffer bounded by `serial.connect.READ_TIMEOUT` (the same
  budget a locally-opened pyserial port is itself configured with) rather
  than blocking on the socket directly — this is what lets
  `send_command()`'s own overall-timeout/idle-gap polling loop keep
  terminating against this transport exactly as it does locally.
  `interact()`/`send_command()` are imported from `serial.connect`
  unchanged and called against `self` (the `RemoteSession` instance) —
  zero new logic needed there, confirming the ticket's own duck-typing
  claim rather than assuming it (covered by
  `test_mbserial_remote_connect.py`'s `TestInteract`-equivalent and
  `send_command` tests).
- **`--reset` composes a plain `BREAK`, client-side**: ticket 007's wire
  protocol (`stream_frame`'s five frame types) has no single "reset, you
  pick how" primitive — `FRAME_BREAK` unconditionally triggers
  `ser.send_break(...)` server-side with no platform branch of its own.
  Per this ticket's own acceptance-criterion fallback clause,
  `connect(reset=True)` sends `RemoteStream.send_break()` — matching
  today's local Linux behavior — and leaves "does a plain BREAK actually
  reset the board regardless of the owning host's OS" as a question for
  ticket 014's hardware pass.
- **Shared `format_locked_message` helper**: `deploy.cli`'s own
  `_format_locked` (ticket 012) was extracted, unmodified in behavior,
  into `mbtools.common.format_locked_message` and both `deploy.cli` and
  `serial.cli` now import and call it — one place owns the "locked for
  `<kind>` by pid `<pid>`" vs. "by a remote session on `<host>`" wording,
  rather than a second copy per client tool. `deploy.cli`'s own existing
  `test_already_locked_fails_fast_naming_holder` still passes unchanged,
  proving the extraction didn't alter behavior.
- **`serial.cli` restructuring**: `_run_connect` now only resolves
  `<name>` and branches on `device.get("host")` (mirroring `deploy.cli`'s
  ticket-012 `_run_deploy`); `_run_connect_local`/`_run_connect_remote`
  each build their own client/session and funnel into a new shared
  `_run_session(session, args, banner)` that runs the interactive-or-
  one-shot body and closes the session on every exit path — identical
  code for either `Session` or `RemoteSession`. The remote branch's
  `RegistryUnavailable` handling (endpoint parsing via `rpartition(":")`,
  the "is `<host>`'s registry daemon reachable?" message) mirrors
  `deploy.cli`'s own ticket-012 remote branch precedent verbatim in
  shape, not literally shared code (the two flows differ in what they do
  once connected: flash vs. interact/send_command).
- **`--baud` has no effect on the remote branch**: `open_stream`'s wire
  op takes no baud parameter (ticket 007's protocol is already frozen at
  the server's own configured baud) — out of this ticket's scope to
  change. The remote banner therefore omits "at N baud" (unlike the local
  banner) rather than claim a baud override that doesn't actually reach
  the owning host.
- **Testing**: `tests/serial/test_mbserial_remote_connect.py` (new, 10
  tests) drives `remote_connect.connect()`/`RemoteSession` against a real
  loopback `RemoteAPIServer` + `FakeSerial` — connect/lock/open_stream,
  close idempotency + lock release, already-locked fail-fast (stream
  never opened, elapsed < 2s), unknown-target `DeviceNotFoundError`,
  `--reset` sends `BREAK` (and no-reset sends none), and
  `interact()`/`send_command()`/`write()` unchanged. One test
  (`test_send_command_reads_the_boards_reply`) scripts
  `announcement_after_writes=1` rather than an unprompted announcement —
  an unprompted announcement would race `send_command()`'s own
  `reset_input_buffer()` call (which, unlike local `FakeSerial`'s no-op
  version, actually discards whatever `RemoteSession`'s background pump
  thread has already buffered), the same real-hardware-realistic
  behavior an OS-level serial receive buffer would exhibit, not a bug in
  `remote_connect.py`.
  `tests/serial/test_mbserial_cli_remote.py` (new, 5 tests) mirrors
  `tests/deploy/test_deploy_cli_remote.py`'s fixture split (local
  `RegistryAPIServer` seeded with a `host`-tagged device + owning
  `RemoteAPIServer`) for: locked-remote-device message names host not
  null pid, owning-host-unreachable-at-connect reports cleanly (no
  traceback), interactive end-to-end, one-shot message end-to-end
  (same `announcement_after_writes=1` reasoning as above), `--reset`
  sends a `BREAK` over the wire. `tests/serial/` + `tests/deploy/`: 125
  passed, 1 skipped (pre-existing, unrelated platform skip) — the
  pre-existing local-only suites are unchanged. Full project suite:
  `uv run pytest -q` — 558 passed, 2 skipped, no failures.
