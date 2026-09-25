---
id: '004'
title: stream op on the local socket
status: done
use-cases:
- SUC-004
- SUC-005
depends-on:
- '002'
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

- [x] `{"op": "stream", "uid": "..."}` on the local socket, after a
      prior `{"op": "lock", "uid": "...", "kind": "serial"}` on the same
      connection, switches that connection into the framed binary
      sub-protocol (`stream_frame.py`) — same framing, same precondition
      checks (lock already held by this connection, kind, uid exists) as
      the remote port's `stream`.
- [x] `DATA`/`BREAK`/`SET_DTR`/`SET_RTS`/`CLOSE` frames behave
      identically over the local socket to the remote port, for the same
      device.
- [x] `remote_api.RemoteAPIServer`'s existing `stream` tests all still
      pass unmodified against the relocated, inherited implementation
      (proving no behavior change from the move).
- [x] `mbregistry unlock --force` (ticket 003) against a board with an
      active *local-socket* stream session closes that session and its
      holder observes EOF — the cross-ticket interaction sprint.md's
      Step 3 predicts, verified here rather than assumed.
- [x] `docs/design/registry-api.md`'s stream section states it applies
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

## Implementation Notes

- `_op_stream_precheck`/`_handle_stream` moved verbatim (mechanics
  unchanged) from `remote_api.RemoteAPIServer` into
  `_api_base.BaseAPIServer`. `api.py` dispatches `{"op": "stream"}` into
  the inherited method exactly as `remote_api.py` already did;
  `remote_api.py`'s own dispatch line (`self._op_stream_precheck(...)`)
  needed no change at all, since it already called through `self.`.
- The `_dispatch_line`/`_handle_connection` tri-state handoff
  (`None`/`_WATCH` sentinel/a `DeviceRecord`) that `remote_api.py`
  already used for `stream` vs. `watch` is now shared: the `_WATCH`
  sentinel moved from a `remote_api.py` module-level object into
  `_api_base.py`, and `api.py`'s own `_dispatch_line` (previously a
  plain `bool` return for `watch` only) was widened to the same
  three-way return, mirroring `remote_api.py`'s shape exactly.
- **Circular import, not anticipated by the ticket description**:
  `_api_base.py` cannot import `mbtools.serial.connect` at module scope
  the way `remote_api.py` always could. `serial.connect` imports
  `registry.client`, which imports `registry.api` (for
  `DEFAULT_SOCKET_PATH`) — and `registry.api` imports `_api_base` for
  `BaseAPIServer`. A module-level `from mbtools.serial.connect import
  ...` in `_api_base.py` completes that cycle the moment anything
  imports `api.py` (`ImportError: cannot import name 'BaseAPIServer'
  from partially initialized module`). Fixed by importing
  `ConnectError`/`open_no_reboot` lazily inside `_handle_stream` itself
  (a function-local import, mirroring `registry.identity`'s existing
  `from mbtools.registry import claims` precedent for the same kind of
  cycle), and by not giving `_stream_baud`/`_stream_settle`/
  `_break_duration` class-level numeric defaults in `_api_base.py`
  (mirrors `_eventbus`'s existing "no default, every concrete subclass
  sets it" convention) — `api.py`'s own new `RegistryAPIServer.__init__`
  parameters (`serial_factory`/`stream_baud`/`stream_settle_s`/
  `break_duration_s`) resolve `serial.connect`'s real
  `BAUD_RATE`/`OPEN_SETTLE`/`BREAK_DURATION` the same lazy way, inside
  `__init__` itself, since `api.py` sits on the same cycle and can't
  import them at its own module scope either. `remote_api.py`'s
  pre-existing module-level import of those three constants was left
  untouched (it was never on the cycle, since nothing imports
  `remote_api.py` from within `registry.client`'s own import chain).
- Handoff from ticket 003 verified, not just assumed:
  `test_force_unlock_closes_a_live_stream_session_and_the_holder_sees_eof`
  (new, `tests/registry/api/test_api_stream.py`) locks + streams on one
  connection, force-unlocks from a second, and asserts: the streaming
  holder's blocked frame read returns `None` (clean EOF, exactly like a
  plain connection drop), the lock is released, the fake port is
  closed, and the server keeps serving other connections afterward — no
  crash, no traceback, no hang. No production code changed for this;
  `api.py`'s own `_uid_connections` map already covers a connection
  regardless of whether it was later switched into stream mode, exactly
  as the ticket description predicted.
- New test file `tests/registry/api/test_api_stream.py` mirrors
  `tests/registry/remote_api/test_remote_stream.py` closely (same
  `FakeSerial`/`stream_frame` fixtures and assertions), adapted to
  `AF_UNIX` + `_Client`-style dispatch. One real difference from the
  remote-port tests: the local socket's holder identity is the real
  kernel-verified peer pid (`SO_PEERCRED`/`LOCAL_PEERPID`), which is the
  *same* pid for every connection one test process opens — unlike the
  remote port, where each TCP connection mints its own random session
  id regardless of which process opened it. Tests asserting "a
  different connection's lock blocks this one" needed an injected
  `peer_pid_fn` (`_sequential_peer_pid_fn`, mirroring `test_api.py`'s
  own identical helper) to get two distinct local `HolderRef`s; this
  bit one test
  (`test_stream_with_another_connections_serial_lock_is_not_locked`)
  during development before the fix.
- Ran `tests/registry/api/`, `tests/registry/remote_api/` (125 passed,
  1 skipped — the pre-existing Linux-only `SO_PEERCRED` test, skipped
  on this macOS dev machine), plus `tests/registry/api_windows/`,
  `tests/registry/cli`, `tests/registry/client`, `tests/relay` as a
  broader safety net for the `_api_base.py`/import-graph change (all
  green) — `WindowsPipeAPIServer` also extends `BaseAPIServer` but never
  dispatches `stream`, so it was unaffected; confirmed, not just
  assumed.

### For ticket 005 (hardware acceptance)

- Nothing about `watch` or `unlock --force` changed in this ticket
  beyond what tickets 001/003 already delivered; this ticket only adds
  `stream` on the local socket and proves (in tests, not yet on
  hardware) that `unlock --force` tears down a live local-socket stream
  session cleanly.
- Worth a real-hardware check: `mbserial`'s local (non-`stream`) path is
  unaffected (unchanged code), but if ticket 005 wants to exercise the
  *local-socket* `stream` op against a real board (as opposed to the
  remote port, already covered by earlier hardware passes), the op is
  `{"op": "lock", "uid": ..., "kind": "serial"}` then `{"op": "stream",
  "uid": ...}` over `mbregistry`'s own Unix socket -- no CLI wraps this
  directly yet (`docs/design/registry-api.md`'s "`stream`" section has
  the full wire protocol).
