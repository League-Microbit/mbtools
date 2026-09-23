---
id: '004'
title: mbrelay, robot-console compatibility, and fleet migration
status: roadmap
branch: sprint/004-mbrelay-robot-console-compatibility-and-fleet-migration
use-cases: []
issues:
- mbrelay-relay-protocol-client-over-mbregistry.md
- mbregistry-windows-platform-support.md
- non-root-usb-access-and-pyocd-permission-hang.md
---
<!-- CLASI: Before changing code or making plans, review the SE process in CLAUDE.md -->

# Sprint 004: mbrelay, robot-console compatibility, and fleet migration

## Goals

Close out the four-program set with `mbrelay` (rebuilt as a pure relay
protocol client over `mbregistry`), preserve robot-console's contract,
bring `mbregistry` up on Windows, and migrate the fleet off the old
`mbdeploy`/`mbrelay.service` packages onto `mbtools`.

## Problem

`mbrelay` is the last of the three client tools and the one with the
widest blast radius if broken: robot-console silently mistunes moved
robots if the `_mbrelay._tcp` pool port, `registry=` TXT record, or
`/names` HTTP endpoint disappear (spec §"robot-console compatibility
contract"). Separately, the fleet's Pi Zero nodes already run the old
`mbdeploy` package and `mbrelay.service`; cutting over is a fleet-wide
operational change, not a per-node opt-in, and the project's own
migration guidance (this repo's CLAUDE.md-equivalent care for
not disturbing robots in use) applies. Windows is also still unaddressed
going into this sprint — sprints 001-003 were Linux-first throughout.

## Solution

- `mbrelay-relay-protocol-client-over-mbregistry.md`: `mbrelay connect
  [robot[@host]]` asks the registry which attached devices are relays
  and free, locks one (local or remote, using sprint 003's peering and
  remote-lock machinery), resets and normalizes on acquire
  (BREAK/reset, `HELLO`, `!VER?`, RAW250/frag-off/echo-off/P7/ch0-grp10),
  tunes to a robot via the name registry (`!CG`, `!GO`, optional `PING`),
  supports `--send`/`--expect` scripting and an interactive terminal, and
  restores defaults (`!DEFAULTS`) on release. The name registry (robot →
  channel/group) gets a home, likely a table in the registry database,
  replicated to peers per sprint 003's event bus.
- Robot-console compatibility: decide between a compatibility endpoint
  hosted by the registry, migrating robot-console itself, or a
  transition period running both — and implement whichever is chosen.
  This is the sprint that must not let the `_mbrelay._tcp` /
  `registry=` TXT / `/names` HTTP contract silently break.
- `mbregistry-windows-platform-support.md`: Windows USB event watch,
  named pipe query service, and Windows service (SCM) install, bringing
  `mbregistry` to parity with the Linux daemon built in sprint 001.
- Fleet migration: retire `mbdeploy serve` and `mbrelay.service` on every
  node, update the Ansible roles, the golden Pi Zero image, and both
  wikis (public `docs/wiki/` and the internal Robot Garage wiki) to point
  at `mbtools`. Pick a spare board (no announcing firmware, e.g. `togov`)
  for any hands-on verification against real hardware, per this
  project's standing rule about not disturbing robots in use.

## Success Criteria

- `mbrelay connect` reaches a free relay (local or remote), normalizes
  and restores it correctly, and supports scripted and interactive
  sessions, matching today's `mbrelay` behavior.
- robot-console continues to resolve relays and names without falling
  back to derived addresses — verified against the actual contract
  (SRV record, TXT `registry=`, `GET /names/<name>`), not just unit
  tests of the new code.
- `mbregistry` installs and runs as a Windows service, with USB
  attach/detach detected and the query API reachable via named pipe.
- At least one fleet node is migrated end-to-end (old packages retired,
  `mbtools` installed and running) without disturbing a robot in active
  use, and both wikis reflect the new install/deployment procedure.

## Scope

### In Scope

- `mbrelay-relay-protocol-client-over-mbregistry.md`.
- `mbregistry-windows-platform-support.md`.
- Fleet migration: Ansible roles, golden Pi Zero image, both wikis,
  retiring `mbdeploy serve` and `mbrelay.service`.

### Out of Scope

- Any further protocol or peering changes — this sprint consumes
  sprint 003's peering and remote-lock machinery as-is.

## Test Strategy

(Deferred to Phase 2 detail planning — this is a roadmap-only entry. The
robot-console compatibility contract needs explicit integration
verification, not just unit coverage, given its silent-failure mode.)

## Architecture

(Deferred to Phase 2 detail planning — this is a roadmap-only entry.
Expected to size as substantial: a new client module, a new
name-registry data table, a Windows platform implementation, and a
robot-console compatibility decision that may add a new external-facing
endpoint.)

### Architecture Overview

(Deferred to Phase 2 detail planning.)

### Design Rationale

(Deferred to Phase 2 detail planning. Notably: the robot-console
compatibility decision (compatibility endpoint vs. migrate robot-console
vs. transition period) and the name-registry's storage/replication
design are both open decisions this sprint's architecture phase should
resolve or explicitly escalate to the stakeholder.)

### Migration Concerns

(Deferred to Phase 2 detail planning. This sprint carries real fleet
migration risk — see Solution above.)

## Use Cases

(Deferred to Phase 2 detail planning. Relevant use cases from
`docs/design/usecases.md`: UC-012 (mbrelay connect to a robot by name),
plus the robot-console compatibility contract described in
`docs/design/specification.md` cross-cutting §7.)

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
