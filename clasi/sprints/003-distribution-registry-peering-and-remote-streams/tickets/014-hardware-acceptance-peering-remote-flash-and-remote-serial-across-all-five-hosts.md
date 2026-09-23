---
id: '014'
title: 'Hardware acceptance: peering, remote flash, and remote serial across all five
  hosts'
status: open
use-cases: [SUC-001, SUC-002, SUC-003, SUC-004, SUC-005]
depends-on: ['009', '010', '012', '013']
github-issue: ''
issue:
- mbregistry-peering-mdns-and-zeromq.md
- mbdeploy-flash-by-name-remote.md
- mbserial-raw-serial-access-remote.md
completes_issue: true
---
<!-- CLASI: Before changing code or making plans, review the SE process in CLAUDE.md -->

# Hardware acceptance: peering, remote flash, and remote serial across all five hosts

## Description

Per sprint.md's Success Criteria and `CLAUDE.md`'s "Hardware test
targets," verify the whole sprint end-to-end across the five dedicated
hosts — meili, loki, hodr, magni (Debian 13 aarch64, one micro:bit each)
and braeburn (macOS, one micro:bit) — mirroring
`docs/acceptance/001-hardware.md`/`002-hardware.md`'s existing format and
`scripts/deploy-test-host.sh`'s existing deploy path. The dev Mac itself
has no micro:bits but can run clients, and a zero-device registry,
against the network (per this sprint's own prompt) — use it as a sixth
node for the `--peer`-across-networks check if convenient, or as an
observer running `mbregistry list`/`mbdeploy`/`mbserial` against the
fleet.

Every earlier ticket's automated tests use fakes/loopback; this is the
first and only ticket that proves the real thing across real hosts,
real mDNS, and real hardware resets.

## Acceptance Criteria

- [ ] Deploy the sprint's build to all five hosts via
      `scripts/deploy-test-host.sh` (stopping `mbregistry.service` first
      on the four Nolanet nodes per `CLAUDE.md`'s existing operational
      note), confirming `pyzmq`/`python-zeroconf` install cleanly on
      both Debian 13 aarch64/Python 3.13 and macOS x86_64.
- [ ] **Peer discovery (SUC-001/UC-013)**: with all five `mbregistry`
      instances running on the garage LAN, confirm every pair discovers
      the other via mDNS within a bounded time (document the observed
      convergence time, mirroring sprint.md's Success Criteria).
- [ ] **Combined listing (SUC-002/UC-005)**: `mbregistry list` (or
      `mbdeploy list`) run from any one host (or the dev Mac) shows all
      five boards, HOST-tagged correctly.
- [ ] **Peer-vanish (SUC-005/UC-013's error flow)**: stop `mbregistry` on
      one Nolanet node (simulating an unplug at the registry level, not
      the board level — this sprint's peering, not USB detach); confirm
      the other four show that host's device as "peer unreachable," not
      stale-connected; restart it and confirm it reconverges.
- [ ] **Remote flash (SUC-003/UC-009)**: `mbdeploy deploy <name>` for a
      board on one Nolanet node, run from a different Nolanet node (and
      separately, from the dev Mac, which owns no boards itself) —
      confirm the same retry/mass-erase robustness
      `docs/acceptance/001-hardware.md` already exercised locally still
      applies (a transient probe/communication hiccup, if one occurs
      naturally, or is otherwise noted as "not observed this session,"
      same honesty standard as that document).
- [ ] **Remote serial with working reset (SUC-004/UC-011)**: `mbserial
      <name>` against a peer-owned relay board, run remotely, with
      `--reset` delivering a working BREAK/DTR reset across the network
      stream (sprint.md's Success Criteria: "A remote `mbserial` session
      can deliver a reset/BREAK to a relay board across the network
      stream" — use the relay firmware per `CLAUDE.md`'s firmware table).
- [ ] **Explicit `--peer` across a simulated network boundary
      (SUC-001/UC-014)**: disable mDNS on one host (or use `--peer
      HOST:PORT` from a host that otherwise wouldn't discover the
      others) and confirm the explicit link still converges the same as
      mDNS-discovered peering.
- [ ] Sudo requirements noted in `CLAUDE.md` (Nolanet nodes need `sudo`
      for local pyOCD/serial access) are re-confirmed as unaffected by
      this sprint's new network-facing daemon paths, or updated if this
      sprint changes them.
- [ ] Findings written to `docs/acceptance/003-hardware.md`, in the same
      format as `001`/`002` (firmware used, per-host findings, any new
      operational notes for `CLAUDE.md`), and `CLAUDE.md` itself updated
      if this pass surfaces anything a future session needs to know
      (new ports to be aware of, any host-specific peering quirk).
- [ ] Per the top-level CLAUDE.md's documentation rule (though that rule
      lives in the `mbdeploy` repo's own CLAUDE.md, not this project's —
      irrelevant here; this project's own doc obligations are whatever
      `docs/design/*.md` and `docs/acceptance/*.md` already establish):
      no `docs/design/*.md` changes are expected from this ticket beyond
      what earlier tickets already made — this ticket's job is
      verification and `docs/acceptance/003-hardware.md`, not new design
      decisions.

## Testing

- **Existing tests to run**: none — this ticket's entire scope is manual
  hardware verification, not automated tests (mirrors
  `docs/acceptance/001-hardware.md`/`002-hardware.md`'s own ticket 010's
  precedent).
- **New tests to write**: none (hardware acceptance ticket) — findings
  are documented in `docs/acceptance/003-hardware.md`, not codified as
  pytest cases.
- **Verification command**: N/A — this ticket's "verification" is the
  hardware pass itself, documented per the acceptance-criteria list
  above.
