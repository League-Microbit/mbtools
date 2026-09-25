---
id: '005'
title: 'Hardware acceptance: watch and unlock --force'
status: open
use-cases: [SUC-001, SUC-002, SUC-003, SUC-004]
depends-on: ['001', '002', '003', '004']
github-issue: ''
issue: mbregistry-api-for-robot-console-watch-lock-label-unlock-force-local-stream.md
completes_issue: true
---
<!-- CLASI: Before changing code or making plans, review the SE process in CLAUDE.md -->

# Hardware acceptance: watch and unlock --force

## Description

Manual hardware verification of this sprint's two riskiest interactions
— a live `watch` client observing real USB attach/detach, and
`unlock --force` actually reaching a *different, live* client's
connection and closing it — on real boards, following this project's
existing hardware-acceptance convention (`docs/acceptance/00N-hardware.md`,
one new `docs/acceptance/008-hardware.md`).

**Hosts**: any two of `meili`/`loki`/`hodr`/`magni`/`braeburn` (per
CLAUDE.md — passwordless `sudo`, one dedicated micro:bit each). **Do
not** use `torture`'s relays for this (CLAUDE.md's standing rule: shared,
scarce hardware, not a routine test target).

Scenario 1 — **watch observing a real attach/detach**: on one host,
start `mbregistry run` (or use the running service), open a `watch`
connection (a small script or `nc`/`socat` against the local socket is
sufficient — no new tooling required), physically unplug and replug that
host's dedicated micro:bit, and confirm `attach`/`detach` JSON lines
arrive in the expected order with no ZeroMQ client involved.

Scenario 2 — **`unlock --force` against a live holder**: from one
session, `lock` the board (`serial` kind) and leave that connection
open (e.g. a `stream` session, once ticket 004 has landed, or a plain
held `lock` connection); from a second, independent session on the same
host, run `mbregistry unlock --force <uid>`; confirm the first
session's connection observes EOF/a connection error essentially
immediately, and that `mbregistry list` shows the board unlocked
afterward.

If either scenario surfaces a real bug (not a known, already-documented
quirk from CLAUDE.md's hardware-quirks list), fix it as part of this
ticket, the same way prior sprints' hardware tickets did (e.g. sprint
005 ticket 011, sprint 007's own hardware pass).

## Acceptance Criteria

- [ ] A `watch` client on a real host observes `attach` immediately
      after a physical plug-in and `detach` immediately after unplug,
      for that host's dedicated board.
- [ ] `mbregistry unlock --force <uid>` against a lock held by a live,
      separate connection on the same host releases the lock and that
      connection observes EOF/an error; `mbregistry list` confirms the
      board is unlocked afterward.
- [ ] Neither scenario touches `torture`'s relays.
- [ ] `docs/acceptance/008-hardware.md` records both scenarios, the
      hosts/boards used, and the outcome (including any quirk found and
      whether it needed a code fix, per this project's existing
      acceptance-doc convention).
- [ ] Any real bug found is fixed and covered by a regression test in
      the automated suite, not left as a manual-only finding.

## Testing

- **Existing tests to run**: `uv run pytest` (full suite) before
  starting the hardware pass, to confirm the branch is otherwise green.
- **New tests to write**: none required if hardware behaves as
  designed; a regression test in `tests/registry/` for any real bug
  this pass finds.
- **Verification command**: `uv run pytest`
