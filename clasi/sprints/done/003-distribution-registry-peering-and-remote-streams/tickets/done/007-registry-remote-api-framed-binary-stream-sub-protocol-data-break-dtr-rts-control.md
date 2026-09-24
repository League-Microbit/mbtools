---
id: '007'
title: 'registry.remote_api: framed binary stream sub-protocol (data + BREAK/DTR/RTS
  control)'
status: done
use-cases:
- SUC-004
depends-on:
- '006'
github-issue: ''
issue: mbserial-raw-serial-access-remote.md
completes_issue: true
---
<!-- CLASI: Before changing code or making plans, review the SE process in CLAUDE.md -->

# registry.remote_api: framed binary stream sub-protocol (data + BREAK/DTR/RTS control)

## Description

Per sprint.md's Architecture (Decision 1), add the `stream` op to
`registry.remote_api`: a client already holding a `serial`-kind lock
(via ticket 006's `lock` op, on the same TCP connection) sends
`{"op": "stream", "uid": "..."}`, and the connection switches from
newline-JSON framing into the binary framed sub-protocol for the rest of
its life — data both directions, plus out-of-band control frames (BREAK,
DTR, RTS) the server applies to the real local serial port.

This is the module's data-plane half (sprint.md Step 1-2, responsibility
4), kept as its own ticket because it changes for different reasons than
the JSON control-plane ops (ticket 006) and has entirely different
framing rules.

## Acceptance Criteria

- [x] Frame format implemented exactly as sprint.md's Decision 1
      specifies: `[1-byte type][4-byte big-endian length][payload]`,
      types `DATA=0x01`, `BREAK=0x02`, `SET_DTR=0x03`, `SET_RTS=0x04`,
      `CLOSE=0x05`. Documented in `docs/design/registry-api.md`'s new
      remote-protocol section (ticket 006 started it; this ticket adds
      the stream sub-protocol's own subsection).
- [x] `stream` requires the connection to already hold a `serial`-kind
      lock on `uid` (same connection's own `HolderRef` — checked the same
      way `_op_flash`'s existing precondition check works in `api.py`);
      `not_locked` if not.
- [x] On entering stream mode, the server opens the local port with
      DTR/RTS held low (no reboot) — same
      `serial.connect._open_no_reboot` behavior, reused rather than
      reimplemented (the implementer should factor the no-reboot-open
      helper so both `serial.connect` and this module call the same
      code, not duplicate it).
- [x] A `DATA` frame from the client is written to the port; anything
      read from the port is sent back to the client as `DATA` frames
      (a background reader thread on the server side, mirroring
      `serial.connect.interact`'s own reader-thread pattern).
- [x] A `BREAK` frame triggers `ser.send_break(duration)` on the server's
      local port (server-side platform is what matters for reset
      semantics, per sprint.md's SUC-004: "the *owning host's* platform
      decides which"). `SET_DTR`/`SET_RTS` frames (payload: one byte,
      0/1) toggle the corresponding line directly — this is the primitive
      `serial.remote_connect` (ticket 013) composes into a `--reset` that
      matches local `--reset`'s existing Linux-BREAK/macOS-reopen
      distinction, decided client-side by which reset the connecting
      user asked for and which frame the server-side platform needs;
      exposing both primitives here (not just BREAK) is what makes that
      composition possible without adding a third RPC.
- [x] `CLOSE` (or a plain connection drop) closes the local port and
      releases the `serial`-kind lock — same "always release on session
      end" guarantee as `serial.connect.Session.close`.
- [x] Frame parsing is fuzz/boundary tested: a truncated frame, a
      zero-length `DATA` frame, and an oversized declared length (a
      client-sent length that would read past any reasonable buffer)
      are all rejected without crashing the connection handler thread or
      leaking the lock.

## Testing

- **Existing tests to run**: `tests/registry/api/`,
  `tests/serial/connect/` — must be unaffected (no changes to the local
  Unix-socket path or local `serial.connect` in this ticket).
- **New tests to write**:
  - Frame encode/decode round-trip for every type.
  - `stream` without a prior `lock` call is refused.
  - A full session against a fake serial port (mirrors
    `mbtools.testing.fakes.FakeSerial`, the same fixture
    `serial.connect`'s own tests use): open, write, read, BREAK,
    SET_DTR, close, lock released after close.
  - Malformed-frame handling per the boundary-testing acceptance
    criterion above.
- **Verification command**: `uv run pytest tests/registry/remote_api/`

## Implementation Notes

- **`serial.connect.open_no_reboot`**: `serial.connect._open_no_reboot`
  was renamed to `open_no_reboot` (public, added to `__all__`) rather
  than duplicated — its signature (`factory, port, baud, settle`) was
  already transport-agnostic (a plain pyserial-shaped factory callable),
  so no other change to `serial.connect` was needed.
  `remote_api._handle_stream` imports and calls it directly. No caller of
  the old private name existed outside `serial/connect.py` itself
  (checked across `src/` and `tests/`), so this is a pure rename, not a
  compatibility-preserving alias.
- **New module `mbtools/registry/stream_frame.py`**: pure encode/decode
  for sprint.md Decision 1's `[1-byte type][4-byte big-endian
  length][payload]` frame — `FRAME_DATA`/`FRAME_BREAK`/`FRAME_SET_DTR`/
  `FRAME_SET_RTS`/`FRAME_CLOSE`, `encode_frame`, `read_frame`, and
  `FrameError`. Kept independent of `remote_api.py` (no socket or
  threading knowledge) so ticket 011's `registry.remote_client` can
  import the same module for its own frame handling instead of a second,
  drifting copy of this five-line spec — not required by this ticket's
  own scope, but free given how small the pure logic is.
  `read_frame(read_exact)` takes a `read_exact(n) -> bytes` callable
  (exactly `n` bytes, or fewer only at EOF) rather than a socket
  directly, which is what makes the boundary/fuzz tests
  (`tests/registry/test_stream_frame.py`) exercise the real production
  decode logic against scripted byte strings, no socket needed.
  `MAX_FRAME_PAYLOAD = 1 MiB` is the oversized-length ceiling the
  acceptance criterion calls for — checked from the header alone, before
  any attempt to read that many payload bytes.
- **`RemoteAPIServer._dispatch_line`** now returns
  `DeviceRecord | None` instead of `None` unconditionally: non-`None`
  (only ever returned for a successfully-accepted `stream`) is
  `_handle_connection`'s signal to stop reading JSON lines from `rfile`
  and hand the connection to the new `_handle_stream` method instead —
  the connection leaves JSON framing for the rest of its life, per the
  ticket's own Description, with no path back into the `for raw_line in
  rfile:` loop on any exit (`CLOSE`, malformed frame, or plain
  disconnect all fall through to the same `finally` teardown).
  `_op_stream_precheck` is the JSON-op half (resolve, confirm *this
  connection's own* `HolderRef` holds a `serial`-kind lock via
  `status.holder != holder` — the same full-equality check
  `_op_mark_flashed` already makes, stronger than `api.py`'s own
  pid-only `_op_flash` check since a `HolderRef` carries more than a
  bare pid); it runs entirely under `self._lock` but never touches the
  port. `_handle_stream` (the binary half) opens the port and runs the
  frame loop entirely outside that lock — the ticket's "serial port I/O
  must never happen while holding the shared assembly lock" constraint.
- **Protocol synchronization contract, not a general re-buffering
  fix**: switching a `TextIOWrapper`-based JSON reader (`rfile`) to raw
  `conn.recv()` mid-connection is only safe if nothing the client sent
  before the `stream` ack is still sitting in `rfile`'s own internal
  decode buffer (Python's `TextIOWrapper` may read ahead of the line it
  returns). Rather than rewriting the JSON dispatch loop's buffering
  end-to-end (higher regression risk to the 33 already-passing
  `remote_api` tests, for a protocol-switch edge case that only bites a
  client that pipelines ahead of a response), this ticket documents the
  requirement explicitly (module docstring's own "Protocol
  synchronization note", and `docs/design/registry-api.md`'s "Stream
  sub-protocol" section): a client must always wait for a response
  before sending its next thing, true of every op this protocol already
  has, including `stream` itself. Flagged here for ticket 011's
  `registry.remote_client` implementer.
- **`BREAK`'s duration is fixed server-side**, not carried in the
  frame's payload — `RemoteAPIServer(break_duration_s=...)`, defaulting
  to `serial.connect.BREAK_DURATION` (the same constant local `mbserial
  --reset`'s Linux path already uses). The ticket's acceptance criterion
  only specifies that `BREAK` triggers `ser.send_break(duration)`, not
  that the client chooses `duration`; a fixed, already-tuned duration
  keeps the frame format simpler (`BREAK`'s payload is always empty) and
  matches the one duration this codebase already uses for "kick a board
  out of a stuck state" (see `serial.connect`'s own docstring on
  `BREAK_DURATION`).
- **`RemoteAPIServer` gained four new constructor parameters**:
  `serial_factory`, `stream_baud`, `stream_settle_s`, `break_duration_s`
  — test-only escape hatches mirroring `serial.connect.connect`'s own
  `serial_factory`/`settle_s` seam, all defaulting to real
  pyserial/`serial.connect`'s own constants in production.
- **Tests**: `tests/registry/test_stream_frame.py` (18 tests, pure
  encode/decode + boundary cases) and
  `tests/registry/remote_api/test_remote_stream.py` (13 tests, a real
  `RemoteAPIServer` over a real loopback TCP socket with
  `mbtools.testing.fakes.FakeSerial` injected via `serial_factory` —
  preconditions, a full open/write/read/BREAK/SET_DTR/SET_RTS/close
  session, a plain connection drop, and the malformed-frame boundary
  cases) — both basenames checked unique across `tests/` first.
  The ticket's own "Existing tests to run" names
  `tests/serial/connect/`, which doesn't exist as a path in this repo
  (local `serial.connect` is tested by
  `tests/serial/test_mbserial_connect.py`); ran `tests/serial/` instead,
  which is where that coverage actually lives.
- **Test run**: `uv run pytest tests/registry/remote_api/ tests/registry/api/ tests/serial/ tests/registry/test_stream_frame.py -q`
  → 101 passed, 1 skipped (the same pre-existing platform-gated
  `SO_PEERCRED` skip). `uv run pytest -q` (full suite) → 463 passed, 2
  skipped — no regressions anywhere in the tree (up from the pre-ticket
  432 passed, 2 skipped).
- **Deviations from the ticket**: none in substance. The ticket's own
  Description mentions `stream` composing with `registry.remote_client`
  and `serial.remote_connect` (tickets 011/013) — neither exists yet;
  this ticket only builds the server side those will call.
