---
id: '004'
title: stream op on the local socket
status: open
use-cases: [SUC-004, SUC-005]
depends-on: ['002']
github-issue: ''
issue: mbregistry-api-for-robot-console-watch-lock-label-unlock-force-local-stream.md
completes_issue: false
---
<!-- CLASI: Before changing code or making plans, review the SE process in CLAUDE.md -->

# stream op on the local socket

## Description

Relocate `remote_api.RemoteAPIServer`'s `_op_stream_precheck`/
`_handle_stream` into `_api_base.py` (mechanics unchanged), mirroring
exactly how ticket 006 moved `list`/`find`/`lock`/`unlock`/
`mark_flashed` into the shared mixin. The sub-protocol
(`stream_frame.py`) and the serial-open call
(`serial.connect.open_no_reboot`) are already transport-agnostic; only
the server loop that drives them lived solely in `remote_api.py`.
`api.py` (local socket) dispatches `stream` into the now-shared method
exactly as `remote_api.py` already does; `remote_api.py`'s own copy is
removed, with no behavior change for an existing remote `stream`
caller.

`api.py`'s per-uid `{uid: connection}` map (added in ticket 003) needs
no change here: it is populated at `lock` time and covers a connection
regardless of whether it is later switched into `stream` mode, so
`unlock --force` closing a local-socket stream session should already
work once this ticket lands — verify this explicitly (see Acceptance
Criteria) rather than assuming it from the architecture alone.

Update `docs/design/registry-api.md`'s stream sub-protocol section to
state it applies to both transports, not just the remote port.

## Acceptance Criteria

- [ ] `{"op": "stream", "uid": "..."}` on the local socket, after a
      prior `{"op": "lock", "uid": "...", "kind": "serial"}` on the same
      connection, switches that connection into the framed binary
      sub-protocol (`stream_frame.py`) — same framing, same precondition
      checks (lock already held by this connection, kind, uid exists) as
      the remote port's `stream`.
- [ ] `DATA`/`BREAK`/`SET_DTR`/`SET_RTS`/`CLOSE` frames behave
      identically over the local socket to the remote port, for the same
      device.
- [ ] `remote_api.RemoteAPIServer`'s existing `stream` tests all still
      pass unmodified against the relocated, inherited implementation
      (proving no behavior change from the move).
- [ ] `mbregistry unlock --force` (ticket 003) against a board with an
      active *local-socket* stream session closes that session and its
      holder observes EOF — the cross-ticket interaction sprint.md's
      Step 3 predicts, verified here rather than assumed.
- [ ] `docs/design/registry-api.md`'s stream section states it applies
      to both the local socket and the remote port.

## Testing

- **Existing tests to run**: `tests/registry/remote_api/` (must still
  pass unmodified against the relocated code), `tests/registry/api/`.
- **New tests to write**: a local-socket `stream` test mirroring the
  existing remote-port `stream` tests verbatim (reusing
  `stream_frame.py`'s framing helpers and the `FakeSerial` seam from
  ticket 007), covering `DATA`/`BREAK`/`SET_DTR`/`SET_RTS`/`CLOSE`; a
  test combining this ticket with ticket 003 (`unlock --force` against
  a live local-socket stream).
- **Verification command**: `uv run pytest tests/registry/`
