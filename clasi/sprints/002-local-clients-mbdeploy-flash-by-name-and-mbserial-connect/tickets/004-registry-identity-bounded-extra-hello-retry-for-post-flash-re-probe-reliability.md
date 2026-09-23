---
id: '004'
title: 'registry.identity: bounded extra HELLO retry for post-flash re-probe reliability'
status: open
use-cases: [UC-001, UC-003]
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

- [ ] A board that answers within the first read window is **untouched**
      by this change — no second `HELLO` is sent, and probe timing for
      the already-passing case is unchanged (regression-safety this
      sprint's Architecture explicitly claims).
- [ ] A board that is silent through the first window, or whose first
      window captures an unparseable line, gets exactly one more
      `HELLO` and one more bounded read window before `probe()` gives up
      and returns the same "no-firmware"/malformed outcome it does
      today (this ticket makes the *chance* of success higher, it does
      not change what a still-failing probe reports).
- [ ] The retry fires at most once per `probe()` call — never a loop,
      never open-ended.
- [ ] `probe()`'s public signature and return shape are unchanged; every
      existing caller (`daemon`'s attach pipeline and its flash-pending
      re-probe) needs no changes.

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
