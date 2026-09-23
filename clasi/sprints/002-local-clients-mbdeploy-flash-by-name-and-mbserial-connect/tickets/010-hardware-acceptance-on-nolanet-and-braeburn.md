---
id: '010'
title: Hardware acceptance on Nolanet and braeburn
status: open
use-cases: [SUC-001, SUC-002, SUC-003, SUC-004, SUC-005, SUC-006]
depends-on: ['007', '008', '009']
github-issue: ''
issue:
- mbdeploy-flash-by-name-via-mbregistry.md
- mbserial-raw-serial-access-local-or-remote.md
- mbdeploy-install-latest-release-hex-from-a-github-repo.md
completes_issue: true
---
<!-- CLASI: Before changing code or making plans, review the SE process in CLAUDE.md -->

# Hardware acceptance on Nolanet and braeburn

## Description

Run this sprint's real-hardware acceptance pass, the same house style as
sprint 001's ticket 010 (`docs/acceptance/001-hardware.md`): against the
five dedicated test hosts in `CLAUDE.md`'s "Hardware test targets"
(`meili`, `loki`, `hodr`, `magni` — Debian 13 aarch64 — and `braeburn`,
macOS 15), over SSH, using `scripts/deploy-test-host.sh <host>` (sprint
001's script, including its tmpfs fallback for `meili`'s full root
filesystem) to deploy this sprint's code. No ticket 001-009 depends on
this ticket or on real hardware to pass — this is the one hardware gate
for the whole sprint.

Checks to run, per host:

- `mbdeploy deploy <name> --repo <repo>` against all three firmware
  repos named in `CLAUDE.md`'s "Firmware for tests" table (radio relay,
  nezha robot, remote joystick) — exercising SUC-001/SUC-002's full
  resolve/lock/flash/verify/wait-for-reprobe/report flow with a real
  GitHub release fetch, not a local `--hex` file, at least once per host.
- `mbdeploy list` output compared against `mbregistry list` on the same
  host (SUC-003's rendering-parity claim, checked against real data, not
  just unit tests).
- `mbserial <name>` connecting to a robot **without rebooting it** — this
  is the one check that specifically needs a robot already running known
  firmware and a way to observe it wasn't reset (e.g., a running
  program's uptime/state, or a `--reset`-vs-not comparison) — plus one
  `--reset` connect on the same or another board, confirmed to actually
  reset it.
- Busy-lock behavior: hold a lock (flash, serial, or debug) on one board
  from one session, attempt the conflicting operation from another
  session on the same host, confirm the fail-fast message names the
  holder's kind and PID (SUC-005), on at least one host — sprint 001's
  own ticket 010 precedent of "exercise the mechanism on one host,
  document it, don't repeat identically on all five" applies here too.
- **Magni's post-flash timing finding, specifically re-tested**: per
  sprint.md's Open Questions, flash the nezha firmware to magni's board
  several times (matching sprint 001's four-attempt pattern) and record
  whether ticket 004's bounded extra HELLO retry closes the gap
  `docs/acceptance/001-hardware.md` documented, or whether it needs to be
  escalated as its own follow-up issue — this is a required check, not
  optional, precisely because sprint.md flagged it as unresolved.
- `mbdeploy debug <name> -- <pyocd args>` at least once, confirming the
  `debug`-kind lock is taken and released.

Record every result — PASS/FAIL/MANUAL, per host, same legend and rigor
as `docs/acceptance/001-hardware.md` — in a new
`docs/acceptance/002-hardware.md`. Follow this project's own `CLAUDE.md`
guidance on not disturbing a robot in active use where a check doesn't
specifically require one (per-host "spare board" note); the mbserial
no-reboot check is the one exception that specifically needs a board
already running firmware to observe.

## Acceptance Criteria

- [ ] `scripts/deploy-test-host.sh` deploys this sprint's code to all
      five hosts.
- [ ] `mbdeploy deploy --repo` flashes successfully via a real GitHub
      release fetch (not a local `--hex` file) at least once, using at
      least one of the three named firmware repos, with the flash
      streamed live, verified, and the new announcement reported.
- [ ] `mbdeploy list` and `mbregistry list` output are compared side by
      side on real data and confirmed to match (SUC-003).
- [ ] `mbserial <name>` is confirmed, on real hardware, not to reboot a
      running robot by default, and a `--reset` connect is confirmed to
      actually reset one.
- [ ] Busy-lock fail-fast (holder kind + PID) is exercised on at least
      one host for at least one lock kind.
- [ ] `mbdeploy debug` is exercised at least once.
- [ ] Magni's nezha-firmware post-flash timing finding is specifically
      re-tested and the result (closed / still flaky / inconclusive) is
      recorded, not skipped.
- [ ] Every result is recorded in `docs/acceptance/002-hardware.md`,
      PASS/FAIL/MANUAL per host, with any manual substitute explicitly
      labeled as such (not silently folded into PASS) — same rigor as
      `docs/acceptance/001-hardware.md`.
- [ ] Any code fix made during this pass (as sprint 001's ticket 010
      found two real bugs) is committed with a regression test, not left
      as a manual-only fix.

## Testing

- **Existing tests to run**: `uv run pytest` (the full suite, once, per
  `.claude/rules/source-code.md` — this ticket is not exempt from that
  even though its own focus is hardware).
- **New tests to write**: none required by the hardware pass itself; any
  code fix discovered during this pass gets its own regression test, per
  sprint 001's ticket 010 precedent (both of its fixes shipped with
  tests).
- **Verification command**: `uv run pytest`
