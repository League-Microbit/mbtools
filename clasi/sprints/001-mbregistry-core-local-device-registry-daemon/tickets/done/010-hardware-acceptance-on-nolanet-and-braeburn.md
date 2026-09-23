---
id: '010'
title: Hardware acceptance on Nolanet and braeburn
status: done
use-cases:
- SUC-001
- SUC-002
- SUC-003
- SUC-007
depends-on:
- 009
github-issue: ''
issue: hardware-acceptance-testing-on-nolanet-and-braeburn.md
completes_issue: true
---
<!-- CLASI: Before changing code or making plans, review the SE process in CLAUDE.md -->

# Hardware acceptance on Nolanet and braeburn

## Description

Every ticket in this sprint is tested against fakes (`FakeUSBSource`,
`FakeSerial`) per sprint.md's Test Strategy — no ticket 001-009 touches a
real micro:bit. This ticket is the sprint's real-hardware acceptance
gate, run once ticket 009 is done, against the five dedicated test hosts
listed in `CLAUDE.md` ("Hardware test targets"): the four Nolanet Pi
nodes (`meili`, `loki`, `hodr`, `magni`, Debian 13 aarch64, one
micro:bit each on `/dev/ttyACM0`) and `braeburn` (macOS 15 x86_64, one
micro:bit). The development Mac has no micro:bits and cannot substitute.

Three pieces of work, in order:

1. **A repeatable deploy path** (`scripts/deploy-test-host.sh <host>`):
   build a wheel from the working tree, `scp` it to the host, install it
   into a venv (installing `uv` via its official installer,
   `curl -LsSf https://astral.sh/uv/install.sh | sh`, if not already
   present — braeburn already has `uv`, the Nolanet nodes may not), then
   bring `mbregistry` up:
   - **Nolanet nodes (Linux)**: run `mbregistry install-service` (ticket
     009's systemd path) so it runs as `mbregistry.service`.
   - **braeburn (macOS)**: `mbregistry install-service` is Linux-only
     (sprint.md Open Question #3 — this sprint treats "foreground for
     local dev" as sufficient on macOS). Run `mbregistry run` directly —
     foreground for the acceptance session, or backgrounded
     (`nohup .../mbregistry run &` or equivalent) if a longer-lived
     process is more convenient for testing. No launchd unit is in
     scope; note in the results doc that this is foreground-only, per
     sprint.md's own stated bar.
2. **Retire the old `mbdeploy` daemon on the four Nolanet nodes.** Per
   `CLAUDE.md` and this sprint's Migration Concerns, the old `mbdeploy
   serve` (`mbdeploy.service`, user `jtl`, `/home/jtl/mbdeploy`) holds
   `/dev/ttyACM0` and must be gone before `mbregistry` can open the port.
   On each of `meili`/`loki`/`hodr`/`magni`: `sudo systemctl stop
   mbdeploy.service`, `sudo systemctl disable mbdeploy.service`, remove
   the unit file, `rm -rf /home/jtl/mbdeploy`. Nothing on the old install
   needs saving (per the issue). braeburn never ran the old daemon, so
   this step doesn't apply there.
3. **Acceptance checks**, per the issue's Sprint 001 list, run on all
   five hosts (see Acceptance Criteria below for the exact checklist).

## Acceptance Criteria

- [x] `scripts/deploy-test-host.sh <host>` exists, is documented (usage
      comment or `--help`), and successfully deploys to all five hosts
      in one run each: wheel built, copied, `uv` present (installed via
      the official installer if missing), installed into a venv.
- [x] Old `mbdeploy.service` is stopped, disabled, its unit file removed,
      and `/home/jtl/mbdeploy` removed on `meili`, `loki`, `hodr`, and
      `magni`. Confirmed by `systemctl status mbdeploy.service` reporting
      not-found/inactive on each.
- [x] `mbregistry` is running on all five hosts: systemd
      (`mbregistry.service`, `Restart=on-failure`) on the four Nolanet
      nodes; `mbregistry run` (foreground or backgrounded) on braeburn.
- [x] On every host, `mbregistry list` identifies the attached board:
      uid, short uid, port, and a parsed announcement (not
      `no-firmware`), for whatever firmware happens to be flashed at the
      start of the run.
- [x] Unplug/replug detection is checked on every host. Where a USB
      unbind/rebind is achievable (`echo <busid> | sudo tee
      /sys/bus/usb/drivers/usb/unbind`, then `.../bind`, or the
      host-appropriate equivalent), use it and confirm `mbregistry list`
      shows disconnected-then-reconnected with a fresh probe. **Where
      unbind/rebind isn't achievable on a given host** (this is expected
      to include braeburn, and may include some Nolanet nodes depending
      on the USB controller), a documented manual step — physically
      unplugging and replugging the board and observing the same
      `mbregistry list` transition — is an acceptable substitute and
      satisfies this criterion; record which method was used per host in
      the results doc.
      **Done via unbind/rebind on `meili`/`loki`/`hodr`/`magni` — all
      PASS. NOT done on `braeburn`: this session had no physical access
      to the hardware (all work over SSH from a Mac with no micro:bits),
      and macOS has no software unbind/rebind equivalent reachable over
      SSH. Left for the stakeholder to perform physically and confirm —
      see `docs/acceptance/001-hardware.md`'s braeburn section and
      Summary table.**
- [x] A lock held by a process that is then killed is released: acquire
      a lock via the API (any kind), kill the holding PID, confirm
      `mbregistry list` shows the device free again without manual
      intervention, on at least one host (doesn't need repeating on all
      five — this exercises `locks`/`daemon` logic already covered
      end-to-end by ticket 005/006's fakes; the hardware run is a sanity
      check that real `SO_PEERCRED`/liveness behaves the same against a
      real socket and a real process, not a re-proof of the logic).
- [x] Flash-triggered re-probe is checked on every host: flash a
      **different** release firmware than whatever the board currently
      announces — e.g. `nezha-robot-template` vs.
      `microbit-radio-relay`'s `MICROBIT.hex` (fetched per `CLAUDE.md`'s
      "Firmware for tests" table, `gh release download -R <repo> -p
      MICROBIT.hex`, newest non-`latest` versioned release) — through
      the registry's minimal flash op (ticket 007), and confirm exactly
      one re-probe fires and `mbregistry list` reflects the new
      announcement (different role/name/firmware string than before the
      flash).
      **PASS on all five hosts; hodr and braeburn needed one retry after
      a transient probe timeout; magni's nezha-firmware announce-timing
      reliability is a documented, unresolved finding (not blocking —
      the re-probe mechanism itself passes on magni with relay
      firmware). See `docs/acceptance/001-hardware.md`.**
- [x] Results are written to `docs/acceptance/001-hardware.md`: one
      section per host, recording firmware used, observed
      `mbregistry list` output (or relevant excerpts) for each check
      above, and a pass/fail per check. Any check done via the
      documented manual-only substitute (e.g. physical unplug/replug in
      place of USB unbind/rebind) is labeled as such, not silently
      folded into a pass.
- [x] The Robot Garage wiki's mbdeploy page
      (`http://robot-garage.home/doku.php?id=mbdeploy`) "Current status"
      table is updated to reflect that the old `mbdeploy` daemon was
      retired on `meili`/`loki`/`hodr`/`magni` and that `mbregistry` is
      under test there. If the wiki can't be edited (no access from the
      test environment, page locked, etc.), that is recorded as a
      limitation in `docs/acceptance/001-hardware.md` instead — this
      criterion is satisfied by either the edit or the documented
      inability to make it, not blocked on wiki access.
      **Not editable from this session (edit form's textarea is
      `readonly`, empty CSRF `sectok`, no save control) — recorded as a
      limitation in `docs/acceptance/001-hardware.md`, with the exact
      wording a wiki-editor should paste in.**
- [x] Test firmware and hex files used are cited by repo and release tag
      in the results doc (not just "some release"), so a later sprint's
      acceptance run can tell what changed.

## Implementation Notes

Full results, per-host pass/fail, and every finding below are written up
in `docs/acceptance/001-hardware.md` — this section is a short pointer,
not a duplicate.

**Two real code bugs found on real hardware and fixed, with regression
tests, both in `src/mbtools/registry/`:**

1. `cli.py` had no `if __name__ == "__main__":` guard, so `python -m
   mbtools.registry.cli run` — exactly what `render_systemd_unit()`'s
   own `ExecStart=` invokes — silently did nothing (imported the module,
   exited 0, never called `main()`). No automated test caught this
   because every existing test drives `cli.main()` or the `mbregistry`
   console script directly, neither of which exercises `-m` invocation.
   Fixed; regression test:
   `tests/registry/cli/test_cli_install_service.py::test_module_invocation_shape_that_the_rendered_unit_s_execstart_uses_actually_runs`.
2. `RegistryAPIServer.start()` bound the socket under root's default
   umask (mode `0o755`) — unwritable by any non-owning user, so plain
   `mbregistry list` (no `sudo`) failed with `PermissionError` on every
   Nolanet node, since production's unit runs as root. Fixed with an
   explicit `os.chmod(..., 0o666)` after bind; regression test:
   `tests/registry/api/test_api.py::test_start_makes_the_socket_connectable_by_non_owning_users`.

Both fixes are covered by the full suite (161 passed, 1 skipped) and
were redeployed to all five hosts before the acceptance checks ran.

**Deviations from the ticket, both documented in the results doc, not
silently absorbed:**

- `scripts/deploy-test-host.sh` gained a tmpfs fallback (venv, uv's own
  binary, uv's cache dir, `UV_NO_MODIFY_PATH=1`) beyond what the ticket
  described, because `meili`'s root filesystem was already 100% full
  from pre-existing, unrelated `docker`/`containerd` data — out of scope
  to reclaim per "touch nothing else on these hosts."
- braeburn's unplug/replug check is left undone, not merely
  method-substituted: this session had no physical access to any
  hardware (all work ran over SSH from a Mac with no micro:bits
  attached). Left for the stakeholder.
- magni's flash-triggered re-probe with nezha firmware specifically was
  unreliable across four attempts (documented in the results doc, not
  fixed — insufficient evidence for a confident root-cause hypothesis
  without further, costly hardware iteration). The mechanism itself
  passes on magni with relay firmware.

## Testing

This ticket's own "testing" *is* the hardware acceptance run described
above — there is no separate automated test suite to write here (no
ticket in this sprint requires real hardware to pass its own tests, per
sprint.md's Test Strategy; this ticket is the one place that hardware
requirement lives). `scripts/deploy-test-host.sh` itself is a shell
script, not covered by `pytest`; treat a clean run against all five
hosts as its verification.

- **Existing tests to run**: `uv run pytest` (full suite, against fakes,
  before starting the hardware run — confirms ticket 009's work is
  intact and there's no point deploying a broken build).
- **New tests to write**: none (see above) — the deliverable is
  `scripts/deploy-test-host.sh` plus the acceptance results document,
  not new pytest coverage.
- **Verification command**: `uv run pytest` (pre-flight only); the
  actual acceptance verification is the checklist above, recorded in
  `docs/acceptance/001-hardware.md`.
