---
id: '005'
title: 'Spawn support (--ready-json, --exit-with-parent, --no-peering)'
status: open
use-cases: [SUC-007]
depends-on: ['004']
github-issue: ''
issue: robot-console-on-mbregistry-multi-instance-and-spawn-support.md
completes_issue: true
---
<!-- CLASI: Before changing code or making plans, review the SE process in CLAUDE.md -->

# Spawn support (--ready-json, --exit-with-parent, --no-peering)

## Description

Make `mbregistry run` usable as a short-lived child process, per
`docs/design/robot-console-integration.md` §4 item 4's spawn recipe.
Depends on ticket 004 because the ready-json line reports the resolved
`--instance` name and every bound port (`--pool-port`/`--names-port`
among them), so the reporting mechanism needs those values to already
be resolvable the way ticket 004 leaves them.

**`--ready-json`**: once every listener in `_run_registry` is started
(`api.start()`, `remote_api.start()`, `peering.start()` if not
`--no-peering`, `relay_pool.start()` if not `--no-relay-pool`,
`names_api.start()`), print exactly one JSON line to stdout:
`{"ready": true, "instance": <resolved --instance>, "socket":
<socket_path or pipe name>, "ports": {"remote": ..., "peer_pub": ...,
"peer_snapshot": ..., "pool": ..., "names": ...}}` (include only the
ports actually applicable — e.g. omit `peer_pub`/`peer_snapshot` when
`--no-peering`, `pool` when `--no-relay-pool`). This must be the only
thing this run prints to stdout under `--ready-json` (existing
diagnostic prints go to stderr already, per `_run_registry`'s existing
`file=sys.stderr` print — keep that convention so `--ready-json`'s
stdout stays parseable).

**`--exit-with-parent`**: start a background thread/selector watching
`sys.stdin` for EOF (the parent closing its end, or the parent process
dying, both surface as EOF on an inherited pipe). On EOF, set the same
`stop_event` `_run_registry`'s POSIX-signal handlers already set,
so shutdown goes through the identical clean-stop path `SIGTERM` uses
today — no separate shutdown code path to maintain.

**`--no-peering`**: skip constructing `PeerDiscovery` entirely in
`_run_registry` (not "construct then never start," and not "start then
immediately stop") — `peering` becomes `None` for the rest of that
function's scope, and every place that currently does
`peering.publish_name_set`/`peering.publish_name_clear` (passed to
`assemble_names_api`) needs a `None`-safe fallback (e.g. a no-op
callback, or `assemble_names_api` already tolerating `None` callbacks —
check its actual signature/behavior at implementation time).

## Acceptance Criteria

- [ ] `--ready-json` prints exactly one valid JSON line, after every
      requested listener is bound, before the daemon's poll loop starts;
      the line's `ports` reflect actually-bound values (consistent with
      ticket 004's "advertise what's bound" fix).
  - [ ] No other stdout output occurs under `--ready-json` (diagnostic
        text stays on stderr).
- [ ] `--exit-with-parent` causes the process to exit within the test's
      timeout when the parent's end of stdin is closed, via the same
      clean-shutdown path `SIGTERM` already takes (verified by asserting
      the same `stop_event`/shutdown sequence runs, not a separate
      `os._exit`-style hard kill).
- [ ] `--no-peering` results in `PeerDiscovery` never being constructed
      — a test asserts the peering-construction call is never made
      (not merely that `start()`/`stop()` become no-ops), and that no
      mDNS registration or ZeroMQ socket bind occurs.
- [ ] `--no-peering` combined with `assemble_names_api`'s
      `name_set_callback`/`name_clear_callback` does not raise (handles
      the missing `peering` object gracefully).
- [ ] All three flags compose correctly together (the combination in
      `docs/design/robot-console-integration.md` §4 item 4's own spawn
      recipe: `--ready-json --exit-with-parent --no-peering` alongside
      `--pool-port 0 --names-port 0 --remote-port 0 --peer-pub-port 0
      --peer-snapshot-port 0`), exercised as one integration-style test.

## Implementation Plan

**Approach**: `--ready-json` first (purely additive, no control-flow
change), then `--no-peering` (a conditional-construction change), then
`--exit-with-parent` (a new background watcher thread) — this order
keeps each change's blast radius on `_run_registry` small and
independently testable.

**Files to modify**:
- `src/mbtools/registry/cli.py`: `--ready-json`, `--exit-with-parent`,
  `--no-peering` argparse additions; `_run_registry` restructured per
  Description above (conditional `PeerDiscovery` construction, the
  ready-json print point, the stdin-EOF watcher thread wired to
  `stop_event`).

**Testing plan**:
- `tests/registry/cli/`: `--ready-json` output-shape test (mocking
  every `assemble_*`/`.start()` call the way existing `cmd_run`/
  `_run_registry` tests already do, per that test file's existing
  fixture pattern); `--no-peering` construction-skipped test;
  `--exit-with-parent` stdin-EOF-triggers-stop_event test (a fake stdin
  the test can close on demand, asserting `stop_event.is_set()` becomes
  true and the function returns without hanging the test).
- An end-to-end-ish test combining all three flags against fully mocked
  listener assembly (no real sockets/ports), matching the design doc's
  spawn recipe shape.
- Run: `uv run pytest tests/registry/cli/ -x`.

**Documentation updates**: none in this ticket — ticket 006 covers docs
for the whole sprint.
