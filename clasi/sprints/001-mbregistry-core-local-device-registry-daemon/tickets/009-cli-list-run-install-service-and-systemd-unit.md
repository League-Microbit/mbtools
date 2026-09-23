---
id: 009
title: CLI (list/run/install-service) and systemd unit
status: in-progress
use-cases:
- SUC-004
- SUC-007
depends-on:
- 008
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

- [x] `mbregistry list` renders STATE/short-uid/FIRMWARE/port columns
      plus an error-note line per row needing one, matching UC-004's
      description; `--json` produces the same data as structured JSON.
- [x] `mbregistry list` against an absent socket prints a clear
      "registry unavailable" message and exits with the stable exit
      code from ticket 008 — no stack trace, no hang.
- [x] `mbregistry run` starts the full daemon (all modules wired) and
      responds to `mbregistry list` from a second process/connection
      while running — exercised in tests via an in-process daemon
      against `tmp_path` store/socket and `FakeUSBSource`, not a
      subprocess.
- [x] `mbregistry install-service` produces a unit file whose rendered
      content is asserted by a golden-file test: correct `ExecStart=`,
      `Restart=on-failure` present, unit named `mbregistry.service`
      (distinct from `mbrelay.service`), and directives (or equivalent
      setup) ensuring both `/run/mbregistry/` and `/var/lib/mbregistry/`
      exist before the service starts. Installing into a real systemd is
      not part of this ticket's automated tests (see sprint.md's Test
      Strategy) — call out manual verification on a spare board (never
      a robot in active use, per this project's `CLAUDE.md`) as a
      follow-up note in the ticket's own PR/commit, not a CI gate.
  - [x] All three console scripts (`mbregistry`, plus confirming
      `mbdeploy`/`mbserial`/`mbrelay` still resolve as ticket 001's
      stubs, unchanged by this ticket) are exercised by at least a
      smoke test.

## Implementation Notes

**Two issues from ticket 008, resolved as part of this ticket's
assembly work (team-lead direction):**

1. **Concurrency — one shared lock across `daemon` and `api`.**
   `mbtools.registry.cli.assemble_daemon_and_api` (new) is the one place
   both `mbregistry run` and this ticket's own tests construct a
   `Daemon`/`RegistryAPIServer` pair: it builds exactly one
   `threading.RLock` and passes it to both (`Daemon(lock=...)`,
   `RegistryAPIServer(lock=...)`, both new constructor parameters,
   defaulting to a private `RLock` each when omitted so every existing
   test in `tests/registry/daemon/` and `tests/registry/api/` needed no
   changes). `Daemon.run_once()`/`_maybe_probe()` were restructured to
   hold that lock only around in-memory/single-sqlite-statement
   bookkeeping (the attach/detach diff, the pre/post-probe store writes)
   — never around `identity.probe()`'s real port I/O, which can block
   for over a second. New test:
   `tests/registry/cli/test_cli_run.py::test_concurrent_daemon_cycles_and_api_calls_do_not_race`
   hammers `daemon.run_once()` from one thread while four client threads
   concurrently `list`/`lock`/`unlock` over the same shared
   `Store`/`LockManager`, asserting no exception and no deadlock from
   any thread, and that the lock table ends up consistent.
2. **Flash no longer holds the shared lock for the pyocd run.**
   `RegistryAPIServer._op_flash` (`src/mbtools/registry/api.py`) now
   holds the shared lock only for the short bookkeeping before the run
   (resolve the record, confirm this connection's flash-kind lock is
   held) and after it (release that lock) — the streamed
   `flash_op.flash_hex(...)` call itself runs with the shared lock
   released. The per-device `flash`-kind lock (already held as a
   verified precondition, never released mid-run) continues to protect
   the device for the whole run, and the daemon's own "never probe a
   locked device" check (`LockManager.status`) still sees it as locked
   throughout — this doesn't reopen the race the flash-kind lock exists
   to prevent, it only stops the *shared* lock from blocking every other
   device's `list`/`get`/`lock` (and, once the lock became shared with
   the daemon in this ticket, the daemon's own scan loop) for the
   duration of one flash. All of `tests/registry/api/test_api.py`'s
   existing flash tests pass unchanged, since the observable outcome for
   any single flash request is identical.
   `docs/design/registry-api.md`'s "Known limitations" section is
   updated to record both fixes in place of the two gaps it previously
   flagged as open.

**Design decisions for this ticket's own scope:**

- **FIRMWARE column.** `store.DeviceRecord` has no dedicated
  firmware-version field (see the ERD in sprint.md) — `role`/
  `common_name` from the device's own announcement stand in for it
  (`cli._firmware_cell`), rendered as `"{role}/{common_name}"`, matching
  `mbrelay`'s `_firmware_cell` precedent for distinguishing "not probed
  yet" / "no firmware" from a real value.
- **`install-service` writes the unit file and prints (does not run) the
  `systemctl` commands** — the ticket's Description explicitly leaves
  this choice to the implementer. Printing keeps the command exercisable
  without root or a real systemd in CI, matching sprint.md's Test
  Strategy. Manual verification of the unit against a real systemd
  belongs on ticket 010's real-hardware pass (Nolanet nodes), on a
  spare board, not a robot in active use, per this project's
  `CLAUDE.md` — not done as part of this ticket.
- **`ExecStart=` invokes `{sys.executable} -m mbtools.registry.cli run`**
  rather than a bare `mbregistry` PATH lookup, mirroring `flash.py`'s own
  `_PYOCD` invocation-through-the-interpreter precedent: mbtools is
  typically installed into an isolated `uv` venv whose `bin/` directory
  is not on the `PATH` systemd uses for `ExecStart=`.
- **macOS foreground dev use** (braeburn): `mbregistry run --socket
  ... --db ...` works unprivileged once both paths are pointed somewhere
  writable (e.g. under `/tmp` or `~/.local/state/`) — the *default*
  paths (`/run/mbregistry/`, `/var/lib/mbregistry/`) still need root on
  either platform, matching production; this ticket adds
  `--socket`/`--db` flags and `$MBREGISTRY_SOCKET`/`$MBREGISTRY_DB` env
  vars (flag beats env beats default) as the override path, not a
  macOS-specific default. Verified against `mbregistry --help`, `list`
  against an absent socket, and `install-service`'s rendered output
  directly on this machine (a Mac); `mbregistry run` itself was not run
  as a real background process in this session (sandboxed shell
  environment does not permit backgrounding long-running daemons) — its
  assembly path (`assemble_daemon_and_api`) is exercised end-to-end,
  with a real `AF_UNIX` socket and real threads, by
  `test_run_then_list_smoke` and the concurrency test above; a real
  `mbregistry run` invocation on braeburn is left to ticket 010's
  hardware pass.

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
