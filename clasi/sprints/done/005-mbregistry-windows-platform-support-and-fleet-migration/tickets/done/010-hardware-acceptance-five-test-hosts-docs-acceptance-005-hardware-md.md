---
id: '010'
title: 'Hardware acceptance: five test hosts (docs/acceptance/005-hardware.md)'
status: done
use-cases:
- SUC-006
depends-on:
- '001'
- '007'
- 008
- '011'
github-issue: ''
issue: mbtools-fleet-deployment-tooling-and-migration-docs.md
completes_issue: true
---
<!-- CLASI: Before changing code or making plans, review the SE process in CLAUDE.md -->

# Hardware acceptance: five test hosts (docs/acceptance/005-hardware.md)

## Description

Final sprint ticket: validate this sprint's hardware-verifiable claims
against the five test hosts (`meili`, `loki`, `hodr`, `magni`,
`braeburn`), and record results in `docs/acceptance/005-hardware.md`,
following the format `docs/acceptance/001-hardware.md` through
`004-hardware.md` already established. Per team-lead scoping, this
ticket must explicitly separate hardware-verified claims from
fakes/CI-only claims — every Windows-specific claim from tickets 002-006
falls in the latter category, since no Windows hardware exists for this
project.

**Scenarios to run**
1. **Re-probe fix, deliberately constructed** (ticket 001): reproduce
   the actual bug scenario from `docs/acceptance/004-hardware.md` ticket
   011 on purpose, not by accident — tune a relay into the data plane
   (`!GO`) against another board's channel/group on one of the test
   hosts, then trigger a re-probe (a `dwc_otg`-style reattach flap is not
   reliably reproducible on demand; a deliberate USB
   unplug/replug or a forced daemon restart mid-data-plane is an
   acceptable substitute — document exactly which trigger was used) and
   confirm the relay's registry row is **not** corrupted, unlike the
   sprint 004 incident. Directly query the physical board afterward
   (`mbserial <uid> --reset HELLO`) to confirm ground truth.
2. **Deployment tooling idempotency** (ticket 007): run the tooling
   against all five test hosts; on at least one, run it a second time
   against an already-healthy host and confirm no disruption (per ticket
   007's own acceptance criteria) — this ticket is where that "at least
   one host" from 007 becomes "all five."
3. **Full command smoke test**: `mbregistry list`, `mbdeploy deploy`,
   `mbserial`, `mbrelay connect`, `mbrelay names` each exercised at
   least once against real hardware, using a spare board with no
   announcing firmware (this project's standing rule — CLAUDE.md/sprint
   004 precedent — never a robot in active use).
4. **`braeburn` longer-uptime mDNS check, if feasible**: given the known,
   not-root-caused mDNS-degrades-after-hours-of-uptime quirk
   (CLAUDE.md, `docs/acceptance/004-hardware.md`), check `braeburn`'s
   mDNS discoverability after whatever uptime window this session
   permits, and record the observed uptime regardless of outcome (a
   negative or inconclusive result is still worth recording, per this
   project's "gather evidence rather than guessing, and say so when the
   root cause isn't nailed down" precedent — do not skip recording this
   just because it can't be made conclusive in one session).
5. **Windows claims — explicitly not run here.** List every
   Windows-specific claim from tickets 002-006 and mark each
   `not hardware-verified` / `verified against fakes and windows-latest
   CI only`, rather than omitting them from the document.

**Files to create/modify**
- `docs/acceptance/005-hardware.md` (new).

**Documentation updates**: this ticket *is* the documentation update;
if any finding here contradicts something stated in `CLAUDE.md`'s
"Hardware test targets" section (e.g. a new quirk, or a previously
recorded one that no longer reproduces), update that section too,
matching the precedent set by tickets 001/010/011 in prior sprints.

## Acceptance Criteria

- [x] `docs/acceptance/005-hardware.md` records, per scenario above:
      what was run, on which host(s), the exact commands/output, and a
      PASS/FAIL/inconclusive verdict.
- [x] The re-probe fix is validated by a *deliberately constructed*
      relay-in-data-plane-during-reprobe scenario, not just cited as
      "the unit test covers this" — real hardware evidence, mirroring
      how the original bug was found.
- [x] Deployment-tooling idempotency is confirmed on all five test
      hosts, with at least one explicit re-run-on-healthy-host check.
- [x] Every one of `mbregistry list`/`mbdeploy deploy`/`mbserial`/
      `mbrelay connect`/`mbrelay names` is exercised against real
      hardware at least once, using a spare/non-production board.
- [x] `braeburn`'s observed uptime at the time of the mDNS check is
      recorded, along with the outcome (discovered / not discovered /
      not attempted and why).
- [x] Every Windows-specific claim from tickets 002-006 is explicitly
      listed and marked not-hardware-verified — this document must not
      silently omit them or imply they were hardware-tested.
- [x] No robot in active use was disturbed during this ticket's hardware
      work (spare-board rule).
- [x] **Torture relay pool re-verified** (ticket 011): after ticket 011's
      local-ownership-wins fix is deployed, `torture`'s relay pool
      (`console_compat.relay_pool`) still offers its own attached relays
      and every host's `mbregistry list` still shows them as
      `host=torture` — confirming ticket 011's hardware check holds up
      alongside this ticket's own five-test-host deployment-tooling
      re-run, not just in isolation right after ticket 011 landed.

## Testing

- **Existing tests to run**: the full suite (`uv run pytest`) locally
  before starting hardware work, as a sanity baseline — this ticket's
  actual verification is hardware, not `pytest`.
- **New tests to write**: none in `tests/` — this ticket is a hardware
  acceptance pass, matching the shape of every prior sprint's own final
  hardware-acceptance ticket.
- **Verification command**: N/A in the `pytest` sense — verification is
  the hardware scenarios above, recorded in
  `docs/acceptance/005-hardware.md`.

## Implementation Notes

- This ticket depends on 001 (the fix being validated), 007 (the tooling
  being validated), 008 (so the acceptance document can reference
  the finished runbook by name/section, the same way prior
  acceptance docs cross-reference sprint.md), and 011 (the
  local-ownership-wins peer-sync fix — added after this ticket was first
  written, per the newly linked issue
  `peer-sync-overwrites-ownership-of-locally-attached-devices.md`; this
  ticket's `torture` relay-pool re-check exercises that fix on real
  hardware a second time, alongside this sprint's other hardware work,
  not just once in isolation).
- Follow `docs/acceptance/004-hardware.md`'s own honesty precedent: if
  something doesn't reproduce cleanly, or a result is ambiguous, say so
  plainly rather than smoothing it over — that document's own
  "New finding (not root-caused, flagged honestly)" section is the house
  style to match.

**Completed this session.** Full results in `docs/acceptance/005-hardware.md`.
Summary:

- **Re-probe fix (ticket 001), deliberately reproduced**: used `magni`'s
  dedicated relay board, tuned it onto `vitut`'s own derived
  channel/group, left it in the data plane, then forced a genuine
  reprobe via sysfs USB unbind/rebind on `magni` (a bare
  `systemctl restart` was tried first and confirmed, by reading
  `store.upsert_attached`'s own docstring and testing it directly, to be
  a no-op for an uid that never transitioned through `disconnected` — a
  real gap between the ticket's suggested substitute trigger and what
  the store's reattach detection actually requires, worth a future
  ticket's attention if `docs/acceptance/005-hardware.md`'s approach
  should become the standing recommendation instead). Registry row held
  correct (`RADIOBRIDGE/relay`) after reprobe; ground truth confirmed
  via `mbserial --reset HELLO` directly against the board.
- **Deployment-tooling idempotency**: all six hosts, twice each,
  including a final pass after this ticket's own code fix (below) to
  prove idempotency against the actual final tree state. `hodr`'s
  known no-default-route issue recurred (pre-existing infra, not
  `mbtools`) and was worked around with the documented offline-install
  path both times.
- **Real bug found and fixed**: `mbrelay connect`'s interactive session
  crashed (`AttributeError: 'NoneType' object has no attribute
  'select'`) against any non-tty-but-real-fd stdin (a redirected file,
  a pipe, `/dev/null`) — `src/mbtools/relay/cli.py`'s `_interactive`
  picked its `select`-based branch on `stdin_fd is not None` rather
  than `is_tty`, but only imports `select`/`termios`/`tty` in the
  `is_tty` branch. The existing test suite's autouse stdin fixture
  (`io.StringIO("")`, no `fileno()` at all) never exercised this gap.
  Fixed by gating on `is_tty`; new regression test
  `tests/relay/test_cli.py::test_interactive_session_survives_non_tty_real_fd_stdin`
  opens a real `os.devnull` file object to reproduce the exact
  "real fd, not a tty" condition. Verified against real hardware after
  redeploying to all six hosts.
- **Paper-cut finding, documented, not fixed**: `braeburn` (macOS) has
  no `/run` directory, so any client command there without an explicit
  `--socket` fails — `registry.paths.default_socket_path()` doesn't
  special-case macOS. Not fixed: doing so would resolve
  `docs/design/specification.md`'s own open question #7 (macOS as a
  supported daemon platform), out of this ticket's scope to decide
  unilaterally.
- Torture's relay pool re-verified a second time (ticket 011 still
  holds); `braeburn`'s mDNS uptime recorded honestly as inconclusive for
  the multi-hour degradation quirk (this session's own redeploys kept
  resetting its process uptime); every Windows claim from tickets
  002-006 listed as not-hardware-verified, with the green CI run cited.
- Full suite: `uv run pytest -q` — 885 passed/3 skipped before this
  ticket's fix, 886 passed/3 skipped after (the one new test). No robot
  in active use was disturbed.
