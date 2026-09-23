---
id: '007'
title: 'registry.remote_api: framed binary stream sub-protocol (data + BREAK/DTR/RTS
  control)'
status: open
use-cases: [SUC-004]
depends-on: ['006']
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

- [ ] Frame format implemented exactly as sprint.md's Decision 1
      specifies: `[1-byte type][4-byte big-endian length][payload]`,
      types `DATA=0x01`, `BREAK=0x02`, `SET_DTR=0x03`, `SET_RTS=0x04`,
      `CLOSE=0x05`. Documented in `docs/design/registry-api.md`'s new
      remote-protocol section (ticket 006 started it; this ticket adds
      the stream sub-protocol's own subsection).
- [ ] `stream` requires the connection to already hold a `serial`-kind
      lock on `uid` (same connection's own `HolderRef` — checked the same
      way `_op_flash`'s existing precondition check works in `api.py`);
      `not_locked` if not.
- [ ] On entering stream mode, the server opens the local port with
      DTR/RTS held low (no reboot) — same
      `serial.connect._open_no_reboot` behavior, reused rather than
      reimplemented (the implementer should factor the no-reboot-open
      helper so both `serial.connect` and this module call the same
      code, not duplicate it).
- [ ] A `DATA` frame from the client is written to the port; anything
      read from the port is sent back to the client as `DATA` frames
      (a background reader thread on the server side, mirroring
      `serial.connect.interact`'s own reader-thread pattern).
- [ ] A `BREAK` frame triggers `ser.send_break(duration)` on the server's
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
- [ ] `CLOSE` (or a plain connection drop) closes the local port and
      releases the `serial`-kind lock — same "always release on session
      end" guarantee as `serial.connect.Session.close`.
- [ ] Frame parsing is fuzz/boundary tested: a truncated frame, a
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
