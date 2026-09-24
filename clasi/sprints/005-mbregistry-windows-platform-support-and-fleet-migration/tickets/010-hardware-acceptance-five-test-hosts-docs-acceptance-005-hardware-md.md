---
id: '010'
title: 'Hardware acceptance: five test hosts (docs/acceptance/005-hardware.md)'
status: open
use-cases:
- SUC-006
depends-on:
- '001'
- '007'
- '008'
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

- [ ] `docs/acceptance/005-hardware.md` records, per scenario above:
      what was run, on which host(s), the exact commands/output, and a
      PASS/FAIL/inconclusive verdict.
- [ ] The re-probe fix is validated by a *deliberately constructed*
      relay-in-data-plane-during-reprobe scenario, not just cited as
      "the unit test covers this" — real hardware evidence, mirroring
      how the original bug was found.
- [ ] Deployment-tooling idempotency is confirmed on all five test
      hosts, with at least one explicit re-run-on-healthy-host check.
- [ ] Every one of `mbregistry list`/`mbdeploy deploy`/`mbserial`/
      `mbrelay connect`/`mbrelay names` is exercised against real
      hardware at least once, using a spare/non-production board.
- [ ] `braeburn`'s observed uptime at the time of the mDNS check is
      recorded, along with the outcome (discovered / not discovered /
      not attempted and why).
- [ ] Every Windows-specific claim from tickets 002-006 is explicitly
      listed and marked not-hardware-verified — this document must not
      silently omit them or imply they were hardware-tested.
- [ ] No robot in active use was disturbed during this ticket's hardware
      work (spare-board rule).

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
  being validated), and 008 (so the acceptance document can reference
  the finished runbook by name/section, the same way prior
  acceptance docs cross-reference sprint.md).
- Follow `docs/acceptance/004-hardware.md`'s own honesty precedent: if
  something doesn't reproduce cleanly, or a result is ambiguous, say so
  plainly rather than smoothing it over — that document's own
  "New finding (not root-caused, flagged honestly)" section is the house
  style to match.
