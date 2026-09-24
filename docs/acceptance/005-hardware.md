# Ticket 010 — Hardware acceptance: six test hosts

Sprint 005 (`mbregistry Windows platform support and fleet migration`),
ticket `010-hardware-acceptance-five-test-hosts-docs-acceptance-005-hardware-md`.
Run 2026-09-24 against real hardware: `meili`, `loki`, `hodr`, `magni`
(Nolanet nodes, Debian 13 aarch64), `braeburn` (macOS 15, x86_64), and
`torture` (Ubuntu 24.04 x86_64, three physical RADIOBRIDGE relays — the
sixth host, per sprint 005 ticket 007's stakeholder-authorized scope
change; see `CLAUDE.md`). All six were SSH-reachable and their
`mbregistry.service`/process was active at session start.

Per the ticket's own scoping: this document separates hardware-verified
claims from fakes/CI-only claims, and every Windows-specific claim from
tickets 002-006 is listed as not-hardware-verified (Scenario 5) — this
project has no Windows node in its test fleet.

## Scenario 1 — Re-probe fix, deliberately constructed (ticket 001)

**PASS.** Reproduced the actual `togov`/sprint-004 bug scenario on
purpose, using `magni`'s dedicated test board (`vevav`, uid `2e78ea8f`,
`RADIOBRIDGE/relay`, already stored with that role in the registry):

1. Locked `vevav`, sent `HELLO`/normalize, tuned it with `!CG 41 30` —
   **`vitut`'s own derived channel/group** (`loki`'s NEZHA2 test robot;
   computed via `mbtools.relay.naming.name_to_radio("vitut") ==
   (41, 30)`), i.e. deliberately "against another board's channel/group"
   as the ticket specifies — then sent `!GO` and closed the port
   *without* a recovery `BREAK`, leaving the board stuck transparently
   forwarding radio traffic in the data plane. Script:
   `reprobe_dataplane_test.py` (scratchpad; adapted from an earlier
   session's `strand_in_dataplane.py`). Unlocked afterward so the
   daemon's `_maybe_probe` would be eligible to touch it again (a locked
   uid is never reprobed).
2. **Trigger used**: a genuine USB detach/reattach via sysfs driver
   unbind/rebind on `magni` (`echo -n '1-1.3' > /sys/bus/usb/drivers/
   usb/unbind`, then `.../bind`), the real hardware-level equivalent of
   a physical unplug/replug — chosen over the ticket's other suggested
   substitute, a bare `systemctl restart mbregistry.service`, after
   confirming by reading `store.upsert_attached`'s own docstring and
   testing it directly that a plain daemon restart is a **deliberate
   no-op** for an uid whose store row never transitioned through
   `disconnected` ("the daemon restarting and rescanning a device that
   was attached the whole time keeps its state and `last_probe`
   untouched" — confirmed empirically first: a restart with `vevav`
   still in the data plane left its row unchanged, uninteresting, and
   would have been a false-positive PASS reproducing nothing). The
   unbind/rebind made `mbregistry list` show `vevav` as `gone` (detach
   correctly registered), then `free`/`local` again after rebind, with
   the daemon journal showing `state=attached_unprobed` and an
   `identity` event right after re-enumeration — a genuine reprobe, not
   a no-op.
3. **Result: not corrupted.** Post-reprobe, `magni`'s own
   `mbregistry list` and every peer's (`loki` checked directly) still
   showed `vevav` / `2e78ea8f` as `RADIOBRIDGE/relay`, `host=local`/
   `host=magni` respectively — never misidentified as `vitut`, `NEZHA2`,
   or anything forwarded from the channel/group it had been tuned to.
4. **Ground truth**: `sudo mbserial 2e78ea8f --reset HELLO` on `magni`
   directly against the physical board returned
   `DEVICE:RADIOBRIDGE:relay:vevav:536019796` — matching the registry
   row exactly.

This is the fix ticket 001 (this sprint) implemented — `identity.probe`'s
`reset_first=True` when the *pre-probe stored role* is a relay, forcing a
`BREAK` before `HELLO` is ever written — holding up against a real,
deliberately constructed instance of the original bug.

## Scenario 2 — Deployment-tooling idempotency (ticket 007), all six hosts

**PASS on all six**, each run twice (first pass rebuilds — the source
tree has changed since ticket 011's hardware pass, mostly ticket 008/009
plus this ticket's own fix below — second pass confirms a true no-op).
`scripts/deploy-host.sh <host>` for each:

| Host | First run | Second run |
|---|---|---|
| `meili` | rebuilt (tmpfs venv, low-disk fallback path exercised again) | `remote marker matches local state — skipping rebuild/reinstall`; `ActiveEnterTimestamp` unchanged |
| `loki` | rebuilt | marker match; `ActiveEnterTimestamp` unchanged |
| `hodr` | hit its known pre-existing no-default-route issue (`docs/acceptance` precedent, ticket 007/011) fetching from PyPI mid-rebuild; restored using the documented `uv pip install --offline --python ~/mbtools-venv/bin/python --force-reinstall <wheel>` workaround against its warm `~/.cache/uv`, then `install-service`/systemctl/udevadm run by hand, then the marker written to match the local tree hash | marker match; `ActiveEnterTimestamp` unchanged |
| `magni` | rebuilt | marker match; `ActiveEnterTimestamp` unchanged |
| `braeburn` | rebuilt (macOS foreground-process path: stopped, rebuilt, relaunched with preserved `--socket`/`--db` args) | marker match; process start time unchanged |
| `torture` | rebuilt; legacy `mbrelay.service` re-checked already-stopped/disabled (idempotent skip logged) | marker match; `ActiveEnterTimestamp` unchanged; legacy-mbrelay skip logged again |

This exceeds ticket 007's own "at least one host, re-run on an
already-healthy host" bar — all six got that same "second run is a
verified no-op" proof, not just one. `hodr`'s network problem is the
same pre-existing infra issue ticket 007 first hit (not an `mbtools`
defect; not fixed here either, per that ticket's own conclusion) — its
recovery path was re-exercised, worked identically, and is documented
here rather than re-diagnosed.

A **second, final** idempotency pass was run against all six hosts after
Scenario 3's bug fix was applied (below), to prove the deploy tooling is
idempotent against the actual final state of this ticket's tree, not
just against the pre-fix snapshot: all six again reported marker-match/
no-op, `hodr` included (its wheel was rebuilt locally and pushed by hand
a second time, matching the same offline workaround).

## Scenario 3 — Full command smoke test

**PASS**, each command exercised at least once against real hardware,
targeting each host's own dedicated test board rather than a robot in
active use (`magni`'s `vevav`, `loki`'s `vitut` — both explicitly
freely-flashable per `CLAUDE.md`'s "you may flash them freely"):

- **`mbregistry list`**: run on all six hosts throughout this session
  (see Scenarios 1/2/4 and below); also confirmed the peering view is
  consistent host-to-host (e.g. `magni`'s `vevav` shows as `host=magni`
  on `loki`, `host=local` on `magni` itself).
- **`mbdeploy deploy --repo`**: `sudo mbdeploy deploy vitut --repo
  League-Microbit/nezha-robot-template` on `loki` — flashed the cached
  latest release (`v0.20260919.7`), hit one transient probe/
  communication error mid-flash (`mbdeploy`'s own retry-once logic
  recovered cleanly on the second attempt — not treated as a bug: this
  is exactly the tool's documented transient-error handling working as
  designed, not a new failure mode), and finished with `vitut
  re-announced as NEZHA2 (flash_count=1)`. `mbregistry list` afterward
  confirmed `vitut` still free/local/`NEZHA2/robot`.
- **`mbserial`, with and without `--reset`**: both run against `vevav`
  on `magni` (`sudo mbserial 2e78ea8f --reset HELLO` and
  `sudo mbserial 2e78ea8f HELLO`), both returned the correct
  `DEVICE:RADIOBRIDGE:relay:vevav:536019796` banner (see Scenario 1
  point 4 for the `--reset` case specifically, used there as the
  ground-truth check).
- **`mbrelay connect`**: both target forms exercised.
  - Local form (`vitut`, run *on* `magni`, its relay's own host):
    tuned to `vitut`'s registry channel/group, **`vitut answered
    PING`** — confirms a real, live radio round trip end to end, not
    just a relay-side echo.
  - `robot@host` form (`vitut@magni`, run *on* `loki`, a different
    host — exercises the remote-relay path over the peering network):
    same successful tune + `PING` answered, via `magni`'s relay pool
    over the network.
- **`mbrelay names`**: `get`/`list` both exercised (`vitut` registered
  via `mbrelay names set vitut 41 30` — its own derived default,
  confirmed present in a subsequent `names list`).

**One real bug found and fixed this ticket**, hit directly while running
the smoke test non-interactively (stdin redirected from `/dev/null`, the
natural way to drive `mbrelay connect` from a script/SSH one-liner
without a real terminal):

```
mbrelay: connected. Ctrl-] to quit.
Traceback (most recent call last):
  ...
  File ".../mbtools/relay/cli.py", line 307, in _interactive
    readable, _, _ = select.select([stdin_fd], [], [], 0.05)
AttributeError: 'NoneType' object has no attribute 'select'
```

`_interactive` (`src/mbtools/relay/cli.py`) chose its `select`-based
raw-terminal read loop whenever `stdin_fd is not None`, but only
imports `select`/`termios`/`tty` inside the `is_tty` branch. A redirected
file, a pipe, or `/dev/null` all give `sys.stdin` a real file descriptor
(`fileno()` succeeds) while `os.isatty()` is still `False` — exactly the
gap between the two conditions, and exactly what this session's own
non-interactive SSH commands hit on the very first real board. The
existing test suite's `_immediate_stdin_eof` autouse fixture used
`io.StringIO("")`, which has **no** `fileno()` at all, so it only ever
exercised the "no fd" fallback path, never "real fd, not a tty."

**Fix**: gate the select-based branch on `is_tty` instead of
`stdin_fd is not None` (`src/mbtools/relay/cli.py`, `_interactive`).
**Test**: `tests/relay/test_cli.py::
test_interactive_session_survives_non_tty_real_fd_stdin` — opens a real
`os.devnull` file object for `sys.stdin` (real `fileno()`, `isatty() ==
False`) and asserts `_run_connect` completes with `EXIT_OK` instead of
raising. Scoped run: `uv run pytest tests/relay/test_cli.py -q` — 24
passed. Verified against real hardware after redeploying the fix to all
six hosts: `sudo mbrelay connect vitut < /dev/null` on `magni` and
`sudo mbrelay connect vitut@magni < /dev/null` on `loki` both now exit
`0` cleanly with no traceback.

**One paper-cut finding, not fixed (flagged, not silently glossed over)**:
`mbregistry list`/`mbserial`/etc. run on `braeburn` *without* an
explicit `--socket` fail confusingly (`registry unavailable at
/run/mbregistry/api.sock: [Errno 2] No such file or directory`) — macOS
has no `/run` directory at all (`ls -ld /run` → `No such file or
directory`), yet `registry.paths.default_socket_path()` returns
`/run/mbregistry/api.sock` unconditionally on every non-Windows
platform. This is not new behavior (ticket 002's own acceptance
criteria explicitly required `default_socket_path()` to stay
byte-for-byte unchanged on macOS/Linux), and `scripts/deploy-host.sh`
already works around it operationally by launching `braeburn`'s daemon
with an explicit `--socket /tmp/mbregistry/api.sock` — but a bare
client command on `braeburn` without that flag still fails today.
Fixing `default_socket_path()`'s macOS behavior would mean resolving
`docs/design/specification.md`'s own still-open question #7 ("whether
macOS is a supported daemon platform... is unresolved") — out of this
acceptance ticket's scope to decide unilaterally. Recommend a follow-up
ticket, once that open question is settled with the stakeholder.

## Scenario 4 — `braeburn` mDNS discovery without `--peer`

**PASS at this session's uptime; the multi-hour degradation neither
reproduced nor newly ruled out**, same honest-inconclusive shape as
`docs/acceptance/004-hardware.md`'s own Scenario 6. `braeburn`'s
`mbregistry` process was restarted twice by this session's own deploy
passes (Scenario 2's first pass, then again after Scenario 3's fix), so
its uptime never grew past this session's own redeploy cadence:
observed uptime at check time was **~3 minutes** (process start
`08:31:19`, checked `08:34:35`). `avahi-browse -r _mbregistry._tcp -t`
on `loki`, no `--peer` flag, found `braeburn` immediately both times it
was checked (once ~14 minutes after an earlier restart, once again at
~3 minutes after the final one) — consistent with the known quirk
(`CLAUDE.md`), which only manifests after several hours of continuous
uptime, not with a fresh process. Recording this uptime and the
positive-but-uninformative result rather than omitting the check,
per the ticket's own "record regardless of outcome" instruction — this
session's own hardware-fix work made a longer, undisturbed uptime
window impractical to obtain.

## Scenario 5 — Torture relay pool re-verified (ticket 011)

**PASS**, re-checked after this ticket's own deploy-tooling pass
redeployed ticket 011's fix (and this ticket's own `mbrelay` fix) to
`torture` a second time:

- `torture`'s own `mbregistry list`: all three physical relays
  (`gozop`/`4f02a351`, `guvov`/`52f41cc6`, `zetog`/`f92f913d`) show
  `host=local`.
- Every other host's `mbregistry list` (`hodr` checked directly, matches
  the earlier session-start snapshot on `meili`/`loki`/`magni`) shows
  all three as `host=torture`.
- Relay pool (port 7444) hands out working relays on connect: a
  four-connection probe against `torture:7444` returned distinct
  `DEVICE:RADIOBRIDGE:relay:...` banners for the first three connections
  (`gozop`, `zetog`, `guvov`) and correctly recycled `gozop` on the
  fourth once its prior connection had already closed (Python's
  refcounted socket close is synchronous, so this is the pool serving a
  freed slot, not a fourth physical device) — no `ImportError`/`0
  devices` symptom of the pre-011 bug.
- `console_compat.relay_pool` continues to offer only this host's own
  attached relays, matching ticket 011's Implementation Notes.

## Scenario 6 — Windows claims from tickets 002-006 (not hardware-verified)

This project has no Windows node in its test fleet (`CLAUDE.md`'s
"Hardware test targets" table: four Nolanet Linux nodes, one macOS, one
Linux relay host — no Windows). Every Windows-specific claim below is
**not hardware-verified** — verified against fakes (unit tests
simulating `sys.platform == "win32"`) and, separately, against a real
Windows VM only via GitHub Actions' `windows-latest` CI runner, which is
real Windows execution but not this project's own physical hardware:

| Ticket | Claim | Status |
|---|---|---|
| 002 | `paths.default_db_path()` returns a `%ProgramData%`-rooted path on Windows; `default_pipe_name()` returns the fixed pipe-name string | not hardware-verified — fakes + `windows-latest` CI only |
| 003 | `WindowsPipeAPIServer` dispatches `list`/`find`/`lock`/`unlock`/`mark_flashed` correctly over a real named pipe; pipe security descriptor is restrictive | not hardware-verified — fakes + `windows-latest` CI only (the real-pipe round-trip test only runs *on* the `windows-latest` runner, never against this project's own hardware) |
| 004 | `render_windows_service_install()`/`render_windows_service_failure_actions()` produce valid `sc.exe` invocations; `cmd_install_service_windows` prints them without executing | not hardware-verified — pure string construction, tested on `windows-latest` CI only; no real SCM registration ever attempted anywhere |
| 005 | `cmd_run`/`cmd_install_service` correctly branch to the Windows path on `sys.platform == "win32"` | not hardware-verified — the branch-selection logic itself is proven on the dev platform (macOS/Linux) per that ticket's own AC; the Windows-side behavior it dispatches to is fakes + `windows-latest` CI only |
| 006 | CI workflow runs the full suite on `windows-latest` and `ubuntu-latest` on every push/PR | **CI-verified, real Windows execution** — not this project's own hardware, but a genuine Windows VM via GitHub Actions, distinct from the fakes-only claims above. Confirmed green: [run 35999415493](https://github.com/League-Microbit/mbtools/actions/runs/35999415493) — `test (windows-latest)` 2m14s, `test (ubuntu-latest)` 8m23s, `test (macos-latest)` 1m57s, all ✓ |

## Full test suite

`uv run pytest -q` (pre-flight baseline, before this ticket's hardware
work): **885 passed, 3 skipped**. After Scenario 3's `mbrelay` fix and
its new regression test: **886 passed, 3 skipped** — no regressions.
Scoped run for the fix itself, `uv run pytest tests/relay/test_cli.py
-q`: **24 passed**.

## Summary

| Scenario | Result |
|---|---|
| Re-probe fix, deliberately constructed (ticket 001) | PASS |
| Deployment-tooling idempotency, all six hosts, twice each | PASS |
| Full command smoke test (`mbregistry list`/`mbdeploy deploy --repo`/`mbserial` ±`--reset`/`mbrelay connect` (local + `@host`)/`mbrelay names`) | PASS |
| `braeburn` mDNS discovery without `--peer` | PASS at this session's (~3-14 min) uptime; multi-hour degradation neither reproduced nor newly ruled out |
| Torture relay pool re-verified (ticket 011) | PASS |
| Windows claims (tickets 002-006) | Explicitly listed, not hardware-verified (fakes + `windows-latest` CI only — see Scenario 6 table) |

**One real bug found and fixed this ticket**: `mbrelay connect`'s
interactive session crashed against any non-tty-but-real-fd stdin
(a redirected file, a pipe, `/dev/null`) — see Scenario 3, fixed in
`src/mbtools/relay/cli.py`'s `_interactive`, with a new regression test
and verified against real hardware after redeploying to all six hosts.
**One paper-cut finding, documented, not fixed**: `mbregistry`/
`mbserial`/etc. client commands on `braeburn` fail without an explicit
`--socket` flag, because macOS has no `/run` directory and
`default_socket_path()` doesn't special-case it — out of scope to fix
unilaterally (would resolve an explicitly open architecture question);
recommend a follow-up ticket. No robot in active use was disturbed —
every flash/data-plane/reprobe test used a host's own dedicated,
freely-flashable test board (`vevav` on `magni`, `vitut` on `loki`),
never a production robot. No changes were needed to `docs/wiki/` (this
repository still has none, per prior tickets' own precedent of using
`docs/design/specification.md`/`docs/design/registry-api.md` as the
documentation home in its absence) beyond this document and the
`CLAUDE.md` update noted below.
