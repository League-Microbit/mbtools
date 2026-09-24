---
id: '007'
title: Idempotent fleet deployment tooling for the five test hosts
status: open
use-cases:
- SUC-003
depends-on: []
github-issue: ''
issue: mbtools-fleet-deployment-tooling-and-migration-docs.md
completes_issue: true
---
<!-- CLASI: Before changing code or making plans, review the SE process in CLAUDE.md -->

# Idempotent fleet deployment tooling for the five test hosts

## Description

Build repeatable, idempotent deployment tooling *inside* `mbtools`
(sprint architecture Decision 4) that goes beyond
`scripts/deploy-test-host.sh` (sprint 004's precedent, which only
builds+installs the wheel into a venv): this tooling also runs
`mbregistry install-service` (systemd unit + udev rule) and completes
the one-time host setup, so a test host ends in a running,
restart-on-failure `mbregistry.service` with correct USB permissions —
not just an installed package. **Applied only to the five hardware test
hosts** (`meili`, `loki`, `hodr`, `magni`, `braeburn`) — never to
`torture` or any other production host (team-lead scoping constraint,
sprint.md Scope/Out of Scope).

**Approach** (implementer's choice between the two, or both — the
ticket's acceptance criteria are shape-agnostic)
- A hardened `scripts/deploy-host.sh`, extending
  `scripts/deploy-test-host.sh`'s existing build-wheel-and-install-into-
  venv logic (sprint 004) with: running `mbregistry install-service`
  remotely, running the printed `systemctl`/`udevadm`/`usermod` follow-up
  commands that `cmd_install_service` already prints rather than runs
  (this script is the operator-facing thing that *does* run them,
  consistent with that function's own "printing rather than running
  keeps the command itself safe to test" design), and enabling/starting
  the service.
- And/or an Ansible role under a new `ansible/` directory doing the
  same, for hosts where an inventory-driven approach is preferred.
- **Idempotency is the hard requirement**: re-running against an
  already-provisioned, already-healthy host must not disrupt the running
  service, must not duplicate the udev rule or systemd unit (this
  already holds for `install-service` alone per sprint 004 ticket 008 —
  this ticket's job is to make the *wrapping* script/role equally safe
  to re-run, e.g. it must stop the service before replacing a running
  daemon's own files the way `CLAUDE.md`'s existing `uv venv --clear`
  root-owned-`__pycache__` caveat already documents, not just assume a
  fresh host).
- Restricted to the five hardware test hosts by construction (a fixed
  host list, or an inventory file scoped to only those five) — no
  mechanism in this tooling should make it easy to accidentally target
  `torture` or an unlisted host.

**Files to create/modify**
- `scripts/deploy-host.sh` (new) and/or `ansible/` (new directory,
  role(s) + playbook).
- `docs/acceptance/005-hardware.md` is where ticket 010 records this
  tooling's actual idempotency runs — this ticket doesn't write that
  file itself, only the tooling it exercises.

**Documentation updates**: `docs/migration.md` (ticket 008) references
this tooling by name/path as part of the runbook; no other doc changes
required by this ticket alone.

## Acceptance Criteria

- [ ] Running the tooling against a fresh (or already-migrated) test
      host results in: the current working tree's wheel installed, the
      systemd unit and udev rule written, the service enabled and
      started, and the operating user in the USB-access group.
- [ ] Re-running the tooling against the same, now-healthy host produces
      no service disruption (verified by checking the service stays
      `active`/doesn't restart unnecessarily, or by an explicit
      before/after uptime check) and no duplicate unit/rule content.
- [ ] The tooling's host list/inventory is fixed to exactly `meili`,
      `loki`, `hodr`, `magni`, `braeburn` — no flag or default that
      would let it target an arbitrary hostname without deliberate
      editing.
- [ ] The tooling's own usage comment/README documents that it must
      never be pointed at `torture` or any host outside the five test
      hosts, matching `scripts/deploy-test-host.sh`'s existing
      usage-comment convention.
- [ ] Exercised against at least one real test host this sprint (the
      full five-host run is ticket 010's job; this ticket proves the
      tooling works on at least one before hardware acceptance relies on
      it for all five).

## Testing

- **Existing tests to run**: none in `tests/` — this is
  operations/deployment tooling (shell/Ansible), not Python library
  code, matching `scripts/deploy-test-host.sh`'s own precedent (no unit
  tests for that script either).
- **New tests to write**: none in `tests/`; verification is direct
  execution against a real test host (see Acceptance Criteria).
- **Verification command**: `scripts/deploy-host.sh <host>` (or the
  Ansible playbook invocation), run twice against the same host to
  demonstrate idempotency, per the acceptance criteria above.

## Implementation Notes

- Build on `scripts/deploy-test-host.sh` rather than duplicating its
  wheel-build-and-install logic — that script already handles the
  low-disk-space tmpfs fallback (`meili`'s known constraint, per
  CLAUDE.md) and the `uv`-bootstrap-if-missing step; this ticket's
  tooling should call it or share its logic, not reimplement it.
- `mbregistry.service` runs as root with no `User=` (per
  `render_systemd_unit`) — stopping it before an in-place venv
  replacement matters the same way CLAUDE.md's existing
  `uv venv --clear` caveat already documents for manual redeploys; the
  idempotent tooling must handle this itself rather than relying on an
  operator remembering it.
