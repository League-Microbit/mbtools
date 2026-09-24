---
id: '004'
title: Relay channel adapters
status: open
use-cases: [SUC-001]
depends-on: ['003']
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

- [ ] `LocalRelayChannel` satisfies the `ByteChannel` Protocol (verified
      by running ticket 003's `RelayControl.reset_and_normalize()`
      against it with a fake serial backend).
- [ ] `RemoteRelayChannel` satisfies the same Protocol, backed by a fake
      `RemoteStream`.
- [ ] `RemoteRelayChannel.send_break()` calls through to
      `RemoteStream.send_break()` with no new wire protocol — confirmed
      by asserting on the fake `RemoteStream`'s call, not just that no
      exception is raised.
- [ ] Neither adapter modifies or subclasses `serial.connect`'s or
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
