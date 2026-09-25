# Ticket 007-007 — Hardware acceptance: two instances on one host plus a silent board named over SWD

Sprint 007 (`silent board SWD naming and multi-instance mbregistry`),
ticket `007-hardware-acceptance-two-instances-on-one-host-plus-a-silent-board-named-over-swd`.
Run 2026-09-24 against real hardware: `meili` and `loki` (both Debian 13
aarch64 Nolanet nodes). `hodr`/`magni` were used only as third-party
observers to cross-check fleet-wide peering consistency; `braeburn` and
`torture` were not touched this session. No other SSH sessions were
found active on `meili`/`loki` beyond this session's own (a couple of
long-dormant `jtl` local-console logins, not concurrent work) before
starting.

Both hosts were redeployed with this branch's build
(`scripts/deploy-host.sh meili` / `scripts/deploy-host.sh loki`,
`mbtools==0.20260924.5`, rebuilt again mid-session after each of the two
fixes below) and `mbregistry.service` restarted each time. Per the
ticket's own scoping, this document separates hardware-verified claims
from anything only exercised by the mocked automated suite.

**Two real bugs were found and fixed this ticket**, both while exercising
Scenario A and Scenario C exactly as specified — see each scenario's own
section for the full trace. Both fixes are small, scoped to the exact
code path each scenario exercises, and each ships with a new regression
test that fails on the pre-fix code and passes on the fix.

## Scenario A — two instances, one host (`meili`)

**PASS**, with one real bug found and fixed along the way.

`meili`'s system `mbregistry.service` was stopped
(`sudo systemctl stop mbregistry.service`), then two hand-run instances
were started with distinct `--instance`/`--socket`/`--db` and every port
(`--remote-port`/`--peer-pub-port`/`--peer-snapshot-port`/`--pool-port`/
`--names-port`) set to `0`:

```sh
mkdir -p /tmp/mbr-a /tmp/mbr-b
sudo /home/eric/mbtools-venv/bin/mbregistry run \
  --socket /tmp/mbr-a/api.sock --db /tmp/mbr-a/devices.db \
  --instance meili-a --remote-port 0 --peer-pub-port 0 --peer-snapshot-port 0 \
  --pool-port 0 --names-port 0 &
sudo /home/eric/mbtools-venv/bin/mbregistry run \
  --socket /tmp/mbr-b/api.sock --db /tmp/mbr-b/devices.db \
  --instance meili-b --remote-port 0 --peer-pub-port 0 --peer-snapshot-port 0 \
  --pool-port 0 --names-port 0 &
```

Both bound distinct ephemeral ports with no collision (`ss -ltnp` showed
ten distinct listeners across the two PIDs).

**Bug found**: `avahi-browse -rt _mbregistry._tcp` from `loki` showed
both instances' `_mbregistry._tcp` advertisements with the literal,
useless `port = [0]` / `txt = ["remote_port=0" ...]` — the daemon's own
mDNS record for a `--remote-port 0` instance never picked up the *real*
ephemeral port `RemoteAPIServer` actually bound; it stayed at the
*requested* `0` forever. Root cause: `cli.py`'s `_run_registry`
constructs `PeerDiscovery(remote_port=remote_port, ...)` with the
CLI-resolved value *before* `remote_api.start()` ever binds a socket,
and nothing read `remote_api.bound_port` back into `peering` before
`peering.start()` built its `ServiceInfo`/TXT record from
`self._remote_port`. This is a different, previously-unknown gap from
the *already-documented* `peer_pub`/`peer_snapshot` limitation
(`docs/service.md` §5's own "PeerDiscovery exposes no bound-port
equivalent to read back from" note) — `RemoteAPIServer.bound_port`
*does* exist and *was* available at the exact call site, unlike the ZMQ
pub/snapshot sockets.

**Fix**: `PeerDiscovery.set_remote_port(port)`
(`src/mbtools/registry/peering.py`), called from `cmd_run`
(`src/mbtools/registry/cli.py`) with `remote_api.bound_port` right after
`remote_api.start()` and right before `peering.start()` — a no-op for an
explicit, non-zero `--remote-port` (the two values already match), and
the fix for the ephemeral case. New regression test:
`tests/registry/cli/test_cli_ports_instance_pipe.py::
test_ephemeral_remote_port_is_advertised_by_peering_not_zero`.

**Re-verified on real hardware after redeploying the fix**: `meili`'s
system service was stopped again, both instances restarted with the
fixed build, and `avahi-browse -rt _mbregistry._tcp` from `loki` this
time showed the real bound ports:

```
=   eth0 IPv4 meili-a                                       _mbregistry._tcp     local
   hostname = [meili-a.local]
   address = [192.168.2.150]
   port = [35521]
   txt = ["snapshot_port=0" "pub_port=0" "remote_port=35521"]
=   eth0 IPv4 meili-b                                       _mbregistry._tcp     local
   hostname = [meili-b.local]
   address = [192.168.2.150]
   port = [43863]
   txt = ["snapshot_port=0" "pub_port=0" "remote_port=43863"]
```

(`35521`/`43863` match the "remote api" ports each instance's own
startup stderr line reported.) `snapshot_port`/`pub_port` staying `0`
here is the pre-existing, already-documented, out-of-scope limitation,
not this bug — see "Not fixed / already known" below.

**Board claim, exactly one instance**: `meili`'s dedicated test board
(`gitev`, uid `5e042b04`, `NEZHA2/robot`) was claimed by instance B only:

```
$ mbregistry list --socket /tmp/mbr-a/api.sock      # no gitev row at all
$ mbregistry list --socket /tmp/mbr-b/api.sock
...
free   gitev  5e042b04  NEZHA2/robot       local     /dev/ttyACM0
```

**Detach/reattach**: a real USB unbind/rebind
(`echo -n 1-1.2 > /sys/bus/usb/drivers/usb/unbind` then `.../bind`,
`gitev`'s own sysfs path on `meili`) was used as the hardware-level
detach/reattach trigger (same technique `005-hardware.md` Scenario 1
used). On detach, instance B correctly showed `gitev` as `gone`
(instance A still showed nothing). On reattach, **instance A won the
re-claim race this time** — B's own store still shows the earlier `gone`
row (unclaimed by B, not re-probed), A shows `free`/`local`. This is a
legitimate race outcome, not a bug: the ticket's own wording anticipates
either instance winning ("confirm the same instance re-claims it, or the
other one does"), and the cross-instance claim (`registry.claims`,
`flock` on a shared temp directory) is explicitly a non-blocking race
with no "sticky owner" guarantee. No collision was observed — at any
given moment exactly one instance's list showed `gitev` as owned, never
both.

Cleanup: both hand-run instances killed (`SIGTERM`, confirmed both
processes gone), `/tmp/mbr-a`/`/tmp/mbr-b` removed, `mbregistry.service`
restarted, `gitev` confirmed back to `free`/`local` under the service.

## Scenario B — silent board named over SWD (`loki`)

**PASS.**

Baseline (normal, announcing firmware), recorded before blanking:

```
$ mbregistry list | grep togov
free              togov  fe9a0254  RADIOBRIDGE/relay  local     /dev/ttyACM0
```

(`togov` is `loki`'s dedicated test board — also the same board named in
sprint 007's own architecture doc as the historical mass-erase incident
this ticket's SWD-naming feature was built to solve, per
`clasi/sprints/007-.../tickets/done/003-....md`'s own Description.)

**Genuine mass erase** via pyOCD directly (not through `mbdeploy`, so
this exercises the *ordinary* (non-flash-triggered) silent-board path,
not the known-blank path):

```
$ sudo /home/eric/mbtools-venv/bin/pyocd list
  0   Arm BBC micro:bit CMSIS-DAP  ...  ✔︎ nrf52833   BBC micro:bit V2
$ sudo /home/eric/mbtools-venv/bin/pyocd erase --mass -t nrf52833
0003781 I Mass erasing device... [eraser]
0004016 I Mass erase complete [eraser]
```

A mass erase alone does not change the daemon's view (the board never
detaches at the USB level, so nothing triggers a re-probe — matches
`005-hardware.md` Scenario 1's own finding that an already-`connected`
row is only re-probed on a genuine attach/detach event). A real
detach/reattach (`echo -n 1-1.3 > /sys/bus/usb/drivers/usb/unbind` then
`.../bind`, `togov`'s own sysfs path on `loki`) was used to force the
re-probe:

```
$ mbregistry list --json | jq '.devices[] | select(.short_uid=="fe9a0254")'
{
  "state": "attached_no_announce",
  "error_note": "no announcement received during probe",
  "chip_identity_name": "togov",
  "chip_identity_serial": 2108549556,
  "device_name": "togov",
  ...
}
```

`chip_identity_name` — read fresh over SWD by this ticket's own
`identity.read_chip_identity`, independent of the stale cached
`device_name` — is **`togov`**, exactly matching the name confirmed
above under normal, announcing firmware. `chip_identity_serial`
(`2108549556`) also matches the original announcement's own
`serial_payload` exactly. The table render (using the correct, freshly-
deployed client — see the `/opt/mbtools` finding below) shows exactly
the contract `clasi/sprints/007-.../tickets/done/001-....md` specifies:

```
$ mbregistry list | grep togov
no-answer         togov  fe9a0254  unknown            local     /dev/ttyACM0
```

STATE = `no-answer`, FIRMWARE = `unknown`, NAME = `togov` — the didn't-
announce state, not known-blank (correct: this was an ordinary silent
probe, not a flash-triggered one).

**Real hardware-only finding (not a code bug — see the CLAUDE.md
addition)**: the plain-table output above initially showed the *wrong*
thing — `STATE=free`, `FIRMWARE=RADIOBRIDGE/relay` (the stale
pre-erase values), plus an extra `togov: no announcement received
during probe` free-text line after the table, which the current
`render_table` source doesn't even contain code to print anymore
(removed by ticket 001). `mbregistry list --json` from the very same
shell, moments apart, showed the *correct* `attached_no_announce`/
`chip_identity_name` the whole time. Tracing it
(`which mbregistry` → `/usr/local/bin/mbregistry` →
`head -1 $(which mbregistry)` → `#!/opt/mbtools/bin/python3`) found a
**second, stale `mbtools` install at `/opt/mbtools`** (version
`0.20260924.4`, one build predating this sprint's `deploy-host.sh` run)
whose `/usr/local/bin/*` symlinks shadow `~/mbtools-venv` — this
project's own deploy target — on `$PATH` for any bare client command.
The **daemon** was unaffected the whole time (`mbregistry.service`'s own
`ExecStart=` correctly names `/home/eric/mbtools-venv/bin/python`,
confirmed in `/etc/systemd/system/mbregistry.service`), which is exactly
why the underlying `--json` data was always right — only the *client-
side* rendering code run by a bare `mbregistry` command was stale. Found
on both `meili` and `loki`; not checked on `hodr`/`magni`/`braeburn`.
Fixed on both hosts touched this session by re-pointing
`/usr/local/bin/{mbregistry,mbdeploy,mbserial,mbrelay}` at
`~/mbtools-venv/bin/...`; proposed as a `CLAUDE.md` addition (added,
this ticket) so a future session checks `head -1 $(which mbregistry)`
before trusting a bare CLI command's rendering on any of these hosts.

**Restore**: reflashed to the newest versioned release (not the moving
`latest` tag), per `CLAUDE.md`'s firmware table:

```
$ sudo mbdeploy deploy togov --repo League-Robotics/microbit-radio-relay@v0.20260913.2 --force-relay
...
mbdeploy: fetched League-Robotics/microbit-radio-relay@v0.20260913.2 asset MICROBIT.hex
mbdeploy: togov re-announced as RADIOBRIDGE (flash_count=1)
$ mbregistry list | grep togov
free              togov  fe9a0254  RADIOBRIDGE/relay  local     /dev/ttyACM0
```

Back to exactly the original baseline. `--force-relay` was required
(the store's last-known role still said relay/bridge, per
`docs/service.md` §13.6).

## Scenario C — spawn recipe (`loki`)

**PASS**, with one real bug found and fixed along the way.

First attempt (pre-fix code, from a real bash parent script using
`coproc` to hold a pipe open to the child, mirroring "a shell script
piping its own stdin through"):

```sh
coproc CHILD ( sudo mbregistry run --socket ... --ready-json --exit-with-parent --no-peering )
read -r ready_line <&"${CHILD[0]}"   # never returns
```

**Bug found**: the parent's `read` on the child's stdout pipe never
returned. The child's own stderr line (`mbregistry: listening on
...`) appeared immediately — stderr is unbuffered/line-buffered by
Python regardless of tty-ness — but the `--ready-json` line, printed
once via a bare `print(json.dumps(ready_payload))` with no `flush=True`,
sat in Python's **block-buffered** stdout (the default whenever stdout
isn't a tty — exactly the case for every real spawning parent this
recipe targets) until something else filled the buffer or the process
exited. Since `cmd_run`'s main loop never writes to stdout again after
that one line, a real parent reading this pipe — precisely
`docs/service.md`'s own documented spawn recipe — would block forever.
The existing automated test suite could not have caught this: every
`--ready-json` test in `test_cli_spawn.py` asserts on the payload via
`capsys` against a fully mocked `assemble_registry` (per ticket 005's
own testing plan, "no real sockets/ports"), and `capsys`'s in-memory
capture object has none of a real OS pipe's block-buffering behavior.

**Fix**: `print(json.dumps(ready_payload), flush=True)`
(`src/mbtools/registry/cli.py`). New regression test — the first in this
test file to use a *real* subprocess and a *real* OS pipe rather than a
mock:
`tests/registry/cli/test_cli_spawn.py::test_ready_json_line_reaches_a_real_pipe_promptly`.
(Writing this test hit one unrelated pitfall worth recording: using
pytest's `tmp_path` fixture for the `--socket` path overflowed macOS's
~104-byte `AF_UNIX` path limit, given this test's own long, descriptive
name — indistinguishable from a genuine hang until the child's stderr
was inspected. Fixed by using the file's existing short-path
`socket_dir` fixture instead, matching every other test in this file.)

**Re-verified on real hardware after redeploying the fix**:

```sh
coproc CHILD ( sudo /home/eric/mbtools-venv/bin/mbregistry run \
  --socket /tmp/mbr-spawn2/api.sock --db /tmp/mbr-spawn2/devices.db \
  --instance spawn-test2 --remote-port 0 --pool-port 0 --names-port 0 \
  --ready-json --exit-with-parent --no-peering )
read -r -t 10 ready_line <&"${CHILD[0]}"
```

Output:

```
parent: child pid=2705132
parent: got ready line: {"ready": true, "instance": "spawn-test2", "socket": "/tmp/mbr-spawn2/api.sock", "ports": {"remote": 40397, "pool": 39253, "names": 35887}}
parent: closing stdin to child
```

The ready line arrived immediately this time (well under the 10s
deadline). Closing the coprocess's write end (`exec {CHILD[1]}>&-`)
ended the child cleanly — confirmed separately with `pgrep -fal
mbr-spawn2` returning no process — proving `--exit-with-parent` too. (My
own follow-up polling loop in that same shell script hit an unrelated
`set -u`/coproc-array shell scripting bug of its own after the child had
already exited correctly; not a product issue, not investigated further
since the two things this scenario needed to prove — the ready line
arriving promptly, and the child exiting on stdin EOF — were both
already independently confirmed.)

## Windows `--pipe` (SUC-008) — not hardware-verified

As with every prior sprint's own acceptance doc (`005-hardware.md`
Scenario 6's precedent): this project has no Windows host in its test
fleet. `--pipe`/the Windows named-pipe transport are verified only
against fakes and `windows-latest` GitHub Actions CI, never against this
project's own physical hardware. Stated here explicitly, not silently
omitted, per this ticket's own Acceptance Criteria.

## Automated tests

Per this ticket's/`.claude/rules/source-code.md`'s own scoping rule
(scoped to the modules a ticket touches; the full suite runs once per
sprint inside `close_sprint`, not per ticket), the scoped regression run
for both fixes:

```
$ uv run pytest tests/registry/cli/test_cli_ports_instance_pipe.py \
    tests/registry/cli/test_cli_run_peering.py \
    tests/registry/cli/test_cli_spawn.py \
    tests/registry/peering -q
127 passed in 40.41s
```

## Final state

- `meili`: `mbregistry.service` active, running this ticket's final
  build (both fixes); `gitev` (`5e042b04`) back to `free`/`local`/
  `NEZHA2/robot`; no orphaned `mbregistry run` processes;
  `/usr/local/bin/*` symlinks re-pointed at `~/mbtools-venv`.
- `loki`: `mbregistry.service` active, running this ticket's final
  build; `togov` (`fe9a0254`) back to `free`/`local`/`RADIOBRIDGE/relay`
  on the same firmware version it started with
  (`v0.20260913.2`); no orphaned `mbregistry run` processes;
  `/usr/local/bin/*` symlinks re-pointed at `~/mbtools-venv`.
- `magni`/`hodr` (view-only this session): unchanged, confirmed still
  showing the fleet consistently (`gitev`→`meili`, `togov`→`loki`, all
  three `torture` relays, `braeburn`'s `zugit`).
- `braeburn`/`torture`: not touched this session.

## Summary

| Scenario | Result |
|---|---|
| A — two instances, one host (`meili`) | PASS — one real bug found and fixed (`PeerDiscovery` never advertised its real ephemeral remote port over mDNS); re-verified on hardware post-fix |
| B — silent board named over SWD (`loki`, `togov`) | PASS — real five-letter name (`togov`) and serial recovered via SWD, matching the announcing-firmware baseline exactly; one real *operational* (not code) finding, a stale shadowing `/opt/mbtools` install, documented and fixed on both touched hosts |
| C — spawn recipe (`loki`) | PASS — one real bug found and fixed (`--ready-json`'s line never flushed to a real pipe); re-verified on hardware post-fix |
| Windows `--pipe` (SUC-008) | Explicitly not hardware-verified — no Windows host in the fleet (fakes + `windows-latest` CI only) |

Two real code bugs found and fixed this ticket, both with new regression
tests exercising the exact real-world shape (a real OS pipe, real
ephemeral-port mDNS advertisement) the mocked suite could not reach; one
real operational finding (stale shadow install) documented in
`CLAUDE.md` rather than silently worked around. No robot/board in active
use elsewhere was disturbed — only each host's own dedicated,
freely-flashable test board was touched (`gitev` on `meili`, `togov` on
`loki`), and both were restored to their exact starting state.
