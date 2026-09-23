---
id: 009
title: 'mbserial: local connect (resolve, serial-lock, open without reboot, interact,
  --reset)'
status: in-progress
use-cases:
- SUC-004
- SUC-005
depends-on:
- '001'
github-issue: ''
issue: mbserial-raw-serial-access-local-or-remote.md
completes_issue: true
---
<!-- CLASI: Before changing code or making plans, review the SE process in CLAUDE.md -->

# mbserial: local connect (resolve, serial-lock, open without reboot, interact, --reset)

## Description

Build `mbtools.serial.connect` (session logic) and `mbtools.serial.cli`
(the `mbserial <name> [--reset] [message...]` entry point, replacing the
sprint-001 stub), implementing SUC-004 and reusing SUC-005's fail-fast
pattern.

Per sprint.md's Architecture and Design Rationale ("mbserial's local
transport opens the port directly, not always through a registry TCP
stream"):

1. Resolve `<name>` via `registry.client.find()` (ticket 001).
2. Lock it (`kind=serial`) via `registry.client.lock()`. On a `locked`
   failure, fail fast naming the holder's kind and PID (SUC-005) — same
   pattern as ticket 007's flash lock, same underlying
   `registry.client.lock()` call.
3. Open the port directly: port DTR/RTS held low by default (no reboot —
   ported `open_port` from today's `mbdeploy`'s `console.py`,
   `/Volumes/Proj/proj/robot-projects/mbdeploy/src/mbdeploy/console.py`).
   If `--reset` was given, deliberately reset the board as part of
   connect: a serial BREAK on Linux (closing/reopening does *not* reset
   a DAPLink target there), a plain reopen on macOS (which *does* reset
   it there) — this platform branch is new code, not ported, since
   today's `mbdeploy console.py` never resets on connect at all.
4. Hand the caller a session: `console.interact()` for the no-message
   interactive terminal, or `console.send_command()` for a one-shot
   message — both ported unchanged from `mbdeploy`'s `console.py` (they
   are already duck-typed against a serial-like object, per that
   module's own docstring, so zero lines of that logic change). Also
   expose the underlying session object for library callers, not just
   the CLI.
5. On exit (Ctrl-D/Ctrl-C from `interact()`, or a library caller closing
   the session), release the lock via `registry.client.unlock()`.

## Acceptance Criteria

- [x] `mbserial <name>` opens the local board's port with DTR/RTS held
      low by default — no reboot — and hands the user an interactive
      terminal (ported `console.interact`).
- [x] `mbserial <name> --reset` deliberately resets the board as part of
      connect: BREAK on Linux, reopen on macOS.
- [x] `mbserial <name> <message...>` (one-shot mode) sends the message
      and prints the reply lines, ported unchanged from `console.
      send_command`'s idle-gap/timeout behavior.
- [x] An already-locked device fails fast with the holder's kind and PID
      (`EXIT_LOCKED`) — no retry, no blocking wait, same as ticket 007's
      flash-lock fail-fast.
- [x] The `serial`-kind lock is released on every exit path: normal
      Ctrl-D, Ctrl-C, and a library caller explicitly closing the
      session — no leaked lock on any of these.
- [x] The session object returned to a library caller (not just the CLI)
      is usable directly — the `SocketSerial`-shaped idea from
      `mbdeploy`'s `remote.py` is the reference shape spec §5.1 names,
      even though this ticket's transport is a direct local port, not a
      socket.

## Testing

- **Existing tests to run**: `uv run pytest tests/registry/` (must still
  pass; no shared code touched).
- **New tests to write**: an in-process daemon+API against a `tmp_path`
  store and socket, with `FakeSerial` (`mbtools.testing.fakes`, sprint
  001) standing in for the port — asserts DTR/RTS held low by default;
  asserts `--reset` drives the platform-specific reset path (the running
  platform's real branch covered directly, the other platform's branch
  covered via an injectable "which platform" seam rather than skipped
  outright); already-locked fail-fast; lock released on normal exit,
  simulated Ctrl-C, and explicit library-caller close.
- **Verification command**: `uv run pytest tests/serial/`
