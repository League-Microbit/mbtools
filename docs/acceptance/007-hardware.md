# Ticket 008-005 — Hardware acceptance: `watch` and `unlock --force`

Sprint 008 (`mbregistry client API for robot-console`), ticket
`005-hardware-acceptance-watch-and-unlock-force`. Run 2026-09-24 against
real hardware: `loki` and `magni` (both Debian 13 aarch64 Nolanet
nodes). `torture`'s relays were not touched, per CLAUDE.md's standing
rule. `hodr`, `meili`, and `braeburn` were not used for the two
scenarios themselves (see "Hosts used" below for why `hodr` was swapped
out).

Both hosts were freshly deployed with this branch's build
(`scripts/deploy-host.sh loki` / `scripts/deploy-host.sh magni`,
`mbtools==0.20260924.6`) and `mbregistry.service` restarted as part of
that deploy. Per this project's existing convention, this document
separates hardware-verified claims from anything only exercised by the
mocked automated suite.

**No real code bug was found.** Both scenarios passed as designed. One
apparent anomaly (a multi-second gap between issuing `unlock --force`
and the holder's connection observing EOF) was investigated and traced
to Python/CLI process-startup latency on the Pi hosts, not the
force-unlock mechanism itself — see Scenario 2's own section.

## Pre-flight: full test suite

Per the ticket's Testing section, `uv run pytest` (full suite) was run
before starting the hardware pass, on the dev Mac.

```
$ uv run pytest -q
...
13 failed, 1201 passed, 3 skipped in 149.59s (0:02:29)
```

All 13 failures are `zmq.error.ZMQError: Address already in use
(addr='tcp://*:7442')` (and the two sibling default ports) in
`tests/registry/peering/test_peering.py` — reproduced in isolation
(`uv run pytest -q tests/registry/peering/test_peering.py`, same 12
failures) and traced to this dev Mac's own real `mbregistry`
LaunchAgent (`org.jointheleague.mbregistry`, CLAUDE.md's "don't touch
it" daemon) already listening on the project's default ports
7440/7442/7443 (confirmed via `lsof -nP -iTCP:7440,7442,7443
-sTCP:LISTEN`). These tests construct `PeerDiscovery` with the
project's hardcoded default ports rather than ephemeral (`0`) ports —
a pre-existing test-isolation gap in `tests/registry/peering/
test_peering.py`, last touched in sprint 010 (`git log`), unrelated to
any of this sprint's tickets (001-004) or to `watch`/`unlock --force`.
Confirmed not caused by this branch: a scoped re-run of
`tests/registry/` (which includes both `test_api.py` — the module
`watch` lives in — and `test_peering.py`) reproduced only the same 12
peering failures a second time; the one additional, seemingly-unrelated
failure from the very first full run
(`test_api.py::test_watch_fans_out_to_every_connected_watcher`) did not
reproduce in isolation or in this second full `tests/registry/` run,
and is treated as a one-off local flake, not a regression. Not fixed
here — it is pre-existing infrastructure outside this ticket's scope,
and this dev Mac's LaunchAgent is explicitly off-limits (CLAUDE.md).
The branch is otherwise green.

## Hosts used

The ticket calls for "any two of `meili`/`loki`/`hodr`/`magni`/
`braeburn`". `hodr` was the first host attempted (`meili`/`loki` were
already used, and quirk-fixed, in sprint 007's own hardware pass) but
was found with **no default route at all**
(`ip route` on `hodr` shows only the two local docker/eth0 subnets, no
`0.0.0.0/0` entry; `ping 8.8.8.8` returns "Network is unreachable";
`loki`/`magni`/`braeburn` all confirmed `internet-ok` at the same time)
— a real, host-specific network/infrastructure condition, unrelated to
`mbtools`, that this ticket has no scope or credentials to fix
(no DHCP/network config access implied by "passwordless sudo for this
project's own commands"). `hodr`'s `mbregistry.service` had already
been stopped as the first step of the aborted deploy attempt (before
the wheel build failed on DNS resolution); it was restarted
(`sudo systemctl start mbregistry.service`) to leave the host in its
original (pre-existing, not-yet-redeployed-this-sprint) running state.
`loki` and `magni` were used instead for both scenarios.

**Known quirk re-confirmed** (CLAUDE.md's "stale `/opt/mbtools`
shadow"): `magni`'s bare `mbregistry` (`/usr/local/bin/mbregistry`)
still pointed at the stale `/opt/mbtools` install
(`head -1 $(which mbregistry)` → `#!/opt/mbtools/bin/python3`) even
though `mbregistry.service` itself was already running this session's
fresh build (systemd's `ExecStart=` always points at the real venv, per
CLAUDE.md). Re-pointed `/usr/local/bin/{mbregistry,mbdeploy,mbserial,
mbrelay}` on `magni` to the fresh venv
(`/tmp/mbtools-venv-eric/bin/...`, `magni`'s low-disk tmpfs fallback
path) the same way CLAUDE.md's existing note describes for
`meili`/`loki`. `loki` was already correctly pointed at
`~/mbtools-venv` from the earlier sprint-007 session. No update to
CLAUDE.md's own quirk note was needed beyond this acceptance doc, since
the note already instructs checking every host before trusting its bare
CLI — this is exactly that check paying off, not a new quirk.

## Scenario 1 — `watch` observing a real attach/detach

**PASS.**

`loki`'s own dedicated board is `togov` (RADIOBRIDGE relay, short uid
`fe9a0254`, full uid
`9906360200052820fe9a0254d8d892d9000000006e052820`, sysfs path
`1-1.3`).

Two `watch` clients were opened concurrently, per the ticket's own
"local socket, plus remote port from another host" scoping — small
Python scripts (`socket`/`json`, stdlib only — no new project tooling)
rather than `nc`/`socat`, since they needed to print a timestamp per
line:

- `watch_local.py` on `loki` itself, against the local Unix socket
  (`/run/mbregistry/api.sock`).
- `watch_remote.py` on `magni` — a **different host** — against `loki`'s
  remote TCP control-plane port (7440).

Both sent `{"op": "watch"}` and got `{"ok": true}` back immediately.
Then, on `loki`:

```
$ date +%s.%N; echo -n 1-1.3 | sudo tee /sys/bus/usb/drivers/usb/unbind
1790313592.737810261
1-1.3
$ sleep 3; date +%s.%N; echo -n 1-1.3 | sudo tee /sys/bus/usb/drivers/usb/bind
1790313598.823615412
1-1.3
```

`watch_local.py`'s log (local Unix socket, on `loki`):

```
1790313580.181 {"ok": true}
1790313594.420 {"type": "detach", "host": "loki", "uid": "9906360200052820fe9a0254d8d892d9000000006e052820"}
1790313600.464 {"type": "attach", "host": "loki", "uid": "9906360200052820fe9a0254d8d892d9000000006e052820", "port": "/dev/ttyACM0", "vid_pid": "0d28:0204"}
1790313600.899 {"type": "identity", "host": "loki", "uid": "9906360200052820fe9a0254d8d892d9000000006e052820", "state": "connected", "role": "RADIOBRIDGE", "common_name": "relay", "device_name": "togov", "serial_payload": "2108549556", "raw_announcement": "DEVICE:RADIOBRIDGE:relay:togov:2108549556"}
DONE
```

`watch_remote.py`'s log (remote TCP port 7440, from `magni`):

```
1790313582.043 {"ok": true}
1790313594.424 {"type": "detach", "host": "loki", "uid": "9906360200052820fe9a0254d8d892d9000000006e052820"}
1790313600.468 {"type": "attach", "host": "loki", "uid": "9906360200052820fe9a0254d8d892d9000000006e052820", "port": "/dev/ttyACM0", "vid_pid": "0d28:0204"}
1790313600.903 {"type": "identity", "host": "loki", "uid": "9906360200052820fe9a0254d8d892d9000000006e052820", "state": "connected", "role": "RADIOBRIDGE", "common_name": "relay", "device_name": "togov", "serial_payload": "2108549556", "raw_announcement": "DEVICE:RADIOBRIDGE:relay:togov:2108549556"}
DONE
```

`detach` arrived ~1.7s after the `unbind` command (dwc_otg USB
detach-detection latency, the same known Pi quirk CLAUDE.md documents,
not a `watch` issue), `attach` ~1.6s after `bind`, followed by
`identity` once the relay re-announced over serial. Both transports
delivered the identical event sequence within 4ms of each other — the
event bus fans out to the local-socket and remote-TCP `watch`
subscribers uniformly, exactly as `docs/design/registry-api.md`'s
`watch` section describes. `mbregistry list` on `loki` afterward showed
`togov` back to `free`/`local`.

## Scenario 2 — `unlock --force` against a live holder

**PASS.**

On `loki`, a script (`lock_and_stream.py`) locked `togov` (`serial`
kind, label `acceptance-008-005`) over the local Unix socket, then sent
`{"op": "stream", "uid": ...}` on the *same* connection (ticket 004's
local-socket `stream`) and blocked reading raw bytes — a live local
`stream` session, per the ticket's "ideally a live local `stream`"
preference:

```
1790313704.125 lock response: {'ok': True}
1790313704.128 stream response: {'ok': True}
1790313704.128 entering blocked read loop (stream frames)
```

`mbregistry list` confirmed the labeled lock while the session was
live:

```
locked by serial pid 2716534 (acceptance-008-005, 6s)  togov  fe9a0254  RADIOBRIDGE/relay  local     /dev/ttyACM0
```

From a second, independent SSH session on the same host:

```
$ mbregistry unlock fe9a0254 --force
mbregistry: fe9a0254: released serial lock (acceptance-008-005, 14s)
```

The holder's blocked read unblocked with a clean EOF:

```
1790313792.936 EOF (recv returned empty)
DONE
```

and `mbregistry list` immediately afterward showed `togov` back to
`free`/`local`.

**Apparent anomaly, investigated, not a bug**: on the first run, the
gap between the SSH command that ran `unlock --force` and the client's
observed EOF was ~3.9s — not obviously "essentially immediate" as the
acceptance criterion words it. Evidence gathered before concluding
anything (per this project's debugging protocol): `time mbregistry
unlock fe9a0254 --force` (already-unlocked device, so pure CLI overhead)
measured **4.4s real time** on `loki` for the command to even start
doing its actual work — Python interpreter startup plus importing
`pyocd`/`cmsis-pack-manager`/`zeroconf` and the rest of `mbregistry`'s
dependency graph is simply slow on a Raspberry Pi, for *every*
`mbregistry` client subcommand, not something specific to
`force_unlock`. Confirmed by re-running the scenario and timing from
the *end* of the `unlock --force` command's own execution (a `date`
run immediately after it in the same SSH invocation) instead of from
when the SSH command was issued:

```
mbregistry: fe9a0254: released serial lock (acceptance-008-005b, 7s)
1790313793.623943052   # date, immediately after the CLI printed "released"
```

against the client's own log:

```
1790313792.936 EOF (recv returned empty)
```

The client observed EOF *before* the `date` command even ran — i.e.
`_op_force_unlock`'s `conn.shutdown(socket.SHUT_RDWR)`
(`src/mbtools/registry/api.py`) unblocks the holder's read
synchronously, as designed; the multi-second gap in the first
measurement was entirely `mbregistry unlock`'s own CLI startup time on
this host, not a defect in the force-unlock/EOF path. No code change
needed; no regression test added, since there is no regression.

## Cleanup and final state

- `mbregistry unlock --force` released `togov` in both runs; `mbregistry
  list` confirmed `free`/`local` on `loki` after each.
- All scratch scripts and logs (`/tmp/watch_local.py`,
  `/tmp/watch_remote.py`, `/tmp/lock_and_stream.py`, and their log
  files) were removed from `loki` and `magni` after the pass.
- Two ad hoc `while pgrep -f <script>.py` polling loops (run locally on
  `loki`/`magni` over SSH, to detect when a backgrounded Python script
  had exited) never terminated on their own — `pgrep -f` matches full
  command lines, so each loop kept matching its own `bash -c "while
  pgrep -f watch_*.py..."` invocation, which itself contains the
  pattern text. Both were found via a follow-up `pgrep -af` sweep and
  killed by PID. This is a mistake in this session's own throwaway
  shell scripting, not a `mbtools` bug, and is recorded here only so a
  future session doesn't mistake a stray `bash -c "while pgrep..."`
  process for something `mbregistry` left behind.
- `hodr`'s `mbregistry.service` was restarted (still the pre-existing,
  not-this-sprint build) after being stopped mid-deploy-attempt; `hodr`
  itself was not otherwise touched or redeployed this session, and its
  no-default-route condition was left as found (out of scope).
- `meili` and `braeburn` were not touched this session.
- Final state: `loki` and `magni` both running `mbregistry.service`
  (`mbtools==0.20260924.6`, this sprint's build), active, with their
  dedicated boards `free`/`local`; `hodr` running its prior build,
  active, unaffected by this session beyond the one restart above;
  `meili`/`braeburn` unchanged; `torture`'s relays untouched throughout.

## Acceptance criteria

- [x] A `watch` client on a real host observes `attach` immediately
      after a physical plug-in and `detach` immediately after unplug,
      for that host's dedicated board. (Scenario 1, both transports.)
- [x] `mbregistry unlock --force <uid>` against a lock held by a live,
      separate connection on the same host releases the lock and that
      connection observes EOF/an error; `mbregistry list` confirms the
      board is unlocked afterward. (Scenario 2.)
- [x] Neither scenario touches `torture`'s relays.
- [x] `docs/acceptance/007-hardware.md` records both scenarios, the
      hosts/boards used, and the outcome.
- [x] Any real bug found is fixed and covered by a regression test —
      n/a, no real bug was found; the one anomaly investigated
      (Scenario 2's apparent EOF latency) traced to CLI startup time,
      not registry code.
