---
id: '004'
title: Relay channel adapters
status: in-progress
use-cases:
- SUC-001
depends-on:
- '003'
github-issue: ''
issue: mbrelay-relay-protocol-client-over-mbregistry.md
completes_issue: true
---
<!-- CLASI: Before changing code or making plans, review the SE process in CLAUDE.md -->

# Relay channel adapters

## Description

Write the two adapters that let ticket 003's `relay.protocol` (which
knows nothing about `mbregistry`) run against a registry-obtained
device connection, local or remote. This is the only new code the
sprint needs for that portability (architecture Decision 3) — no
new wire protocol, since remote reset reuses sprint 003's existing
`RemoteStream.send_break()`/`set_dtr()`/`set_rts()` as-is (Decision 4).

**Approach**
- `LocalRelayChannel(ByteChannel)`: opens the local serial port the same
  way `serial.connect` does (direct pyserial object after the caller has
  already taken the `relay` lock), implements `send_break()` via
  `ser.send_break()` directly, `write_nowait`/`start_reading`/
  `stop_reading`/`drain`/`pending_bytes`/`set_watermarks` over the local
  fd/pyserial object.
- `RemoteRelayChannel(ByteChannel)`: wraps a
  `registry.remote_client.RemoteStream` (obtained via
  `RemoteRegistryClient.open_stream(uid)`, after the caller has taken a
  remote `relay` lock), implements `send_break()`/nothing-else-needed by
  calling the existing `RemoteStream.send_break()`, `write`/`read` over
  the stream's existing `write`/`read`.
- Both classes satisfy exactly the `ByteChannel` Protocol ticket 003
  ported — no changes needed to `relay.protocol` to consume either one.

**Files to create/modify**
- `src/mbtools/relay/channel.py` (new — `LocalRelayChannel`,
  `RemoteRelayChannel`).
- `tests/relay/test_channel.py` (new).

**Documentation updates**: none required by this ticket alone.

## Acceptance Criteria

- [x] `LocalRelayChannel` satisfies the `ByteChannel` Protocol (verified
      by running ticket 003's `RelayControl.reset_and_normalize()`
      against it with a fake serial backend).
- [x] `RemoteRelayChannel` satisfies the same Protocol, backed by a fake
      `RemoteStream`.
- [x] `RemoteRelayChannel.send_break()` calls through to
      `RemoteStream.send_break()` with no new wire protocol — confirmed
      by asserting on the fake `RemoteStream`'s call, not just that no
      exception is raised.
- [x] Neither adapter modifies or subclasses `serial.connect`'s or
      `serial.remote_connect`'s existing classes (parallel, not shared,
      per architecture "Impact on Existing Components").

## Testing

- **Existing tests to run**: `uv run pytest
  tests/registry/test_remote_client.py` (confirm `RemoteStream`'s
  `send_break`/`set_dtr`/`set_rts` are unchanged — this ticket only
  consumes them).
- **New tests to write**: `tests/relay/test_channel.py` — fake serial
  backend for `LocalRelayChannel`, fake `RemoteStream` for
  `RemoteRelayChannel`, both exercised through ticket 003's
  `RelayControl` to confirm end-to-end compatibility.
- **Verification command**: `uv run pytest tests/relay/test_channel.py
  tests/relay/test_protocol.py`

## Implementation Notes

Interface decisions later tickets (005 mbrelay CLI, 006/007 console-compat)
depend on:

- **`open()` never resets; reset is always `send_break()`.** Both
  adapters' `open()` only establishes/confirms the transport (local:
  `mbtools.serial.connect.open_no_reboot`, DTR/RTS held low; remote: a
  no-op, since `registry.remote_api._handle_stream` already opened the
  owning host's port the same no-reboot way before handing back the
  `RemoteStream`). Neither ever toggles DTR to reset the board —
  `RelayControl.hello`'s own BREAK-fallback path is the only reset
  mechanism either adapter implements, matching the sprint's Decision 4
  (no new wire protocol) and the measured Linux behavior (reopen does not
  reset a DAPLink target) documented in `serial.connect`'s own module
  docstring.
- **`RemoteRelayChannel` is single-use.** Its constructor takes an
  already-`open_stream()`-obtained `RemoteStream` (the caller's job:
  `RemoteRegistryClient.lock(uid, "relay")` then `.open_stream(uid)`, on
  one connection — a new `"relay"` lock kind, not `"serial"`). Once
  `close()` runs (directly, or via `RelayControl.reset_and_normalize`'s
  own `finally`), the underlying stream is torn down for good; a later
  ticket wanting another cycle constructs a fresh `RemoteRelayChannel`
  around a freshly obtained stream rather than reusing this instance.
  `LocalRelayChannel`, by contrast, is reusable — its `open()`/`close()`
  can cycle repeatedly, each `open()` calling `open_no_reboot` again.
- **`write_nowait`/`drain`/`pending_bytes`/`set_watermarks` are
  synchronous, not queued.** Both adapters write directly to their
  transport inside `write_nowait` (blocking, not buffered) rather than
  porting the legacy asyncio `SerialChannel`'s non-blocking write queue —
  there is no event loop here for a write to block, and every command
  `relay.protocol` sends is a short line with generous timeouts already
  built around it. Consequently `pending_bytes` is always `0`, `drain()`
  is a no-op, and `set_watermarks`' callbacks are recorded but never
  fire. A later ticket relying on backpressure/high-water signaling would
  need to add real queuing to whichever adapter it needs it on — neither
  currently provides it.
- **Each adapter owns one background daemon thread for reading**, spawned
  by `start_reading`/joined by `stop_reading` (also called from `close`).
  `LocalRelayChannel`'s thread blocks in `ser.read(max(1, ser.
  in_waiting))` (same pattern as `serial.connect.interact`'s own pump —
  it unblocks on the port's own configured read timeout, no explicit
  unstick needed). `RemoteRelayChannel`'s thread blocks in `RemoteStream.
  read()`, which has no per-call timeout of its own, so `stop_reading`
  unblocks it by closing the stream (best-effort, idempotent) before
  joining — the same move `serial.remote_connect.RemoteSession.close`
  already makes for the identical underlying blocking call.
- **Composition, not inheritance**, per the ticket's own acceptance
  criterion: neither adapter subclasses `serial.connect.Session` or
  `serial.remote_connect.RemoteSession` — both of those are
  `read()`/`write()`-shaped wrappers for an interactive console; these
  two adapters are `ByteChannel`-shaped (`start_reading(on_data,
  on_error)` push callbacks), a different interface entirely.
