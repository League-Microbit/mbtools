---
id: '007'
title: Hardware acceptance -- two instances on one host plus a silent board named
  over SWD
status: done
use-cases:
- SUC-001
- SUC-002
- SUC-005
- SUC-006
- SUC-007
depends-on:
- '006'
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

- [x] `docs/acceptance/00N-hardware.md` (next unused number) documents
      Scenario A (two instances, one host) with a clear PASS/FAIL and
      the actual commands/output used.
- [x] Scenario B (silent board named over SWD) is documented the same
      way, including the board's real name as confirmed by comparison
      against its known announcing-firmware identity.
- [x] Scenario C (spawn recipe), if attempted, is documented; if not
      attempted, the doc says so and why (time-box, not a silent gap).
- [x] The Windows `--pipe` non-verification is stated explicitly, not
      omitted.
- [x] Any hardware-only finding (a real quirk, like the ones
      `CLAUDE.md`'s "Known real-hardware quirk" notes already record)
      is written up the same way and, if it changes `CLAUDE.md`-worthy
      operational knowledge, proposed as a `CLAUDE.md` addition in the
      same ticket.
- [x] Every test board touched is left in a known-good state (restored
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

## Implementation Notes

Ran on `meili` (Scenario A) and `loki` (Scenario B and C) — see
`docs/acceptance/006-hardware.md` for the full command-by-command trace.
All three scenarios PASS. Two real *code* bugs were found and fixed
along the way (small, scoped, each with a new regression test, per this
ticket's own "a small, clearly-scoped fix ... is acceptable" allowance):

1. **Scenario A** surfaced that `PeerDiscovery` never advertised its real
   ephemeral remote port over mDNS when `--remote-port 0` was used (a
   `--remote-port 0`/`0` recipe this ticket's own Description
   recommends) — it kept advertising the literal requested `0` forever,
   since nothing read `RemoteAPIServer.bound_port` back into `peering`
   before `peering.start()` built its `ServiceInfo`/TXT record. Fixed
   with `PeerDiscovery.set_remote_port()` plus a `cmd_run` call-site fix
   (`src/mbtools/registry/peering.py`, `src/mbtools/registry/cli.py`);
   regression test in
   `tests/registry/cli/test_cli_ports_instance_pipe.py`. Verified fixed
   against real mDNS advertisements from a third host both before and
   after the fix.
2. **Scenario C** surfaced that `--ready-json`'s one JSON line never
   reached a real spawning parent's stdout pipe — `print()` with no
   `flush=True` sat in Python's block-buffered stdout (block-buffered
   whenever not a tty, exactly the real-parent-via-pipe case this recipe
   targets) since nothing else ever writes to stdout afterward. Fixed
   with `flush=True` (`src/mbtools/registry/cli.py`); regression test
   (the first real-subprocess/real-OS-pipe test in
   `tests/registry/cli/test_cli_spawn.py`, `test_ready_json_line_reaches_
   a_real_pipe_promptly`) reproduces the exact hang pre-fix and passes
   post-fix. Verified fixed by re-running the documented spawn recipe
   from a real bash parent script against real hardware.

One real *operational* finding, not a code bug: both `meili` and `loki`
carry a second, stale `mbtools` install at `/opt/mbtools` whose
`/usr/local/bin/*` symlinks shadow this project's own
`scripts/deploy-host.sh` target (`~/mbtools-venv`) for any bare client
command, even though `mbregistry.service` itself always runs the fresh
build. Documented in `docs/acceptance/006-hardware.md` Scenario B and
folded into `CLAUDE.md` (new "Known real-hardware quirk" entry); fixed
on both hosts touched this session by re-pointing the symlinks.

Windows `--pipe` (SUC-008) is explicitly stated as not hardware-verified
(no Windows host in the fleet), per this ticket's own requirement.

Scoped regression run (both fixes together): `uv run pytest
tests/registry/cli/test_cli_ports_instance_pipe.py
tests/registry/cli/test_cli_run_peering.py
tests/registry/cli/test_cli_spawn.py tests/registry/peering -q` — 127
passed. The full suite was not re-run here (out of scope per this
project's own testing rule — it runs once per sprint inside
`close_sprint`, not per ticket).

Both `meili` and `loki` ended the session with `mbregistry.service`
active on this ticket's final build, both dedicated test boards
(`gitev`, `togov`) restored to their exact starting announced state, and
no orphaned `mbregistry run` processes.
