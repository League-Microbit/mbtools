---
id: '005'
title: Spawn support (--ready-json, --exit-with-parent, --no-peering)
status: done
use-cases:
- SUC-007
depends-on:
- '004'
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

- [x] `--ready-json` prints exactly one valid JSON line, after every
      requested listener is bound, before the daemon's poll loop starts;
      the line's `ports` reflect actually-bound values (consistent with
      ticket 004's "advertise what's bound" fix).
  - [x] No other stdout output occurs under `--ready-json` (diagnostic
        text stays on stderr).
- [x] `--exit-with-parent` causes the process to exit within the test's
      timeout when the parent's end of stdin is closed, via the same
      clean-shutdown path `SIGTERM` already takes (verified by asserting
      the same `stop_event`/shutdown sequence runs, not a separate
      `os._exit`-style hard kill).
- [x] `--no-peering` results in `PeerDiscovery` never being constructed
      — a test asserts the peering-construction call is never made
      (not merely that `start()`/`stop()` become no-ops), and that no
      mDNS registration or ZeroMQ socket bind occurs.
- [x] `--no-peering` combined with `assemble_names_api`'s
      `name_set_callback`/`name_clear_callback` does not raise (handles
      the missing `peering` object gracefully).
- [x] All three flags compose correctly together (the combination in
      `docs/design/robot-console-integration.md` §4 item 4's own spawn
      recipe: `--ready-json --exit-with-parent --no-peering` alongside
      `--pool-port 0 --names-port 0 --remote-port 0 --peer-pub-port 0
      --peer-snapshot-port 0`), exercised as one integration-style test.

## Implementation Notes

Implemented entirely in `src/mbtools/registry/cli.py`:

- `assemble_registry` gained a `no_peering: bool = False` keyword-only
  parameter. When `True`, the `PeerDiscovery(...)` construction call is
  skipped entirely (not called, not started-then-stopped), the
  function's own fourth return value is `None`, and
  `assemble_daemon_and_api`'s `event_callback`/`lock_display_callback`/
  `name_set_callback`/`name_clear_callback` are all passed `None`
  instead of a bound method off a nonexistent `peer_discovery`. Every
  pre-ticket-005 caller/test that omits `no_peering` is unaffected
  (defaults to `False`, unchanged behavior).
- `_run_registry` threads `args.no_peering` into `assemble_registry`,
  and is `None`-safe everywhere it touches the resulting `peering`
  local: `peering.start()`/`.stop()`/`.connect_peer(...)` and the
  `assemble_names_api` callback wiring are all guarded with
  `if peering is not None`. A `--peer` given alongside `--no-peering`
  is a no-op (nothing to connect through), noted to stderr only.
- `--ready-json` builds a `ports` dict from each listener's own
  `bound_port` (`remote_api`, `relay_pool` if not `--no-relay-pool`,
  `names_api`) plus the *resolved* `peer_pub_port`/`peer_snapshot_port`
  when peering is enabled (`PeerDiscovery` exposes no bound-port
  equivalent for its own ZeroMQ sockets to read back from -- it is
  "existing, unmodified" this sprint per sprint.md Step 3 -- so those
  two report the requested value, matching the pre-existing stderr
  diagnostic line's own behavior). The resolved `--instance` is reported
  directly when given, else via a new small `_short_hostname()` helper
  (a third copy of `registry.peering`'s own private helper of the same
  name, following that module's own "duplicate a small per-module
  helper rather than cross-import a private name" precedent). Printed
  once, to stdout only, immediately before `daemon.run(...)`.
- `--exit-with-parent` starts a new daemon thread
  (`_watch_stdin_for_parent_exit`) that blocks reading `stdin` until EOF,
  then sets the exact same `stop_event` `cmd_run`'s own `SIGTERM`/
  `SIGINT` handlers already set -- shutdown always goes through the one
  path `daemon.run`'s own `stop` callback checks in `_run_registry`'s
  `finally` block.
- **Test seam added** (per this ticket's own handoff note that one was
  needed): `_run_registry` gained one new keyword-only parameter,
  `stdin: Any = None` (defaults to `sys.stdin` when omitted/`None` --
  production/`cmd_run` never passes it), used only so a test can hand
  `--exit-with-parent`'s watcher a fake/pipe file object. No other new
  seam was needed: `--no-peering`'s "construction never called" proof
  and `assemble_names_api`'s None-callback safety are exercised directly
  against `assemble_registry`/`assemble_names_api` (mirroring
  `test_cli_run_peering.py`/`test_cli_ports_instance_pipe.py`'s own
  precedent of testing this module's assembly functions rather than
  `_run_registry`); `--ready-json`'s shape, `--exit-with-parent`'s
  wiring, and the three-flag composition test are all exercised against
  a fully mocked `assemble_registry`/`assemble_relay_pool`/
  `assemble_names_api` (no real sockets/ports), per this ticket's own
  Testing plan.

New test file: `tests/registry/cli/test_cli_spawn.py` (15 tests, all
passing; `uv run pytest tests/registry/cli/ -q` -- 103 passed, no
regressions in the rest of that directory).

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
