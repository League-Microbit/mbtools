# Ticket 010 — Hardware acceptance on Nolanet and braeburn

Sprint 002 (`Local clients: mbdeploy flash-by-name and mbserial connect`),
ticket `010-hardware-acceptance-on-nolanet-and-braeburn`. Run 2026-09-23
against the same five dedicated test hosts as
`docs/acceptance/001-hardware.md`: `meili`, `loki`, `hodr`, `magni`
(Debian 13 aarch64, user `eric`, passwordless sudo) and `braeburn` (macOS 15
x86_64). No micro:bits on the dev Mac; every check below ran over SSH
(`ssh -o BatchMode=yes <host>`).

Deploy path used throughout: `scripts/deploy-test-host.sh <host>` (sprint
001's script, unchanged this sprint). On each Nolanet node,
`mbregistry.service` was stopped, the stale tmpfs/venv from a previous run
was `sudo rm -rf`'d (root-owned `__pycache__` from the running-as-root
daemon — the same operational note sprint 001 recorded), the script re-run,
then the service restarted. On `braeburn`, the existing `nohup`'d
`mbregistry run` process was killed and relaunched from the freshly
installed venv. All five hosts ended this session running this sprint's
code (`mbtools-0.20260923.1`, with the `registry.client`/`registry.render`/
`deploy.flash`/`deploy.release`/`serial.connect` modules this sprint adds).

## Firmware used

Fetched by `mbdeploy deploy --repo`, real GitHub release fetch (not `gh
release download`, not a local `--hex` file), newest non-`latest` versioned
release:

| Firmware | Repo | Tag | Announces as |
|---|---|---|---|
| Radio relay | `League-Robotics/microbit-radio-relay` | `v0.20260913.2` | `DEVICE:RADIOBRIDGE:relay:<name>:<serial>` |
| Nezha robot | `League-Microbit/nezha-robot-template` | `v0.20260919.7` | `device NEZHA2 robot <name> <serial>` |
| Remote joystick | `League-Microbit/Remote-Joystick-Student` | `v0.20260922.1` | `DEVICE:JOYSTICK:joystick:<name>:<serial>` — the colon dialect, resolved this session (`CLAUDE.md`'s "(check on first probe)" placeholder updated) |

All five boards started this session already flashed (from prior sprint
work): `meili`/`loki`/`hodr`/`braeburn` with `NEZHA2/robot`, `magni` with
`RADIOBRIDGE/relay`.

## Operational finding: local pyOCD/serial access needs `sudo` on the Nolanet nodes

Not a code bug — recorded here because it blocked the first attempt on
every Linux host and needs to be known before the next hardware session.

`mbdeploy deploy`/`mbdeploy debug` (client-side `pyocd`, via
`deploy.flash`) and `mbserial` (client-side raw serial open, via
`serial.connect`) both open the board directly from the *invoking user's*
process — unlike `mbregistry.service`, which already runs as root
specifically because pyOCD needs raw USB access
(`docs/acceptance/001-hardware.md`). On `meili`/`loki`/`hodr`/`magni`,
`/dev/ttyACM0` is `root:plugdev` and `eric` is not in the `plugdev` group,
and no udev rule grants CMSIS-DAP access either — so every plain (no-sudo)
`mbdeploy deploy`, `mbdeploy debug`, and `mbserial` invocation on these four
hosts fails: pyOCD logs a stream of `[Errno 13] Access denied ...` USB
warnings and blocks forever (`Waiting for a debug probe matching unique ID
'...' to be connected...`, never satisfied, never timing out) with a plain
serial open raising `PermissionError` immediately. `sudo <venv>/bin/mbdeploy
...` / `sudo <venv>/bin/mbserial ...` fixed every case below. `braeburn`
(macOS) needed no `sudo` for either tool — confirmed directly (a `--repo`
deploy and an `mbserial` connect both ran as plain `eric`).

This is host provisioning (missing `plugdev` membership / udev rule), not a
`mbtools` defect, and out of this ticket's "touch nothing else on these
hosts" scope to fix by editing host groups. It is, however, adjacent to a
real gap worth flagging as a follow-up rather than guessing a fix for
(four-phase debugging protocol: evidence gathered, no confirmed hypothesis
about the right fix): `deploy.flash._run_streamed`'s `subprocess.Popen` has
no timeout on the `pyocd` child, so the *specific* failure mode above (pyocd
retrying an inaccessible probe indefinitely) makes `mbdeploy deploy`/`debug`
hang forever instead of failing fast — inconsistent with this codebase's
otherwise-consistent "fail fast, no blocking wait" convention (SUC-005, the
lock path). Not fixed here: a bounded subprocess timeout risks truncating a
legitimately slow real flash, and the actual root cause (host permissions)
already has a documented, immediate workaround (`sudo`). Recorded here as a
finding for a future ticket to decide, not guessed at.

## Checks, with host(s) exercised

Legend: PASS / FAIL / MANUAL.

### `mbdeploy deploy <name> --repo <owner/repo>` — all three firmware repos, real GitHub fetch

| Firmware repo | Host | Result | Notes |
|---|---|---|---|
| `nezha-robot-template` | `loki` (`vitut`) | PASS | `sudo mbdeploy deploy vitut --repo League-Microbit/nezha-robot-template` — streamed erase/program output, fetched `v0.20260919.7`, re-announced `NEZHA2` (`flash_count=1`) |
| `nezha-robot-template` | `meili` (`gitev`) | PASS | same repo/tag, `flash_count=3` (board's cumulative count this session) |
| `nezha-robot-template` | `braeburn` (`zugit`) | PASS | ran as plain `eric`, no `sudo` needed (macOS) |
| `microbit-radio-relay` | `magni` (`vevav`) | PASS, with a real recovery exercised | see below |
| `Remote-Joystick-Student` | `hodr` (`togov`) | PASS | fetched `v0.20260922.1`, re-announced `JOYSTICK` (`flash_count=1`) — resolved the "announces as (check on first probe)" placeholder in `CLAUDE.md` |

**Relay guard + mass-erase recovery, on `magni`:** `vevav` was already
`RADIOBRIDGE/relay` at the start of this run. `mbdeploy deploy vevav --repo
League-Robotics/microbit-radio-relay` (no `--force-relay`) correctly
refused: `mbdeploy: vevav is a relay/bridge (RADIOBRIDGE) -- use
--force-relay to flash it anyway.` (exit 1). With `--force-relay`, the
first flash attempt hit a real `flash erase sector failure (address
0x00000000; result code 0x67)` — the exact locked-part signature
`deploy.flash._LOCKED_SIGNATURES` matches — and `flash_hex` recovered
automatically: `Mass erasing device...` / `Mass erase complete`, reflash,
success, re-announced `RADIOBRIDGE` (`flash_count=1`). The mass-erase
recovery path (ticket 005) is proven against real hardware, not just its
own fake-runner unit tests, by this one exchange.

Every deploy above streamed pyOCD's erase/program progress live (not
buffered to the end), verified via the normal pyOCD post-flash check, and
reported the new announcement — matching the acceptance criterion in full.

### `mbdeploy list` vs `mbregistry list` parity (SUC-003)

Checked on all five hosts, both table and `--json` output.

| Host | Table match | JSON match |
|---|---|---|
| meili | PASS (identical) | not separately diffed (table already identical; see loki for the JSON check) |
| loki | PASS (identical) | PASS — `diff <(mbregistry list --json) <(mbdeploy list --json)` empty |
| hodr | PASS (identical) | not separately diffed |
| magni | PASS (identical) | not separately diffed |
| braeburn | PASS (identical) | not separately diffed |

Table output (STATE/NAME/UID/FIRMWARE/PORT columns, column widths, cell
formatting) was byte-identical between the two commands on every host —
expected by construction (`registry.render`'s functions are shared, not
duplicated, per sprint.md's Architecture), confirmed against real device
data rather than only unit-tested fixtures. The `--json` diff on `loki` is
the one host where both commands' *raw* output (not just a visual
table) was directly diffed byte-for-byte; not repeated on the other four
since the table check already confirms the same underlying fetch/render
path.

### `mbserial <name>` no-reboot-by-default, `--reset` does reset (SUC-004)

Exercised on both platform branches this sprint's `serial.connect` module
introduces (Linux BREAK vs. macOS reopen):

**`loki` (Linux, BREAK path):**
- `(sleep 3) | sudo mbserial vitut` (no `--reset`): connected, printed
  `connected to vitut at 115200 baud -- Ctrl-D or Ctrl-C to exit`, then
  **silence** for the full 3s window — no boot banner, no `device NEZHA2
  robot ...` line. `mbregistry list` immediately after showed `free`
  (lock released cleanly on session end).
- `(sleep 3) | sudo mbserial vitut --reset`: same connect, then
  **immediately** a full boot sequence — `device NEZHA2 robot vitut
  2198604104`, `boot tests ready`, `boot radio vitut ch 41 grp 30`, the
  `boot verbs: ...` help line, `boot cal none stored`, then periodic
  `DBG:wifi ...` lines — unambiguous evidence of a real reboot.

**`braeburn` (macOS, reopen path):**
- Same no-`--reset`/`--reset` pair against `zugit`: no-`--reset` produced
  silence (no boot banner) for the 3s window; `--reset` produced the
  identical boot-sequence shape (`device NEZHA2 robot zugit 3764434439`,
  `boot tests ready`, ..., periodic `DBG:wifi` lines).

Result: **PASS on both hosts, both platform branches** — connecting
without `--reset` demonstrably does not reboot the board (no boot banner
where a reset would produce one, immediately and reliably), and `--reset`
demonstrably does (the same distinctive boot banner appears every time it
is passed, on both the BREAK and reopen code paths).

A longer (15s) no-`--reset` session on `loki` (held open for the busy-lock
check below) also never printed a boot banner, only the same periodic
`DBG:wifi ...` line the reset case eventually settles into — further
evidence against a delayed/missed reboot on the no-reset path, not just a
window-too-short artifact.

### Busy-lock fail-fast: holder kind + PID (SUC-005)

Exercised on `loki`. Held a `serial`-kind lock on `vitut` from one
backgrounded session (`(sleep 15) | sudo mbserial vitut`, PID `2568665`),
then from a second, concurrent session:

- `sudo mbserial vitut` → `mbserial: vitut is locked for serial by pid
  2568665` (exit 5, `EXIT_LOCKED`).
- `sudo mbdeploy deploy vitut --hex /nonexistent.hex` → `mbdeploy: vitut is
  locked for serial by pid 2568665` (exit 5) — same holder identified
  correctly from a *different* tool than the one holding the lock, and it
  never got far enough to notice the bogus hex path, confirming the lock
  check runs before hex resolution matters for this failure mode.

`mbregistry list` while the lock was held independently showed `locked by
serial pid 2568665`, matching both fail-fast messages exactly. When the
holding session ended (15s sleep elapsed, normal Ctrl-D-equivalent EOF
exit), the lock was released automatically — confirmed `free` again with
no manual intervention. **PASS**, one host, one lock kind, per the ticket's
own "exercise the mechanism on one host" scoping precedent from sprint 001.

### `mbdeploy debug` (SUC-006's debug passthrough)

Exercised on `loki`, twice — once via a deliberate interrupt, once to a
clean completion, to cover both of `_run_debug`'s unlock paths (module
docstring: "the lock is released ... success, failure, or a Ctrl-C"):

1. `sudo mbdeploy debug vitut -- reset --uid <wrong-length-uid>` — a typo'd
   `--uid` left pyOCD waiting forever for a probe ID that doesn't exist
   (same "no probe" hang shape as the sudo-less case above, this time by
   operator error rather than a permissions gap). `mbregistry list`
   confirmed the `debug`-kind lock was held (`locked by debug pid ...`)
   for the whole time it waited. `sudo kill -INT` on the `mbdeploy`/`pyocd`
   process pair reproduced a Ctrl-C: `mbregistry list` immediately after
   showed `free` again — the lock was released on the interrupted path,
   not leaked.
2. `sudo mbdeploy debug vitut -- reset` (no `--uid`, the host's one probe
   used by default) — completed cleanly, exit 0, `mbregistry list`
   confirmed `free` immediately after — the lock was released on the
   normal-completion path too.

**PASS** — both the debug-kind lock's acquisition (visible mid-flight) and
its release (on both an interrupted and a clean pyOCD invocation) are
confirmed against real hardware.

### Magni's nezha-firmware post-flash timing finding — re-tested (ticket 004's bounded extra HELLO retry)

`docs/acceptance/001-hardware.md` recorded, on this exact board (`magni`,
board `vevav`, then named `vevov`), a **1-out-of-4** capture rate for the
nezha firmware's post-flash announcement within the daemon's probe window
(two `no-firmware`, one malformed-announcement capture, one successful
capture only via a manual, longer, direct-Python retry) — the daemon's own
probe window was the identified gap, and ticket 004 added one bounded extra
`HELLO`-and-read window specifically to close it.

Re-test this session, same board, same four-attempt pattern, each attempt
a full `mbdeploy deploy vevav --repo League-Microbit/nezha-robot-template
--force-relay` (`--force-relay` only needed once, to get off the relay
firmware the guard would otherwise block):

| Attempt | Result | `mbdeploy`'s own report |
|---|---|---|
| 1 | **captured** | `vevav re-announced as NEZHA2 (flash_count=2)` |
| 2 | **captured** | `vevav re-announced as NEZHA2 (flash_count=3)` |
| 3 | **captured** | `vevav re-announced as NEZHA2 (flash_count=4)` |
| 4 | **captured** | `vevav re-announced as NEZHA2 (flash_count=5)` |

**Result: 4-out-of-4 (100%) this session**, against 1-out-of-4 (25%, and
that one only via a manual bypass of the daemon's own probe) in sprint
001. Every attempt this session captured the announcement through the
ordinary `mbdeploy deploy` flow — `_wait_for_reprobe`'s own default 15s
timeout, waiting on the daemon's probe pipeline — with no manual
intervention and no timeout. This is consistent with (not proof of, on a
sample of 4) ticket 004's bounded extra `HELLO` retry having closed the
gap sprint 001 found: **closed**, not "still flaky" or "inconclusive",
based on this session's evidence. A larger sample on a future hardware pass
would strengthen this further, but four fully-successful attempts against
a firmware/board combination that previously failed 3-out-of-4 is a
material, not marginal, result.

## Full test suite

`uv run pytest -q`: **319 passed, 2 skipped**, both before this session's
hardware pass and again after (no source changes were made — see below) —
confirms the hardware run itself didn't rely on any code that regressed
the existing suite, and that no code fix was needed to make the above
checks pass.

## Code fixes made during this pass

**None.** Every check above passed against the sprint's existing code,
including the one true stress case (the mass-erase recovery on `magni`'s
`0x67` failure) and the magni re-test this ticket specifically called out
as a required, not-guaranteed-to-pass check. The one real finding (`sudo`
required for local pyOCD/serial access on the four Nolanet nodes, and the
adjacent unbounded-hang-without-it observation) is host provisioning, not
a `mbtools` defect — documented above and in `CLAUDE.md`'s "Hardware test
targets" section rather than "fixed" with a speculative code change, per
the four-phase debugging protocol's "don't guess-fix" guidance. Per this
ticket's own acceptance criteria, that check is satisfied as "no fix
needed" rather than skipped.

## Summary

| Acceptance criterion | Result |
|---|---|
| `scripts/deploy-test-host.sh` deploys to all 5 hosts | PASS |
| `mbdeploy deploy --repo` flashes via real GitHub fetch, streamed/verified/reported | PASS (all three repos, at least one host each; magni exercised both the relay guard and mass-erase recovery for real) |
| `mbdeploy list` / `mbregistry list` parity on real data | PASS (table on all 5 hosts; `--json` byte-diffed on loki) |
| `mbserial` no-reboot by default; `--reset` does reset | PASS (both platform branches: loki/BREAK, braeburn/reopen) |
| Busy-lock fail-fast (holder kind + PID) | PASS (loki, `serial`-kind lock, blocked both `mbserial` and `mbdeploy`) |
| `mbdeploy debug` exercised | PASS (loki; both interrupted and clean-completion unlock paths) |
| Magni's nezha-firmware timing finding re-tested | PASS — **closed**: 4/4 captured this session vs. 1/4 in sprint 001 |
| Every result recorded here, PASS/FAIL/MANUAL, manual substitutes labeled | PASS (this document); nothing had to be left MANUAL this session — real hardware was available and cooperative throughout |
| Any code fix committed with a regression test | N/A — no code fix was needed; the one finding (sudo requirement) is host provisioning, documented, not a code change |

No item in this pass was left MANUAL. The `sudo`-required finding and the
adjacent unbounded-pyocd-hang-without-it observation are the two items
worth a future ticket's attention; neither blocks this ticket, and both
are documented above and in `CLAUDE.md`.
