---
id: '005'
title: 'Hardware acceptance: watch and unlock --force'
status: done
use-cases:
- SUC-001
- SUC-002
- SUC-003
- SUC-004
depends-on:
- '001'
- '002'
- '003'
- '004'
github-issue: ''
issue: mbregistry-api-for-robot-console-watch-lock-label-unlock-force-local-stream.md
completes_issue: true
---
<!-- CLASI: Before changing code or making plans, review the SE process in CLAUDE.md -->

# Hardware acceptance: watch and unlock --force

## Description

Manual hardware verification of this sprint's two riskiest interactions
— a live `watch` client observing real USB attach/detach, and
`unlock --force` actually reaching a *different, live* client's
connection and closing it — on real boards, following this project's
existing hardware-acceptance convention (`docs/acceptance/00N-hardware.md`,
one new `docs/acceptance/008-hardware.md`).

**Hosts**: any two of `meili`/`loki`/`hodr`/`magni`/`braeburn` (per
CLAUDE.md — passwordless `sudo`, one dedicated micro:bit each). **Do
not** use `torture`'s relays for this (CLAUDE.md's standing rule: shared,
scarce hardware, not a routine test target).

Scenario 1 — **watch observing a real attach/detach**: on one host,
start `mbregistry run` (or use the running service), open a `watch`
connection (a small script or `nc`/`socat` against the local socket is
sufficient — no new tooling required), physically unplug and replug that
host's dedicated micro:bit, and confirm `attach`/`detach` JSON lines
arrive in the expected order with no ZeroMQ client involved.

Scenario 2 — **`unlock --force` against a live holder**: from one
session, `lock` the board (`serial` kind) and leave that connection
open (e.g. a `stream` session, once ticket 004 has landed, or a plain
held `lock` connection); from a second, independent session on the same
host, run `mbregistry unlock --force <uid>`; confirm the first
session's connection observes EOF/a connection error essentially
immediately, and that `mbregistry list` shows the board unlocked
afterward.

If either scenario surfaces a real bug (not a known, already-documented
quirk from CLAUDE.md's hardware-quirks list), fix it as part of this
ticket, the same way prior sprints' hardware tickets did (e.g. sprint
005 ticket 011, sprint 007's own hardware pass).

## Acceptance Criteria

- [x] A `watch` client on a real host observes `attach` immediately
      after a physical plug-in and `detach` immediately after unplug,
      for that host's dedicated board.
- [x] `mbregistry unlock --force <uid>` against a lock held by a live,
      separate connection on the same host releases the lock and that
      connection observes EOF/an error; `mbregistry list` confirms the
      board is unlocked afterward.
- [x] Neither scenario touches `torture`'s relays.
- [x] `docs/acceptance/007-hardware.md` records both scenarios, the
      hosts/boards used, and the outcome (including any quirk found and
      whether it needed a code fix, per this project's existing
      acceptance-doc convention). (Written as `007-hardware.md`, not
      `008-hardware.md` as this ticket's text assumed — sprint 007's own
      hardware doc was already numbered `006-hardware.md`, not
      `007-hardware.md`, so `007` was the next unused number on disk;
      the project's acceptance docs are numbered sequentially across the
      whole doc set, not per sprint number.)
- [x] Any real bug found is fixed and covered by a regression test in
      the automated suite, not left as a manual-only finding. (No real
      bug was found — see `docs/acceptance/007-hardware.md`'s
      "Scenario 2" section for the one anomaly investigated and ruled
      out as CLI startup latency, not a code defect.)

## Testing

- **Existing tests to run**: `uv run pytest` (full suite) before
  starting the hardware pass, to confirm the branch is otherwise green.
- **New tests to write**: none required if hardware behaves as
  designed; a regression test in `tests/registry/` for any real bug
  this pass finds.
- **Verification command**: `uv run pytest`

## Implementation Notes

- Full details, real commands, and real output for both scenarios are
  in `docs/acceptance/007-hardware.md`. Summary: both scenarios PASS,
  on `loki` and `magni` (not `hodr` — see below), no real bug found.
- **Pre-flight full suite**: 13 failures, all `zmq.error.ZMQError:
  Address already in use (addr='tcp://*:7442')` in
  `tests/registry/peering/test_peering.py`, reproducing in isolation
  and traced to this dev Mac's own real, do-not-touch `mbregistry`
  LaunchAgent already bound to the project's default ports. Pre-
  existing (`test_peering.py` last touched sprint 010), unrelated to
  this sprint. One additional single-test failure in the very first
  full run (`test_api.py::test_watch_fans_out_to_every_connected_watcher`)
  did not reproduce in isolation or in a second scoped
  `tests/registry/` run — treated as a local flake, not a regression.
- **Host swap**: `hodr` (the natural second host, since `meili`/`loki`
  were already used/quirk-fixed in sprint 007) turned out to have no
  default route at all (`ip route` shows only local subnets, `ping
  8.8.8.8` → "Network is unreachable") — a real, pre-existing
  host/network condition, out of this ticket's scope to fix. Its
  `mbregistry.service`, stopped as the first step of the aborted
  deploy, was restarted to its prior (not-yet-redeployed) state.
  `loki` and `magni` were used instead.
- **Quirk re-confirmed, not new**: `magni`'s bare `mbregistry` CLI still
  resolved to the stale `/opt/mbtools` shadow (CLAUDE.md's existing
  quirk note); re-pointed `/usr/local/bin/{mbregistry,mbdeploy,
  mbserial,mbrelay}` to the fresh venv the same way CLAUDE.md already
  documents for `meili`/`loki`. No CLAUDE.md update needed — the
  existing note already tells the next session to check every host.
- **Scenario 1** (`watch`): a local-socket watcher on `loki` and a
  remote-TCP watcher (port 7440) from `magni` both observed the same
  `detach`/`attach`/`identity` sequence, within 4ms of each other,
  across a real USB unbind/rebind (`1-1.3`) of `loki`'s own board
  (`togov`, `fe9a0254`).
- **Scenario 2** (`unlock --force`): locked+streamed `togov` on `loki`'s
  local socket from one session; `unlock fe9a0254 --force` from a
  second session released it and the streaming session's blocked read
  observed a clean EOF. One apparent anomaly (a ~3.9s gap between
  issuing the SSH command and the observed EOF) was investigated per
  this project's debugging protocol and traced entirely to `mbregistry
  unlock`'s own ~4.4s CLI-startup time on this Pi (heavy imports:
  `pyocd`/`cmsis-pack-manager`/`zeroconf`) — timing from the *end* of
  the CLI's own execution instead showed the client's EOF arriving
  *before* that measurement point, confirming
  `_op_force_unlock`'s `conn.shutdown(SHUT_RDWR)` unblocks the holder
  synchronously, as designed. No code change made; no regression test
  added, since nothing regressed.
- **Cleanup note for future sessions**: two throwaway `while pgrep -f
  <script>.py; do sleep 1; done` polling loops (used to detect when a
  backgrounded test script had exited) never exited on their own,
  because `pgrep -f` matched each loop's *own* command line (which
  contains the same `.py` filename as text). Found via `pgrep -af` and
  killed by PID; not an `mbtools` bug, just this session's own shell
  scripting mistake — recorded so a stray `bash -c "while pgrep..."`
  process isn't mistaken for something `mbregistry` left behind.
- Final state: `loki`/`magni` running `mbtools==0.20260924.6` (this
  sprint's build), `mbregistry.service` active, dedicated boards
  `free`/`local`; `hodr` running its prior build, active, otherwise
  untouched; `meili`/`braeburn` untouched; `torture` untouched
  throughout.
