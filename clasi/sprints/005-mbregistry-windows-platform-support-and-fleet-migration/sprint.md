---
id: '005'
title: mbregistry Windows platform support and fleet migration
status: roadmap
branch: sprint/005-mbregistry-windows-platform-support-and-fleet-migration
use-cases: []
issues:
- mbregistry-windows-platform-support.md
- relay-in-data-plane-can-be-misidentified-by-reprobe.md
---
<!-- CLASI: Before changing code or making plans, review the SE process in CLAUDE.md -->

# Sprint 005: mbregistry Windows platform support and fleet migration

## Goals

Split out of sprint 004 for scope realism (see that sprint's planning
notes). Bring `mbregistry` up as a genuine second supported daemon
platform on Windows, and finish the operational cutover of the garage
fleet from the old `mbdeploy`/`mbrelay.service` packages to `mbtools`,
with both wikis reflecting the new install/deployment procedure.

## Problem

Sprints 001-004 were Linux-first (with macOS as a development
convenience). `mbregistry-windows-platform-support.md` (split from the
sprint 1 daemon issue) is still open: no Windows USB event source, no
Windows service (SCM) install, no named-pipe local query API. Separately,
sprint 004 will finish the last client tool (`mbrelay`) and its
robot-console compatibility layer, but the fleet's Pi Zero nodes still
run the *old* `mbdeploy` package and `mbrelay.service` — cutting them
over to `mbtools` is a fleet-wide operational change, not a per-node
opt-in, and needs the Ansible roles, the golden Pi Zero image, and both
wikis (public `docs/wiki/` and the internal Robot Garage wiki) updated
together, not left to drift.

**Hard constraint carried from planning: there is no Windows test
machine available to this project.** Windows work in this sprint must be
built and verified with fakes/mocks for the OS-specific seams (USB event
source, SCM registration, named pipe transport), the same way sprint
003's remote-stream code was unit-tested before hardware existed, and the
sprint's own hardware-acceptance ticket must explicitly flag the Windows
pieces as **not hardware-verified** rather than silently skipping them.
Real Windows verification is a follow-up outside this sprint's authority
to promise.

## Solution

- `mbregistry-windows-platform-support.md`: a Windows attach/detach event
  source behind the same interface `registry.usbwatch`'s Linux
  implementation uses; Windows service (SCM) registration with
  restart-on-failure parity with the systemd unit `registry.cli`
  installs today; a named-pipe local query service as the Windows
  counterpart to the Unix-socket API in `registry.api`; and Windows file
  paths under `ProgramData` (the `/var/lib` and `/run` equivalents),
  flagged as an assumption for stakeholder confirmation per the brief.
- Fleet migration: retire `mbdeploy serve` and `mbrelay.service` on every
  remaining node, update the Ansible roles and golden Pi Zero image (this
  tooling lives in the old `mbdeploy` repo, not this one — expect this
  sprint's tickets to touch that repo as well as `mbtools`), and update
  both wikis to point at `mbtools`. This consumes sprint 004's
  robot-console compatibility decision and `mbrelay` client as prerequisites
  — a node cannot be safely cut over until `mbrelay`/robot-console
  compatibility exists.
- As in sprint 004, hands-on verification against real hardware uses a
  spare board with no announcing firmware, per this project's standing
  rule about not disturbing a robot in active use.

## Success Criteria

- `mbregistry` installs and runs as a Windows service, with USB
  attach/detach detected (verified with a fake/injectable event source —
  no Windows hardware available) and the local query API reachable over
  a named pipe.
- At least one more fleet node is fully migrated (old packages retired,
  `mbtools` installed and running) without disturbing a robot in active
  use, and both wikis reflect the new install/deployment procedure.
- The sprint's hardware-acceptance results explicitly list which claims
  are hardware-verified and which (all Windows-specific ones) are
  verified only against fakes.

## Scope

### In Scope

- `mbregistry-windows-platform-support.md`.
- Fleet migration: Ansible roles and golden Pi Zero image (old
  `mbdeploy` repo), both wikis, retiring `mbdeploy serve` and
  `mbrelay.service` on remaining nodes.

### Out of Scope

- Any further protocol or peering changes — this sprint consumes sprint
  003's peering/remote-lock machinery and sprint 004's `mbrelay`/
  robot-console-compatibility work as-is.
- Real Windows hardware verification — no test machine is available;
  see "Problem" above.

## Test Strategy

(Deferred to Phase 2 detail planning — this is a roadmap-only entry.
Windows-specific code must be designed for and verified with
fakes/mocks at the OS-seam boundary, since no Windows hardware exists
for this project.)

## Architecture

(Deferred to Phase 2 detail planning.)

### Architecture Overview

(Deferred to Phase 2 detail planning.)

### Design Rationale

(Deferred to Phase 2 detail planning.)

### Migration Concerns

(Deferred to Phase 2 detail planning. This sprint, like 004, carries
real fleet migration risk.)

## Use Cases

(Deferred to Phase 2 detail planning.)

## GitHub Issues

(GitHub issues linked to this sprint's tickets. Format: `owner/repo#N`.)

## Definition of Ready

Before tickets can be created, all of the following must be true:

- [ ] Sprint planning document is complete (sprint.md, including its
      Architecture and Use Cases sections)
- [ ] Architecture review passed (or skipped, for changes with no
      architectural impact)
- [ ] Stakeholder has approved the sprint plan

## Tickets

| # | Title | Depends On |
|---|-------|------------|

Tickets execute serially in the order listed.
