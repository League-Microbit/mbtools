---
id: '004'
title: 'registry.identity: bounded extra HELLO retry for post-flash re-probe reliability'
status: done
use-cases:
- UC-001
- UC-003
depends-on: []
github-issue: ''
issue: mbdeploy-flash-by-name-via-mbregistry.md
completes_issue: true
---
<!-- CLASI: Before changing code or making plans, review the SE process in CLAUDE.md -->

# registry.identity: bounded extra HELLO retry for post-flash re-probe reliability

## Description

Extend `mbtools.registry.identity.probe()` with one bounded extra
retry: if the first read window ends with nothing usable (silence, or a
line that doesn't parse against either announcement dialect), send
`HELLO` once more and read for one more bounded window before giving up.
This is the same "exactly one retry" house style already used elsewhere
in this project (`flash.py`'s transient-probe retry, its mass-erase-then-
retry) — applied here because `docs/acceptance/001-hardware.md`'s magni
finding showed a real, not-root-caused timing sensitivity: four
flash-triggered re-probe attempts on one board (nezha firmware) produced
two timeouts and one malformed-line capture, while a manual, longer,
second-HELLO attempt captured the announcement every time.

Per sprint.md's Design Rationale, this retry is **unconditional** — it
applies to every call to `probe()` (attach-time, UC-001, and post-flash,
UC-003), not scoped only to post-flash re-probes. Scoping it would
require threading extra context into `probe()` that the module's own
boundary ("the one place a port is opened") doesn't otherwise need, for
a fix that's equally plausible as a general timing-robustness
improvement.

This is independent of tickets 001-003 (touches only `identity.py`, no
socket/client code) and can be built in parallel with them.

**Important — this is a mitigation, not a confirmed root-cause fix.**
Sprint.md's Open Questions section flags this explicitly: ticket 010's
hardware acceptance pass must specifically re-exercise magni's board
with nezha firmware and record whether this closes the gap or needs to
be escalated as its own follow-up issue.

## Acceptance Criteria

- [x] A board that answers within the first read window is **untouched**
      by this change — no second `HELLO` is sent, and probe timing for
      the already-passing case is unchanged (regression-safety this
      sprint's Architecture explicitly claims).
- [x] A board that is silent through the first window, or whose first
      window captures an unparseable line, gets exactly one more
      `HELLO` and one more bounded read window before `probe()` gives up
      and returns the same "no-firmware"/malformed outcome it does
      today (this ticket makes the *chance* of success higher, it does
      not change what a still-failing probe reports).
- [x] The retry fires at most once per `probe()` call — never a loop,
      never open-ended.
- [x] `probe()`'s public signature and return shape are unchanged; every
      existing caller (`daemon`'s attach pipeline and its flash-pending
      re-probe) needs no changes.

## Implementation Notes

- `identity.probe()`'s single read-and-parse block became a
  `for attempt in range(2):` loop: write `HELLO`, read one bounded
  window, return immediately on a parse match. A first-window success
  returns from inside the loop, so a promptly-answering board never
  reaches the second iteration and never sees a second `HELLO` --
  `written == [b"HELLO\n"]` in that case, same as before this ticket.
  `malformed_raw` is tracked across both attempts (not reset between
  them), so a malformed line in window 1 followed by silence in window
  2 still returns the malformed outcome rather than losing it.
- `mbtools.testing.fakes.FakeSerial` gained an `announcement_after_writes`
  parameter (default `0`, preserving every existing direct-`FakeSerial`
  test's "answers on the very first `readline()`, no `write()` required"
  contract) so a test can script a board that stays silent until a
  *second* `write()` (`HELLO`) has actually happened -- needed to prove
  the retry sends a real second `HELLO` rather than just re-reading the
  same window twice.
- New tests: `tests/registry/identity/test_identity.py` (retry-then-
  success, still-silent-after-both-windows, malformed-first-window-then-
  silent-second-window preserves the malformed outcome, first-window-
  success sends only one `HELLO`) and `tests/testing/test_fakes.py`
  (direct coverage of the new `announcement_after_writes` gate and its
  default).
- This is a mitigation per sprint.md's own framing, not a confirmed fix
  for magni's finding -- ticket 010's hardware acceptance pass is where
  that gets confirmed or escalated.

### Additional team-lead-scoped work: daemon/API shutdown-ordering fix

Also fixed, per explicit team-lead dispatch scope (not part of this
ticket's own acceptance criteria): a flaky `IndexError` in
`store._row_to_record`, observed intermittently in
`tests/registry/cli/test_cli_run.py::test_run_then_list_smoke`, traced to
a real shutdown-ordering bug in `RegistryAPIServer.stop()`
(`src/mbtools/registry/api.py`), not a test-only artifact.

**Root cause**: `stop()` joined `_accept_thread` and `_sweep_thread` but
never the per-connection handler threads `_accept_loop` spawns into
`_conn_threads`. Every handler thread calls back into `store`/`locks`
(`_op_list`, `_op_find`, ...); every production and test caller closes
`store` immediately after `stop()` returns, so a handler thread that
hadn't yet finished a request at the moment `stop()` returned could keep
executing against the store concurrently with (or just after) its sqlite
connection being closed -- the observed race, which surfaced as
`_row_to_record` receiving a malformed row rather than a cleaner
"connection closed" exception.

**Fix**: `stop()` now also joins every thread in `_conn_threads`
(`timeout=2.0`, same bound already used for the accept/sweep threads)
before removing the socket file and returning. The existing "already-open
connections are not forcibly closed, they wind down on their own"
behavior is unchanged -- a client deliberately keeping a connection open
across `stop()` still isn't force-closed, its `join(timeout=2.0)` simply
times out without blocking shutdown, same as today. What changed is that
a connection which *has* seen (or promptly sees) EOF is now actually
waited for instead of left to race the caller's next line of code.

**Regression test**: `test_stop_joins_connection_handler_threads_before_returning`
in `tests/registry/api/test_api.py` -- opens a real client connection,
makes one request, closes the client side, calls `stop()`, and asserts
every thread in `srv._conn_threads` is no longer alive.

**Test cleanup audit**: `tests/registry/cli/test_cli_run.py`'s two
background-daemon tests already joined their own `daemon.run()`/
`run_once()`-loop threads before calling `api.stop()`/`store.close()` --
no change needed there. No other test file runs a `Daemon` or
`RegistryAPIServer` on a background thread without already joining it;
the gap was entirely inside `RegistryAPIServer.stop()` itself, not in
any individual test's cleanup.

**Verification**: full suite (`uv run pytest -q`) run 5 times in a row
after the fix, plus 3 additional runs after simplifying it (dropped an
unnecessary alive-thread filter on `_conn_threads` that would have hidden
the evidence a regression test needed) -- consistently green every time,
no flakes observed (215 passed, 1 skipped, each run).

## Testing

- **Existing tests to run**: `uv run pytest tests/registry/identity/`
  (sprint 001's full identity/probe test suite — must pass unmodified,
  proving a first-window success is untouched).
- **New tests to write**: extend the existing `FakeSerial`-based probe
  tests (`mbtools.testing.fakes`) with a scripted "silence on the first
  window, a real announcement after the second HELLO" scenario, asserting
  the second `HELLO` was actually sent and the announcement was parsed;
  a "still silent after both windows" scenario confirming the existing
  no-firmware/malformed outcome is preserved, not converted into an
  infinite retry.
- **Verification command**: `uv run pytest tests/registry/`
