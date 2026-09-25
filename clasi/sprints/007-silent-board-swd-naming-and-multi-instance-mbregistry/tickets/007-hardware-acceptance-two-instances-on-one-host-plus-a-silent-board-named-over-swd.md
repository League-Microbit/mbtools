---
id: '007'
title: Hardware acceptance -- two instances on one host plus a silent board named
  over SWD
status: open
use-cases: [SUC-001, SUC-002, SUC-005, SUC-006, SUC-007]
depends-on: ['006']
github-issue: ''
issue:
- name-silent-boards-over-swd-and-keep-list-table-clean.md
- robot-console-on-mbregistry-multi-instance-and-spawn-support.md
completes_issue: true
---
<!-- CLASI: Before changing code or making plans, review the SE process in CLAUDE.md -->

# Hardware acceptance: two instances on one host plus a silent board named over SWD

## Description

Manual hardware verification of this sprint's two headline claims,
against real boards on real hosts — not part of the automated suite.
Follows the pattern of `docs/acceptance/001-hardware.md` through
`005-hardware.md`: write up the run as
`docs/acceptance/007-hardware.md` (or the next unused number — check
what's already on disk at implementation time), separating
hardware-verified claims from anything only exercised in CI/mocks, the
same way `005-hardware.md`'s own header does.

Use hosts from `meili`/`loki`/`hodr`/`magni`/`braeburn`, per this
project's `CLAUDE.md` hardware-test-host table and its "pick a spare
board" rule. Do **not** use `torture`'s relays (CLAUDE.md's standing
exclusion, reiterated in sprint.md's Scope). Each of the five allowed
hosts has exactly one micro:bit dedicated to `mbtools` testing — plan
around that (e.g. a silent-board test needs a board temporarily blanked
or running non-announcing firmware, which is fine on the dedicated test
board, but restore it to a known-good state afterward, matching prior
acceptance tickets' own courtesy).

**Scenario A — two instances, one host.** On one spare host, stop the
system `mbregistry.service` if running (or use `--user` scope, whichever
is cleaner given sprint 006's service-install work, if it has merged by
the time this ticket runs), then start two `mbregistry run` processes
by hand with distinct `--instance`, `--socket`, `--db`, and either
distinct explicit ports or `0` for every port. Confirm: both advertise
correctly over mDNS (`avahi-browse`/`dns-sd` from a third host, or from
the same host); the host's one dedicated micro:bit is claimed and listed
by exactly one of the two instances (unplug/replug and confirm the same
instance re-claims it, or the other one does if the first was killed);
`mbregistry list --socket <instance-A-socket>` and `--socket
<instance-B-socket>` show consistent, non-colliding output.

**Scenario B — silent board named over SWD.** On a spare host (can be
the same one), get the dedicated test board into a non-announcing state
— either genuinely blank (a deliberate mass erase, if you're comfortable
recovering it afterward) or flashed with firmware that doesn't announce
(anything without a `HELLO` handler). Confirm `mbregistry list` shows
the board's real five-letter name in `NAME` (cross-check it against the
name that board already shows when running normal, announcing firmware,
if known from a prior acceptance doc) and the correct didn't-announce
`STATE`/`FIRMWARE` text. Reflash the board back to known-good firmware
afterward and confirm the registry picks up the change on next probe.

**Scenario C (if time permits) — spawn recipe.** Exercise
`mbregistry run --ready-json --exit-with-parent --no-peering` from a
simple parent script (a shell script piping its own stdin through, or a
short Python subprocess wrapper) on one spare host, confirming the ready
line prints and the child exits when the parent's stdin closes.

**Explicit non-verification note**: Windows named-pipe (`--pipe`,
SUC-008) is **not** hardware-verified this sprint (no Windows host in
the fleet) — say so plainly in `docs/acceptance/007-hardware.md`,
mirroring `005-hardware.md`'s own "Scenario 5... not hardware-verified"
precedent for Windows claims, rather than silently omitting it.

## Acceptance Criteria

- [ ] `docs/acceptance/00N-hardware.md` (next unused number) documents
      Scenario A (two instances, one host) with a clear PASS/FAIL and
      the actual commands/output used.
- [ ] Scenario B (silent board named over SWD) is documented the same
      way, including the board's real name as confirmed by comparison
      against its known announcing-firmware identity.
- [ ] Scenario C (spawn recipe), if attempted, is documented; if not
      attempted, the doc says so and why (time-box, not a silent gap).
- [ ] The Windows `--pipe` non-verification is stated explicitly, not
      omitted.
- [ ] Any hardware-only finding (a real quirk, like the ones
      `CLAUDE.md`'s "Known real-hardware quirk" notes already record)
      is written up the same way and, if it changes `CLAUDE.md`-worthy
      operational knowledge, proposed as a `CLAUDE.md` addition in the
      same ticket.
- [ ] Every test board touched is left in a known-good state (restored
      firmware, no orphaned `mbregistry run` processes left running
      outside the normal service) by the end of this ticket.

## Implementation Plan

**Approach**: Run Scenario A and B on whichever two of
`meili`/`loki`/`hodr`/`magni`/`braeburn` are least likely to be in use by
another concurrent session (check for other active SSH sessions/sprint
work first, same courtesy prior acceptance tickets extended). Write the
acceptance doc as you go, not reconstructed afterward from memory,
matching `001-hardware.md` through `005-hardware.md`'s own level of
command-by-command detail.

**Files to create**:
- `docs/acceptance/00N-hardware.md` (next number after `005-hardware.md`
  — confirm nothing else claimed a number in between before choosing
  one).

**Files to modify** (if a real hardware finding warrants it):
- `CLAUDE.md` (hardware-test-host table / "Known real-hardware quirk"
  notes), only if this run surfaces something future sessions need to
  know, mirroring how sprint 003/005's hardware findings got folded back
  in.

**Testing plan**: This ticket *is* the (manual) test — no new automated
tests are added here. If any hardware finding reveals an automated-test
gap, file it as a note in the acceptance doc for a future ticket/sprint
rather than expanding this ticket's own scope.

**Documentation updates**: `docs/acceptance/00N-hardware.md` (new); a
possible `CLAUDE.md` update per above.
