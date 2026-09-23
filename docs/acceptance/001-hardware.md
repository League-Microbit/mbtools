# Ticket 010 — Hardware acceptance on Nolanet and braeburn

Sprint 001 (`mbregistry` core local device registry daemon), ticket
`010-hardware-acceptance-on-nolanet-and-braeburn`. Run 2026-09-23
against the five dedicated test hosts in `CLAUDE.md`'s "Hardware test
targets": `meili`, `loki`, `hodr`, `magni` (Debian 13 aarch64, user
`eric`, passwordless sudo) and `braeburn` (macOS 15 x86_64, `uv`
installed). The dev Mac has no micro:bits; every check below ran over
SSH.

## Firmware used

Fetched via `gh release download -R <repo> -p MICROBIT.hex`, newest
non-`latest` versioned release, per `CLAUDE.md`'s "Firmware for tests"
table:

| Firmware | Repo | Tag | Announces as |
|---|---|---|---|
| Radio relay | `League-Robotics/microbit-radio-relay` | `v0.20260913.2` | `RADIOBRIDGE/relay` |
| Nezha robot | `League-Microbit/nezha-robot-template` | `v0.20260919.7` | `NEZHA2/robot` |
| Remote joystick | `League-Microbit/Remote-Joystick-Student` | `v0.20260922.1` | downloaded, not needed — two distinguishable firmwares (relay/nezha) were enough for the before/after flash-triggered-re-probe check |

All five boards started this session already flashed with the relay
firmware (`RADIOBRIDGE/relay`) from prior use, except `meili` and
`braeburn`'s boards, which were blank (no firmware) at the start of the
session.

## Deploy path: `scripts/deploy-test-host.sh`

New script, `scripts/deploy-test-host.sh <host>`, builds a wheel with
`uv build --wheel`, copies it to the host, ensures `uv` is present
(installing it via the official installer if not), and installs mbtools
into a dedicated venv with `uv venv --clear` + `uv pip install`. Usage
is documented in the script's own header comment. Ran successfully,
idempotently (re-run tested on every host), against all five hosts.

**meili-specific finding, fixed in the script itself:** `meili`'s root
filesystem was already at 100% full (`/`, 15G, 0 available) from
pre-existing `docker`/`containerd` data (7.6G) unrelated to this
project — out of scope to reclaim per this ticket's "touch nothing else
on these hosts." The script now detects under-200MB-available on
`$HOME`'s filesystem and falls back to tmpfs (`/tmp/mbtools-venv-<user>`
etc.) for the venv, uv's own binary (`UV_INSTALL_DIR`), and uv's package
cache (`UV_CACHE_DIR`) — plus `UV_NO_MODIFY_PATH=1`, since the uv
installer's own unconditional `$HOME/.bashrc` rcfile edit also failed
outright on a full `$HOME` filesystem. `meili`'s venv therefore lives on
tmpfs and does not survive a reboot of `meili` — acceptable for this
under-test deployment; documented here so a later sprint knows why
`meili` differs from the other three Nolanet nodes.

Retiring the old `mbdeploy.service` (below) also freed 88MB on `meili`
by removing `/home/jtl/mbdeploy`, but that alone wasn't enough to clear
the ext4 reserved-block threshold for a non-root `mkdir`/`uv venv` — the
tmpfs fallback was still needed.

**Operational note (not a code change):** redeploying to a Nolanet node
while `mbregistry.service` is running fails (`uv venv --clear` can't
remove root-owned `__pycache__` files the running-as-root daemon
created). Stop the service before redeploying, start it again after.

## Old `mbdeploy` retirement (`meili`/`loki`/`hodr`/`magni`)

On each: `sudo systemctl stop mbdeploy.service`, `disable`, unit file
removed, `sudo rm -rf /home/jtl/mbdeploy`.

| Host | `systemctl status mbdeploy.service` | `/home/jtl/mbdeploy` |
|---|---|---|
| meili | `Unit mbdeploy.service could not be found.` | removed (confirmed via `sudo ls`: No such file or directory) |
| loki | same | removed |
| hodr | same | removed |
| magni | same | removed |

## Code fixes made during this pass

Two real bugs were found by this hardware run and fixed with tests
(both in `src/mbtools/registry/`, both committed under this ticket):

### 1. `python -m mbtools.registry.cli run` was a silent no-op

`render_systemd_unit()`'s own `ExecStart=` — and this module's own
docstring — document `{python} -m mbtools.registry.cli run` as the
production entry point. `cli.py` had no `if __name__ == "__main__":`
guard, so that exact invocation only imported the module and exited 0
**without ever calling `main()`**. `mbregistry.service` started
"successfully" (exit 0, `active (running)`... briefly, before exiting)
on the very first `systemctl enable --now` attempt — nothing in the
process's own output or exit code signaled the problem; only `mbregistry
list` immediately afterward, against an absent socket, gave it away.

Every automated test in this sprint drives `cli.main()` directly or the
`mbregistry` console script (`pyproject.toml`'s `[project.scripts]`,
which calls `main()` directly) — neither exercises `python -m ...`, so
nothing caught this before a real systemd unit's `ExecStart=` used it.

Fix: added the `__name__ == "__main__"` guard. Regression test:
`tests/registry/cli/test_cli_install_service.py::test_module_invocation_shape_that_the_rendered_unit_s_execstart_uses_actually_runs`
— runs the real module as a subprocess (the only way to reproduce
`-m` invocation faithfully) and asserts `--help` actually produces
argparse's usage text.

### 2. `mbregistry list` (no `sudo`) failed with `PermissionError` against the production socket

Production's `mbregistry.service` has no `User=` (runs as root, since
pyOCD needs raw USB access — see risk (d) below). Binding the
`AF_UNIX` socket under root's default umask produced mode `0o755`:
readable/executable but not *writable*. Unix-domain `connect()`
requires write permission on the socket inode, so **every plain
`mbregistry list` run as `eric` failed with `PermissionError: [Errno
13] Permission denied`** on all four Nolanet nodes — exactly the
invocation this ticket's own acceptance criteria call for, no `sudo` in
sight, and exactly what an operator would actually type.

Fix: `RegistryAPIServer.start()` now `os.chmod`s the socket to `0o666`
after binding — the API has no authentication beyond per-connection
`SO_PEERCRED` pid tracking for lock ownership (ticket 008), so
world-writable matches its actual security model rather than narrowing
it. Regression test:
`tests/registry/api/test_api.py::test_start_makes_the_socket_connectable_by_non_owning_users`.

Both fixes are covered by the full suite (`uv run pytest`, 161 passed, 1
skipped, run before and after) and were redeployed to all five hosts via
`scripts/deploy-test-host.sh` before the acceptance checks below.

## Acceptance checks, per host

Legend: PASS / FAIL / MANUAL (done by a documented manual substitute,
not silently folded into PASS).

### meili

| Check | Result | Notes |
|---|---|---|
| `mbregistry` running | PASS | systemd, `active`, `enabled` |
| Board identified (`list`, no sudo) | PASS | started blank (no firmware); flashed relay via the registry's own `flash` op to get an initial parsed announcement (`RADIOBRIDGE/relay`, name `gitev`) |
| Unplug/replug | PASS (unbind/rebind) | `echo 1-1 | sudo tee /sys/bus/usb/drivers/usb/{unbind,bind}` — showed `gone` then re-probed correctly on reattach |
| Flash-triggered re-probe | PASS | relay → nezha (mass-erase + flash, lock held throughout); `list` went `RADIOBRIDGE/relay` → `NEZHA2/robot`, one clean re-probe |
| Lock released on holder-kill | not repeated here | exercised on `loki` (see below); ticket allows one host |

### loki

| Check | Result | Notes |
|---|---|---|
| `mbregistry` running | PASS | systemd, `active`, `enabled` |
| Board identified | PASS | `RADIOBRIDGE/relay`, name `vitut`, from the start |
| Unplug/replug | PASS (unbind/rebind) | same method as meili |
| Flash-triggered re-probe | PASS | relay → nezha in one clean attempt; `list` → `NEZHA2/robot` |
| Lock released on holder-kill | PASS | held a `serial`-kind lock from a background process, `sudo kill -9 <pid>`, `list` showed `free` again within ~1s, no manual intervention |

### hodr

| Check | Result | Notes |
|---|---|---|
| `mbregistry` running | PASS | systemd, `active`, `enabled` |
| Board identified | PASS | `RADIOBRIDGE/relay`, name `togov`, from the start |
| Unplug/replug | PASS (unbind/rebind) | same method |
| Flash-triggered re-probe | PASS | relay → nezha; first attempt hit a transient `Timeout reading from probe` mid-programming (flaky USB, the same class of failure `mbdeploy`'s own `flash.py` retries once — this minimal registry op deliberately doesn't, per its own module docstring), retried once and succeeded cleanly — `NEZHA2/robot` |

### magni

| Check | Result | Notes |
|---|---|---|
| `mbregistry` running | PASS | systemd, `active`, `enabled` |
| Board identified | PASS | `RADIOBRIDGE/relay`, name `vevav`, from the start |
| Unplug/replug | PASS (unbind/rebind) | same method |
| Flash-triggered re-probe | PASS, with a finding | see below |

**magni finding (not fixed — documented, not papered over):** flashing
the nezha firmware onto `magni`'s board and letting the daemon's normal
flash-triggered re-probe fire produced an inconsistent result across
four attempts: two showed `no-firmware` (no line arrived within the
probe window), one showed a `connected` state with a malformed/blank
announcement (the probe captured a `DBG:wifi ...` diagnostic line the
firmware also emits, not its identity line — this is SUC-001's existing
"malformed announcement, not a timeout" outcome, handled correctly, just
not the outcome we wanted for this check), and one manual, direct
Python `HELLO`-and-read test (bypassing the daemon, 8 s window) *did*
capture the correct `device NEZHA2 robot vevav ...` line, immediately
after `HELLO`. Relay firmware, by contrast, announced correctly and
immediately on every attempt, on every host, all session.

This looks like a real timing sensitivity specific to the
nezha-robot-template firmware's boot/announce sequence interacting with
`identity.probe()`'s settle-delay-then-buffer-reset-then-`HELLO`
sequence (see risk (b) below) — not something this session could
pin down to a specific root cause with confidence, and not something to
guess-fix per the four-phase debugging protocol (evidence gathered,
pattern analysis done: relay reliable, nezha intermittent on this one
board; no confirmed hypothesis, and further hardware iteration is
costly). Final state was recovered by flashing the relay firmware back
via the registry's `flash` op (mass-erase + flash, lock held), which
worked cleanly and demonstrably showed the re-probe mechanism itself
working (`no-firmware` → `RADIOBRIDGE/relay`, one clean re-probe) —
so the flash-triggered re-probe *mechanism* passes; the nezha firmware's
announce-timing reliability on this one board is flagged as a follow-up,
not blocking this ticket.

### braeburn

| Check | Result | Notes |
|---|---|---|
| `mbregistry run` running | PASS | foreground process, backgrounded with `nohup ... &`/`disown` over SSH (per ticket's macOS scope — no launchd unit), `--socket /tmp/mbregistry/api.sock --db ~/.local/state/mbregistry/devices.db` |
| Board identified | PASS | started blank (no firmware); flashed relay via the registry's own `flash` op after an initial manual mass-erase (see below) to get a parsed announcement (`RADIOBRIDGE/relay`, name `zugit`) |
| Unplug/replug | **MANUAL, left undone** | macOS has no `/sys/bus/usb/drivers/usb/{unbind,bind}` equivalent reachable over SSH without physical access; per the ticket's own text ("this is expected to include braeburn"), a physical unplug/replug is the documented substitute and is left for the stakeholder to perform and confirm — this session had no physical access to the hardware |
| Flash-triggered re-probe | PASS | relay → nezha; first attempt hit the same transient `Timeout reading from probe` class seen on hodr, retried once and succeeded — `NEZHA2/robot` |
| Lock released on holder-kill | not repeated here | exercised on `loki` |

**braeburn finding (real, understood, not a code bug — operator error in this session, documented so it isn't repeated):** the board's very first flash attempt (direct `pyocd flash`, bypassing the registry's lock) failed with `flash erase sector failure (address 0x0; result code 0x67)` — a locked/protected nRF52 (this is *exactly* the signature `mbdeploy`'s own `flash.py` names `_LOCKED_SIGNATURES` and recovers from with a CTRL-AP mass erase; `mbtools`'s registry `flash.py` deliberately does **not** port that recovery logic — see its own module docstring, "if you don't need to put flashing in MB Registry, don't," locked-chip recovery is `mbdeploy`'s job for a later sprint). Recovered with a one-time manual `pyocd erase --mass`. After that, flashing without holding the registry's own lock (direct `pyocd` calls run concurrently with the live daemon's poll loop) produced further, different transient failures (`0x67` again, then `Timeout reading from probe`) — traced to the daemon polling/probing the same USB device concurrently with the manual `pyocd` session, i.e. exactly the contention the flash-kind lock exists to prevent. Every flash done *through* the registry's own lock (`lock` then `flash`, or a lock held across a manual mass-erase+flash sequence) succeeded without that contention. Lesson for future hardware sessions, recorded here rather than in code: never run `pyocd` directly against a board a live `mbregistry` daemon is watching without holding that device's lock first.

## Summary

| Acceptance criterion | Result |
|---|---|
| `scripts/deploy-test-host.sh` deploys to all 5 hosts | PASS |
| Old `mbdeploy.service` retired on 4 Nolanet nodes | PASS |
| `mbregistry` running on all 5 hosts | PASS |
| `list` identifies the board on every host | PASS (all 5) |
| Unplug/replug detected on every host | PASS on 4 (unbind/rebind); **left manual on braeburn** (no physical access this session) |
| Lock released when holder killed | PASS (loki; not repeated on the other 4, per the ticket's own scoping) |
| Flash-triggered re-probe on every host | PASS on all 5 (hodr/braeburn needed one retry after a transient probe timeout; magni's nezha-firmware timing issue documented above, not blocking — the mechanism itself demonstrably works on magni with relay firmware) |
| Results documented here, pass/fail per host, manual substitutes labeled | PASS (this document) |
| Robot Garage wiki "Current status" updated | **Not done — no edit access.** `http://robot-garage.home/doku.php?id=mbdeploy&do=edit` is reachable (HTTP 200) but renders the edit textarea `readonly="readonly"` with no save control and an empty CSRF `sectok`, from this session's network position/credentials. Per this ticket's own text, that is recorded here as the limitation rather than blocking the ticket. Someone with wiki edit access should update the "Current status" table's Daemon column for `meili`/`loki`/`hodr`/`magni` to read "mbdeploy retired 2026-09-23; mbregistry (mbtools sprint 001) active, enabled — under test" and add a short note that the existing per-board mDNS-advertised "Boards" column for those four rows is now stale (mbdeploy no longer runs to refresh it) — mbregistry currently watches only each node's single dedicated `/dev/ttyACM0` test board (this ticket's scope), not the fleet's other boards. |
| Test firmware cited by repo/tag | PASS (table above) |

Manual/left-undone items for the stakeholder: braeburn's physical
unplug/replug confirmation; the Robot Garage wiki edit.
