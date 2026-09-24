---
status: done
sprint: '005'
tickets:
- 005-007
- 005-008
- 005-009
- 005-010
---

# Idempotent fleet deployment tooling, migration runbook, and user docs

## Description

Sprint 005's roadmap entry originally assumed the fleet-migration half of
its scope would touch the old `Busboombot/mbdeploy` repo's own Ansible
roles and golden Pi Zero image to retire `mbdeploy serve`/
`mbrelay.service` fleet-wide. Team-lead scoping for sprint 005 narrowed
that: this sprint builds repeatable deployment tooling *inside* `mbtools`
itself, applies it only to the five existing hardware test hosts
(`meili`, `loki`, `hodr`, `magni`, `braeburn`), and writes a runbook the
stakeholder executes against the rest of the fleet — including the
production relay host `torture` — on their own schedule. No existing
issue file tracked this half of the sprint's work (the two issues
already split out, `mbregistry-windows-platform-support.md` and
`relay-in-data-plane-can-be-misidentified-by-reprobe.md`, cover the
daemon/platform side only), so this issue exists to track it.

`mbtools` also currently has no user-facing documentation beyond its
design docs (`docs/design/`, `docs/brief.md`) — the README stops at
"Status: early planning" (stale as of sprint 001) and a bare development
section. That gap closes here too, since it's part of making the tools
usable by someone who isn't this project's own planning history.

## Scope

- **Idempotent deployment tooling** (`scripts/deploy-host.sh` and/or an
  Ansible role, implementer's choice): builds a wheel, installs it,
  runs `mbregistry install-service` (systemd unit + udev rule), adds the
  operating user to the udev group, enables/starts the service — safe to
  re-run on an already-provisioned host. Applied only to the five
  hardware test hosts; never to `torture` or any other production host.
- **Migration runbook** (`docs/migration.md`): the stakeholder-executed
  procedure for the rest of the fleet — retiring `mbdeploy serve` and
  `mbrelay.service`, cut-over order (relay host `torture` last, after
  verifying robot-console against sprint 004's compatibility pool on an
  already-migrated host), rollback, and the exact text to paste into
  both wikis (public `docs/wiki/`-equivalent guidance and the internal
  Robot Garage wiki — `mbtools` itself has no `docs/wiki/`; the runbook
  gives the stakeholder ready-to-paste text for wherever their fleet docs
  live).
- **README usage section**: install, the four programs, and common flows
  (`list`, `deploy --repo`, `mbserial`, `mbrelay connect robot@host`,
  peering/`--peer`, ports) — no machine specifics (public repo).
- **Final hardware acceptance** on the five test hosts, covering this
  issue's tooling idempotency and the sprint's other hardware-verifiable
  claims (the re-probe fix), with results in
  `docs/acceptance/005-hardware.md`.

## Port from

N/A — new tooling and docs specific to `mbtools`'s own deployment story;
no equivalent exists in either source repo to port from
(`scripts/deploy-test-host.sh`, sprint 004, is this project's own
precedent, not a port).

## Depends on

Sprint 004's `mbrelay`/robot-console-compatibility work (a node cannot be
safely cut over per the runbook until that compatibility layer exists —
already shipped). No dependency on `mbregistry-windows-platform-support.md`
— fleet hosts in scope here are Linux/macOS, not Windows.
