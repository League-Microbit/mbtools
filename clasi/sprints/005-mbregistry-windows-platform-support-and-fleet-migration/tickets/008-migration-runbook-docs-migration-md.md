---
id: '008'
title: Migration runbook (docs/migration.md)
status: open
use-cases:
- SUC-004
depends-on:
- '007'
github-issue: ''
issue: mbtools-fleet-deployment-tooling-and-migration-docs.md
completes_issue: true
---
<!-- CLASI: Before changing code or making plans, review the SE process in CLAUDE.md -->

# Migration runbook (docs/migration.md)

## Description

Write `docs/migration.md`: the complete, executable procedure the
stakeholder runs themselves, on their own schedule, to retire
`mbdeploy serve`/`mbrelay.service` on the rest of the fleet — including
the production relay host `torture` — using ticket 007's tooling as the
per-host mechanism. This sprint does not execute any of this against a
production host (sprint.md Scope/Out of Scope); this ticket only
documents it, completely enough that the stakeholder doesn't need to
reconstruct any of the reasoning from sprint history.

**Approach** — `docs/migration.md` must cover, at minimum:
- **Pre-requisites**: sprint 004's `mbrelay`/robot-console-compatibility
  work must already be running and verified on at least one host before
  any node is cut over (sprint 004 sprint.md's own stated dependency,
  restated here for the stakeholder).
- **Cut-over order**: ordinary nodes first; the relay host `torture`
  **last**, and only after robot-console has been directly verified
  against sprint 004's compatibility pool (`_mbrelay._tcp`/`registry=`/
  `/names`) on an already-migrated node — the exact verification steps
  from `docs/acceptance/004-hardware.md`'s Scenario 4 are a good
  template to restate here in operator-facing form.
- **Per-host procedure**: retire `mbdeploy serve`
  (`sudo systemctl stop/disable mbdeploy.service`, remove the unit — the
  exact commands already used and documented in
  `docs/acceptance/001-hardware.md`'s "Old `mbdeploy` retirement"
  section for the four Nolanet test nodes, generalized for the remaining
  fleet), retire `mbrelay.service` similarly, then run ticket 007's
  deployment tooling to bring the host up on `mbtools`.
- **Rollback**: what to do if a cut-over host doesn't come up cleanly —
  re-enable the old `mbdeploy.service`/`mbrelay.service` units (not
  deleted, only disabled, until the new install is confirmed healthy —
  state this explicitly as the rollback's precondition) and stop the new
  `mbregistry.service`.
- **Exact wiki text to paste**: both the public-docs-equivalent guidance
  and the internal Robot Garage wiki's "Current status" table — the
  Daemon column text per host, following the same pattern
  `docs/acceptance/001-hardware.md`'s own "Not done — no edit access"
  note already drafted for the four Nolanet test nodes ("mbdeploy
  retired `<date>`; mbregistry (mbtools) active, enabled"), extended to
  cover `torture` and any other remaining hosts by name once migrated.
  This sprint does not edit the wiki itself (Scope) — this section is
  ready-to-paste text only.
- **Explicit statement of what this sprint did and did not do**: the
  five test hosts got the new deployment tooling exercised against them
  (ticket 007/010); no production host, including `torture`, was
  touched by this sprint. This is a load-bearing statement for the
  stakeholder to not assume more happened than actually did.

**Files to create/modify**
- `docs/migration.md` (new).

**Documentation updates**: this ticket *is* the documentation update.

## Acceptance Criteria

- [ ] `docs/migration.md` exists and covers every bullet in the Approach
      section above: prerequisites, cut-over order (`torture` last),
      per-host retirement + install procedure, rollback, and exact
      paste-ready wiki text.
- [ ] The document explicitly states this sprint did not execute any
      part of the runbook against a production host.
- [ ] The per-host retirement commands match what
      `docs/acceptance/001-hardware.md` already used and verified on the
      four Nolanet test nodes (no invented command sequence).
- [ ] The robot-console verification step references the specific
      contract (`_mbrelay._tcp`/`registry=`/`/names`) and points at
      `docs/acceptance/004-hardware.md`'s Scenario 4 as the template,
      rather than restating it from scratch with room to drift.
- [ ] The document names `docs/migration.md`'s own tooling dependency
      (ticket 007's script/role) by path, so the stakeholder isn't left
      to guess which tool to run.

## Testing

- **Existing tests to run**: none — this is a documentation-only
  ticket.
- **New tests to write**: none.
- **Verification command**: N/A (a documentation review, not a test
  run — read the finished `docs/migration.md` end-to-end and confirm it
  is executable by someone who wasn't in this sprint's planning).

## Implementation Notes

- This document is written *for* the stakeholder, not for this
  project's own SE process — keep it operator-facing (concrete commands,
  concrete order, concrete rollback), not a restatement of sprint
  architecture.
- Cross-reference `docs/acceptance/001-hardware.md` and
  `docs/acceptance/004-hardware.md` by section rather than duplicating
  their content wholesale, so this document doesn't drift from the
  hardware evidence those two already recorded.
