---
id: '007'
title: Idempotent fleet deployment tooling for the five test hosts
status: done
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
not just an installed package. **Applied only to the six hardware test
hosts** (`meili`, `loki`, `hodr`, `magni`, `braeburn`, `torture`) — never
to any other production host. **Scope updated mid-sprint (stakeholder
decision, 2026-09-24): `torture` is now the sixth host** — see the
Acceptance Criteria's scope note and Implementation Notes below for the
full story; this supersedes sprint.md's original "never `torture`"
scoping text for this ticket specifically.

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
- Restricted to the six hardware test hosts by construction (a fixed
  host list, or an inventory file scoped to only those six) — no
  mechanism in this tooling should make it easy to accidentally target
  an unlisted host.

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

**Scope note (stakeholder decision, 2026-09-24 — see Implementation
Notes): this ticket's host list was expanded from five to six hosts
mid-sprint, adding `torture`.** Every criterion below that named "the
five test hosts" is read as the six: `meili`, `loki`, `hodr`, `magni`,
`braeburn`, `torture`. `torture` additionally gets a one-time,
idempotent pre-step (stop and disable the legacy `mbrelay.service`,
leaving its unit file and binary on disk) before `mbregistry` starts
there.

- [x] Running the tooling against a fresh (or already-migrated) test
      host results in: the current working tree's wheel installed, the
      systemd unit and udev rule written, the service enabled and
      started, and the operating user in the USB-access group. (macOS
      `braeburn` is the one exception, unchanged from sprint 004: no
      systemd/udev exist there, so `mbregistry` runs as the foreground
      process the module docstring already documents — see
      Implementation Notes.)
- [x] Re-running the tooling against the same, now-healthy host produces
      no service disruption (verified by checking the service stays
      `active`/doesn't restart unnecessarily, or by an explicit
      before/after uptime check) and no duplicate unit/rule content.
- [x] The tooling's host list/inventory is fixed to exactly `meili`,
      `loki`, `hodr`, `magni`, `braeburn`, `torture` — no flag or default
      that would let it target an arbitrary hostname without deliberate
      editing.
- [x] The tooling's own usage comment/README documents the fixed six-host
      list and that it must never be pointed at any other host, matching
      `scripts/deploy-test-host.sh`'s existing usage-comment convention
      — updated from "never `torture`" to "`torture` is the sixth host,
      with its own one-time legacy-service step" per the scope change
      above.
- [x] Exercised against at least one real test host this sprint (the
      full six-host run is ticket 010's job; this ticket proves the
      tooling works on at least one before hardware acceptance relies on
      it for all six). Exercised, with idempotency proven by a second
      run, against **all six** hosts this session (see Implementation
      Notes) — beyond this criterion's own bar.

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

### Scope change: six hosts, not five (stakeholder decision, 2026-09-24)

Dispatched mid-sprint with an explicit instruction to treat this
ticket's "five test hosts" as six, adding `torture` — the former
production relay host, previously out of this sprint's scope per
sprint.md's own "does not touch `torture`" scope correction. This
ticket's deliverable (`scripts/deploy-host.sh`) and this file's
Acceptance Criteria were updated accordingly, per that instruction.
`sprint.md` itself was **not** edited (out of this ticket's write
authority — a sprint-level document); the discrepancy between
sprint.md's "never torture" text and this ticket's actual six-host
scope is intentional and should be reconciled by the team-lead at
sprint close, not silently left for a future reader to trip over.

### What was built

`scripts/deploy-host.sh` (new), extending `scripts/deploy-test-host.sh`
(called as a subprocess, never reimplemented) with:

- A fixed, in-script `ALLOWED_HOSTS` array (`meili loki hodr magni
  braeburn torture`) — the only way to add a host is to edit the
  script; no flag/env override exists.
- **Idempotency via a working-tree content hash**, not a version number
  (this project only bumps `pyproject.toml`'s version once per sprint,
  at `close_sprint` — see the programmer-agent workflow rule — so two
  different code states inside the same sprint can share one version
  string; a version-only check would have missed real changes). The
  script hashes `git rev-parse HEAD` + `git diff HEAD` locally and
  compares it to a marker file on the host (`/tmp/.mbtools-deploy-state-
  <user>` — deliberately `/tmp`, not `$HOME`: `meili`'s root filesystem
  is observed at 100% full, and even a few-byte marker write to `$HOME`
  failed there during this session's testing; the same tmpfs-not-
  surviving-reboot tradeoff `deploy-test-host.sh` already accepts for
  its own venv fallback applies here too — a reboot makes the next run
  look like "changed" and do one harmless full rebuild rather than
  silently skip one it can't prove is current). Only when the hash
  differs does the script stop the service, rebuild, and reinstall;
  `install-service` and its systemctl/udevadm/usermod follow-ups are
  still run every time regardless (they write deterministic content and
  are no-ops against an already-correct, already-running host), which
  self-heals unit/udev drift without ever touching the running daemon.
  Both the systemd path and the macOS path assert the service's
  `ActiveEnterTimestamp` (Linux) / process start time (`braeburn`) is
  byte-identical before and after a hash-matched ("nothing changed")
  run, and fail loudly if it is not — this is the script's own
  automated proof of "no disruption on a no-op re-run", not just a
  manual claim.
- **A real bug hit during testing, fixed in the tooling**: stopping
  `mbregistry.service` alone was not enough to satisfy CLAUDE.md's own
  documented "`uv venv --clear` can't remove root-owned `__pycache__`"
  caveat — reproduced verbatim on `hodr` (`uv venv --clear` failed with
  `Permission denied` removing `lib/`, even immediately after `systemctl
  stop`, because *stopping* the daemon doesn't *fix ownership* of files
  it already wrote as root while it ran). Fixed by having the script
  `sudo rm -rf` both possible venv locations (the normal `$HOME` one and
  the low-disk tmpfs one) before calling `deploy-test-host.sh`, whenever
  a rebuild is needed — root removes its own leftovers, then the
  unprivileged rebuild starts clean. This is exactly the "idempotent
  tooling must handle this itself rather than relying on an operator
  remembering it" the ticket's own pre-existing Implementation Notes
  called for, just one layer deeper than "stop the service" alone.
- **`torture`-only handling**, gated on `$HOST = torture`: before
  anything else, checks `systemctl is-active`/`is-enabled
  mbrelay.service`; if either says yes, stops and disables it (`||
  true`-guarded, so a partial prior state doesn't abort the script) and
  explicitly does **not** touch `/etc/systemd/system/mbrelay.service` or
  `/usr/local/bin/mbrelay` (no `rm`, ever). If already stopped/disabled,
  logs and skips — the idempotent no-op path. Every other host is
  Linux/systemd (the four Nolanet nodes plus `torture` itself) except
  `braeburn` (macOS): that host gets no `install-service` call at all
  (there is no systemd there to write a unit into) — instead the script
  finds the currently-running foreground `mbregistry run` process (per
  `registry/cli.py`'s own module docstring, "macOS foreground dev use"),
  preserves its exact `--socket`/`--db` args, and (only on a hash
  mismatch) stops and relaunches it via `sudo nohup ... & disown`,
  matching how it was already being run by hand on that host before
  this ticket.
- Relay pool is enabled by default (`mbregistry run` only disables it
  with `--no-relay-pool`, which this script never passes, and
  `render_systemd_unit`'s default `ExecStart=` doesn't add it either) —
  no extra flag was needed to satisfy "`mbregistry` must come up with
  the relay pool enabled" on `torture`.

### Verified this session (all six hosts, each run twice)

- `meili`: low-disk tmpfs venv fallback path exercised (confirms that
  fallback still works through this new wrapper); first run rebuilt +
  started the service, second run skipped the rebuild and confirmed
  `ActiveEnterTimestamp` unchanged.
- `loki`, `magni`: normal `$HOME` venv path; same first-run-rebuilds,
  second-run-no-op-confirmed pattern.
- `hodr`: **hit a real infra issue, unrelated to this ticket's code** —
  `hodr` currently has no default network route (`nmcli` shows
  `connected (local only)`; `ip route` has no `0.0.0.0/0` entry, and
  `nmcli connection up` didn't restore one). This surfaced mid-test as a
  DNS failure fetching `pyocd` during `uv pip install`, after the script
  had already (correctly, per its own logic) stopped `mbregistry.service`
  and removed the old venv in preparation for a rebuild it couldn't then
  complete — the script does not roll back a failed rebuild (same as
  `deploy-test-host.sh` itself, which has never had rollback; a
  network-dependent build step failing mid-flight is a pre-existing risk
  class this ticket doesn't newly introduce). Restored `hodr` to healthy
  by hand this session, using `uv pip install --offline` against its
  already-warm local cache (`~/.cache/uv`, still had every dependency
  from a prior install) — a workaround, not something the script does
  automatically. `hodr`'s networking is a host/infra problem for the
  stakeholder to fix, not an `mbtools` defect; flagging here so a future
  session doesn't re-diagnose it from scratch. Once restored, a second
  `deploy-host.sh hodr` run confirmed the marker/idempotency path itself
  works correctly (no rebuild attempted, service undisturbed).
- `braeburn`: first run stopped the existing hand-started process,
  rebuilt, and relaunched it with its original args (confirmed via
  `ps`); second run confirmed via process start-time comparison that
  nothing was touched.
- `torture`: first run performed the one-time legacy-`mbrelay.service`
  stop/disable (confirmed: `systemctl is-active`/`is-enabled` both now
  report inactive/disabled, unit file and `/usr/local/bin/mbrelay`
  binary still present with their original timestamps/permissions),
  then built, installed, and started `mbregistry.service` (confirmed
  active/enabled). Second run confirmed both the legacy-step's own
  idempotent skip path ("already stopped and disabled -- skipping") and
  the same `ActiveEnterTimestamp`-unchanged proof the other Linux hosts
  got.

### Known limitation found on `torture`, out of this ticket's scope

The scope update also asked to confirm `mbregistry` "sees all four
relays" on `torture` after the cutover. It does not, currently — not
because of anything in this ticket's tooling, but because of a
pre-existing bug in `registry.store`/`registry.peering` that this
ticket's real-hardware pass against `torture` is the first scenario to
expose (three of `torture`'s four relay boards were previously tested on
`hodr` in an earlier sprint, and `hodr`'s own stale peered rows for
those same uids keep winning a host-ownership race against `torture`'s
own, correctly-probed local rows — full description, code pointers, and
reproduction with `torture`'s relay-pool port (7444) is now in
`CLAUDE.md`'s "Known real bug" note, right after the existing dwc_otg
quirk). This is a `registry.store`/`registry.peering` correctness issue
— a different module boundary than `scripts/deploy-host.sh` — and fixing
it is architecture-level work (the protocol has no "local wins" rule at
all today), not something this ticket's scope covers. Flagging
prominently rather than silently declaring the "sees all four relays"
bullet done: it is not, and shouldn't be marked as verified by ticket
010's hardware acceptance pass without either fixing this first or
explicitly noting it as a known gap there too. Separately, and
independently of the bug above: at the time of this session's testing
only 3 of `torture`'s 4 relay boards were even physically enumerating
(`ls /dev/ttyACM*`/`lsusb` agreed on 3; `dmesg` showed a `USB disconnect`
for the 4th earlier in the day) — ordinary hardware flakiness, not
something this tooling can fix either, but worth knowing before assuming
a 4th relay is available for a future test.

### Full suite

`uv run pytest -q` — 870 passed, 3 skipped (no regressions; this
ticket's changes are shell script + `CLAUDE.md` + this ticket file, no
Python source touched).
