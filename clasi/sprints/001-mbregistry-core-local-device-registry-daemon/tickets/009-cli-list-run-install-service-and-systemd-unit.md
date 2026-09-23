---
id: '009'
title: 'CLI (list/run/install-service) and systemd unit'
status: open
use-cases: [SUC-004, SUC-007]
depends-on: ['008']
github-issue: ''
issue: mbregistry-device-registry-daemon.md
completes_issue: true
---
<!-- CLASI: Before changing code or making plans, review the SE process in CLAUDE.md -->

# CLI (list/run/install-service) and systemd unit

## Description

Build `mbtools.registry.cli`, the `mbregistry` console script (replacing
ticket 001's placeholder), with three subcommands, and finish this
sprint's Definition of Ready item on the systemd unit.

**`mbregistry list [--json]`** (UC-004, spec §3.8): connects to the API
socket (ticket 008) as a client — per spec §3.5's "other programs
consult it through a service," this command does not read `store`
directly, even though it's running on the same host as the daemon.
Renders a table with the conveniences spec §3.8 asks for, learned from
`mbrelay`: a STATE column (free / `locked by <kind> pid <n>` /
no-firmware / gone), a short-uid column (`_firmware_cell`-style
rendering precedent in `microbit-radio-relay/server/src/mbrelay/cli.py`
around line 406, for how to render "unknown/never asked" vs. a real
firmware string), a FIRMWARE/version column, an error-note line under
any row that needs one, and `--json` for machine consumption. If the
socket is absent, prints a clear "registry unavailable" message (not a
stack trace) and exits with the stable exit code ticket 008 defined.

**`mbregistry run`** (the daemon's actual entry point): constructs
`usbwatch.PollingPortWatcher`, `identity`, `store` (pointed at
`/var/lib/mbregistry/devices.db` by default), `locks`, `flash`,
`daemon`, and `api` (socket at `/run/mbregistry/api.sock` by default),
and runs the daemon loop in the foreground. This is what a systemd
`ExecStart=` invokes, and also what a developer runs directly on macOS
per sprint.md's Open Questions #3 ("runs in the foreground for local
dev" is this sprint's stated bar for macOS).

**`mbregistry install-service`** (UC-015, spec §3.8): writes a systemd
unit file (`Restart=on-failure`, correct `ExecStart=` pointing at
`mbregistry run`, `RuntimeDirectory=`/`StateDirectory=` or equivalent to
ensure `/run/mbregistry/` and `/var/lib/mbregistry/` exist) and prints
the `systemctl enable/start` commands for the operator to run (or runs
them directly — implementer's choice, document whichever is chosen).
Per sprint.md's Migration Concerns, the unit is named `mbregistry.
service`, distinct from the fleet's existing `mbrelay.service`/old
`mbdeploy serve` — this ticket does not need to detect or retire those
(sprint 004's job).

## Acceptance Criteria

- [ ] `mbregistry list` renders STATE/short-uid/FIRMWARE/port columns
      plus an error-note line per row needing one, matching UC-004's
      description; `--json` produces the same data as structured JSON.
- [ ] `mbregistry list` against an absent socket prints a clear
      "registry unavailable" message and exits with the stable exit
      code from ticket 008 — no stack trace, no hang.
- [ ] `mbregistry run` starts the full daemon (all modules wired) and
      responds to `mbregistry list` from a second process/connection
      while running — exercised in tests via an in-process daemon
      against `tmp_path` store/socket and `FakeUSBSource`, not a
      subprocess.
- [ ] `mbregistry install-service` produces a unit file whose rendered
      content is asserted by a golden-file test: correct `ExecStart=`,
      `Restart=on-failure` present, unit named `mbregistry.service`
      (distinct from `mbrelay.service`), and directives (or equivalent
      setup) ensuring both `/run/mbregistry/` and `/var/lib/mbregistry/`
      exist before the service starts. Installing into a real systemd is
      not part of this ticket's automated tests (see sprint.md's Test
      Strategy) — call out manual verification on a spare board (never
      a robot in active use, per this project's `CLAUDE.md`) as a
      follow-up note in the ticket's own PR/commit, not a CI gate.
  - [ ] All three console scripts (`mbregistry`, plus confirming
      `mbdeploy`/`mbserial`/`mbrelay` still resolve as ticket 001's
      stubs, unchanged by this ticket) are exercised by at least a
      smoke test.

## Testing

- **Existing tests to run**: `uv run pytest tests/registry/api/` and
  the full `tests/registry/` tree — this ticket is the integration
  point for every module built this sprint.
- **New tests to write**: `list` table and `--json` rendering against a
  running in-process daemon with `FakeUSBSource`-driven devices in
  various states (free, locked, no-firmware, gone); `list` against an
  absent socket; an end-to-end `run`-then-`list` smoke test; the
  systemd unit golden-file test.
- **Verification command**: `uv run pytest tests/registry/cli/` and
  `uv run pytest` (full suite — this is the sprint's last ticket, and
  per `.claude/rules/source-code.md` the full suite otherwise only runs
  once at `close_sprint`; running it here too is a sanity check before
  handoff, not a substitute for that gate).
