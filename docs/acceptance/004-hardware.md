# Ticket 010 — braeburn mDNS discovery and peering fix: hardware investigation

Sprint 004 (`mbrelay/robot-console compatibility and fleet migration`),
ticket `010-braeburn-mdns-discovery-and-peering-fix`. Run 2026-09-24
against real hardware: `braeburn` (macOS 15.7.4 x86_64, `ssh eric@braeburn`,
passwordless sudo, `mbregistry run` under `nohup`) and `loki` (Debian 13
aarch64, one of the four Nolanet nodes, `tcpdump` installed for this
session via `apt-get`). This ticket picks up where sprint 003's own
hardware pass (`docs/acceptance/003-hardware.md`) left off, having hit that
protocol's three-attempt cap without root-causing it — see that doc's "New
finding: `braeburn` mDNS/peering asymmetry" section for the original
symptom and the three hypotheses already refuted there (host firewall, DNS
resolution, shared zeroconf/zmq context). `docs/acceptance/003-hardware.md`
is not retroactively edited, per this ticket's own scope note.

## Approach

Rather than re-guess at candidates, this session went straight to
packet-level ground truth: synchronized `tcpdump` captures on both
`braeburn` and `loki` (`sudo tcpdump -i any -n udp port 5353`, backgrounded
with `nohup ... & disown` — a bare `&` over a non-interactive SSH command
gets `SIGHUP`ed the moment the SSH session exits, which cost the first
attempt its capture entirely; `nohup`+`disown` is required, not optional,
for this to work at all) while triggering `_mbregistry._tcp` queries from
`loki` (`avahi-browse -r _mbregistry._tcp -t`) and comparing what left/
arrived at each host's own NIC against what a bare `Zeroconf().
register_service()` script and a freshly-restarted `mbregistry run` did in
the same conditions.

## Candidate 1: macOS Application Firewall / Local Network privacy prompt

**Ruled out.** `socketfilterfw --getglobalstate` on `braeburn` confirmed
the Application Firewall is globally disabled (`State = 0`, matching
`docs/acceptance/003-hardware.md`'s own finding), so it cannot be gating
anything right now. The `--listapps` per-binary allow-list only has an
entry for `/usr/bin/python3` (the system Python), never the venv's own
`uv`-installed interpreter the daemon actually runs under — a plausible
mechanism this ticket's plan called out — but this was directly refuted
by experiment: a bare `Zeroconf().register_service()` script, run with
`/Users/eric/mbtools-venv/bin/python` (the daemon's *exact* interpreter)
via the *exact* same headless path the daemon itself uses
(`nohup ... & disown` over SSH, no logged-in GUI session, no possibility
of a TCC permission prompt ever being shown or granted), was discovered by
`loki`'s `avahi-browse` within seconds. If a macOS Local Network/
Application-Firewall permission gate were denying this venv/launch-method
combination, that script would have failed identically to the daemon —
it did not. (Reading `TCC.db` directly to check the permission database
itself was attempted and blocked by this session's own tool-use
classifier as a sensitive-data access; the behavioral test above answers
the same question without needing that read.)

## Candidate 2: which interface/address zeroconf binds/advertises on macOS

**Ruled out — the working and non-working processes bind identically.**
`sudo lsof -p <mbregistry pid> -n -P -iUDP` on `braeburn` showed the
daemon's `Zeroconf()` instance (default `InterfaceChoice.All`, never
overridden) with one UDP socket per active interface, all bound to port
5353: `en0` (192.168.1.49), `en1` (192.168.1.214), `bridge100`
(192.168.139.3), `bridge101` (192.168.155.0), `lo0` (127.0.0.1), plus the
`*:5353` wildcard — `braeburn` is indeed four-ways multi-homed, as
sprint 003's own finding already noted. Both the already-running (later
shown to be non-responsive — see "New finding" below) daemon and a
freshly-restarted one bind this identical set. There is nothing to "pin"
here: interface selection is consistent between the working and
non-working cases, so it is not the differentiator. No code change made
for this candidate.

## Candidate 3: REQ-socket creation ordering relative to the event loop

**Confirmed as a real, distinct defect — fixed.** Not "creation ordering"
literally (the REQ socket is always created fresh, well after the shared
`zmq.Context`, on every code path), but the same underlying concern the
ticket's plan named: `PeerDiscovery.connect_peer()` — wired as
`_BrowseListener`'s `on_peer_ready`, and therefore invoked *synchronously*
from `python-zeroconf`'s own `ServiceBrowser` callback-dispatch thread, a
thread this module does not own — called `_PeerLink.start()` inline,
which performed the snapshot REQ/REP round trip (blocking, up to the
5-second default `snapshot_timeout_ms`) before returning. Every new or
refreshed peer discovery could therefore stall zeroconf's own
callback-processing thread for up to 5 seconds against a slow or
unreachable peer. **Fixed**: `_PeerLink.start()` now performs only the
fast, non-blocking SUB connect+subscribe inline (preserving the existing
"subscribe before snapshot" Clone-pattern ordering) and dispatches the
snapshot fetch (and the SUB recv-loop startup it gates) to its own thread
(`_PeerLink._run_snapshot_and_recv`), joined with a bounded timeout by
`stop()`. See `src/mbtools/registry/peering.py`'s module docstring
("`connect_peer` never blocks its caller (ticket 010)") and
`_PeerLink.start`/`.stop`/`._run_snapshot_and_recv`.

Decoupling the fetch from `connect_peer`'s return surfaced a second, real
race during this same investigation (found via a timing-based repro, not
guessed): the async fetch and the SUB socket's own disconnect detection
became two independent signals about the same link with no ordering
guarantee, so a peer that dropped the link *during* the fetch's own
window could have a late-but-genuinely-successful snapshot reply
overwrite an already-correct `mark_peer_unreachable` back to reachable.
Fixed in the same change: `_PeerLink._link_dropped`, set the moment
`_monitor_loop` observes `zmq.EVENT_DISCONNECTED`, now guards
`_fetch_snapshot`'s `on_reachable` call. Regression test:
`tests/registry/peering/test_peering_eventbus.py::
test_snapshot_success_after_link_dropped_does_not_resurrect_reachable`.

Not conclusively the root cause of the "New finding" below — this defect
is real and independent of it, and is fixed regardless of whether it
explains any part of that multi-hour behavior.

## New finding (not one of the three original candidates): mDNS responsiveness degrades over uptime, not a fixed misconfiguration

This is the actual, most significant result of this session, found by
accident while setting up the packet captures above and then run down
deliberately:

`braeburn`'s `mbregistry run` process, running continuously since
21:19 that evening (~3.5 hours of uptime at the time of testing), had
**completely stopped answering `_mbregistry._tcp` PTR queries** — not
"slow", not "sometimes" — zero reply traffic of any kind on port 5353
related to `_mbregistry`, confirmed directly:

- `loki`'s `avahi-browse -r _mbregistry._tcp -t` query for
  `_mbregistry._tcp.local.` was captured, by `tcpdump` running on
  `braeburn` itself, arriving intact at `braeburn`'s NIC (both its `en0`
  and `en1` addresses independently, since `loki` is itself dual-homed).
- `braeburn`'s own outbound port-5353 traffic during that same capture
  window was 100% macOS system `mDNSResponder` chatter (AirPlay
  `_companion-link`, HomeKit `rp*` records, etc.) — **zero** packets
  referencing `_mbregistry` in either direction, on a live daemon that
  was otherwise fully functional (its TCP control-plane port 7440, PUB
  port 7442, and REP port 7443 were all still bound and had been
  reachable via `--peer braeburn:7440` throughout, matching
  `docs/acceptance/003-hardware.md`'s own workaround finding).

Two controlled comparisons, run immediately after, both succeeded
instantly:

1. A bare `Zeroconf().register_service()` script (same venv interpreter,
   same headless `nohup`-over-SSH launch as the daemon) was discovered by
   `loki`'s `avahi-browse` within seconds of starting.
2. The stale daemon was killed and restarted (`kill <pid>`, then the
   identical `mbregistry run --socket ... --db ...` command under
   `nohup`) — the **freshly-started** process was discovered by `loki`'s
   `avahi-browse` immediately, well under the polling interval used to
   check.

This conclusively narrows the fault to *"something in this process's own
mDNS engine/socket state degrades over sustained uptime on this specific
host"*, not a static misconfiguration any of the three original
candidates could describe (all three are framed as fixed, one-time
conditions — present or absent from process start). `braeburn`'s LAN
segment carries substantial continuous non-`mbtools` mDNS multicast
traffic: ~28 packets/second measured over an 11-second sample (AirPlay/
HomeKit/Continuity chatter from numerous other devices on the same
subnet) — a volume the Nolanet nodes' segment does not see. This is a
plausible contributing environmental factor (e.g. a `python-zeroconf`/
macOS UDP listener socket wedging or losing its receive-buffer state
under sustained heavy ambient multicast load) but is **not confirmed** —
reproducing a multi-hour degradation with instrumentation attached is
beyond this ticket's real-time budget.

**This is reported honestly as open, not silently dropped.** Per the
debugging protocol's own escalation guidance and this ticket's explicit
acceptance criterion allowing "workaround re-confirmed, root cause still
open" after real effort: three named candidates were checked (two ruled
out with direct evidence, one confirmed as a real, fixed, independent
defect), and a fourth, more specific and better-evidenced failure mode
was found and characterized, but not fully root-caused within this
session.

## Current state confirmed working (supporting evidence, not a fix claim)

After the fresh restart above, `loki`'s own already-running
`mbregistry.service` (not a temporary test instance -- the real fleet
daemon, up since before this session) picked up `braeburn`'s advertisement
through its normal, unmodified mDNS browsing and shows it correctly in its
regular `mbregistry list` output, **no `--peer` flag used**:

```
STATE  NAME   UID       FIRMWARE      HOST      PORT
-----  -----  --------  ------------  --------  -----------------------
gone   zugit  0f0a31a9  NEZHA2/robot  braeburn  /dev/cu.usbmodem14602
```

(`STATE=gone` reflects the board's own probe/attach state, unrelated to
peering -- the `HOST=braeburn` tag is what this row proves: discovery and
snapshot convergence both worked, unprompted, from a plain restart.) This
is exactly consistent with this doc's own framing throughout: the system
works correctly from a cold start and degrades only after sustained
uptime, so a snapshot of "is it working right now" taken shortly after a
restart is expected to say yes -- this is supporting evidence for the
"New finding" above, not a claim that the underlying issue is resolved.
Ticket 011's real-hardware acceptance pass should check this again after
`braeburn` has been running for several hours, not just after a fresh
restart, to see whether it still holds.

## Mitigations shipped this ticket

1. **Self-check diagnostic** (`PeerDiscovery._run_self_check`, run every
   60s by default on its own thread): re-resolves this host's own mDNS
   registration through its own `Zeroconf` instance and logs a `WARNING`
   the moment that fails. This is root-cause-agnostic by design — whatever
   causes the next occurrence of this exact failure mode, on any host, it
   is now a log line within one interval instead of a multi-hour hardware
   investigation with packet captures. See `src/mbtools/registry/
   peering.py`'s module docstring ("Self-check diagnostic (ticket 010)")
   and `tests/registry/peering/test_peering.py`'s
   `test_self_check_warns_when_own_registration_stops_resolving`.
2. **Startup interface/address logging**: `PeerDiscovery.start()` now
   logs, at `INFO`, exactly which single address it chose to advertise
   and on which ports — a future multi-homed-host recurrence (candidate 2)
   is a log line to check instead of an `lsof`/`ifconfig` session.
3. The existing `--peer braeburn:7440` (or raw IP) workaround remains
   valid and is re-confirmed working (inbound connections to `braeburn`
   were never affected by any of this — only `braeburn`'s own outbound
   mDNS advertisement/query-answering degraded).

## No regression to Linux-to-Linux peering

`tests/registry/peering/` (62 tests, all passing, run repeatedly to rule
out flakiness from the async-dispatch change above) and the full suite
(`uv run pytest -q`, 775 passed / 2 skipped) both pass. This ticket's code
change (`_PeerLink`'s async snapshot dispatch) applies uniformly to every
peer connection, not a braeburn-specific code path, so the existing
already-working Nolanet-to-Nolanet mDNS/ZMQ peering
(`docs/acceptance/003-hardware.md`) is exercised by the same tests and is
unaffected. Real-hardware re-confirmation across the full fleet (per this
ticket's own acceptance criteria, "verified on real hardware in ticket
011") is ticket 011's scope, not repeated here.

## Residual issue for follow-up (explicitly flagged)

The root cause of `braeburn`'s mDNS engine going silent after several
hours of uptime is **not found**. A future session should: deploy this
ticket's self-check diagnostic fleet-wide, let `braeburn` run for the same
multi-hour window, and either catch the `WARNING` log with a Python-level
stack/thread dump attached at that moment, or instrument `python-zeroconf`
itself (e.g. a patched build logging its internal socket read-loop
exceptions) to see what, if anything, throws. The heavy-ambient-multicast-
traffic environmental factor above is the leading hypothesis but is
unconfirmed.

---

# Ticket 011 — Real-hardware acceptance

Sprint 004, ticket `011-real-hardware-acceptance` (the sprint-closing
ticket — verifies every preceding ticket's claims: 005 `mbrelay` CLI, 006
relay pool, 007 `/names` HTTP API, 008 udev/non-root USB, 009 pyOCD
fail-fast, 010 braeburn mDNS). Run 2026-09-24 against the full fleet:
`meili`, `loki`, `hodr`, `magni` (Debian 13 aarch64, Nolanet nodes) and
`braeburn` (macOS 15.7.4 x86_64), all over SSH as `eric`. `robot-console`
checkout used as the compatibility spec: per this ticket's own dispatch
brief, `/Volumes/Proj/proj/league-projects/microbit/robot-console`
(commit `9f536e76`, 2026-09-22) — the newer of the two local checkouts;
`/Volumes/Proj/proj/robot-projects/robot-console` (commit `8dbc60cb`,
2026-09-14) is the stale copy the ticket text itself warns not to use as
the spec of record.

## Pre-flight

`uv run pytest tests/relay/ tests/registry/console_compat/
tests/registry/peering/ tests/registry/flash/` — 218 passed, before
spending any hardware time (file paths differ from the ticket's own text
per the same per-command-subpackage convention tickets 007/008/009 already
documented; `tests/registry/peering/` and `tests/registry/flash/`, not
`test_peering.py`/`test_flashlogic.py` at the package root).

## Fleet redeploy

All five hosts were running a stale pre-sprint-004 build (`mbrelay names`
answered "not yet implemented — see mbtools sprint 004" on first check) —
redeployed via `scripts/deploy-test-host.sh` after `sudo systemctl stop
mbregistry.service` on each Nolanet node. `uv venv --clear` failed on
three of the four nodes (`hodr`, `magni`, and `meili` on a later
redeploy) with `Permission denied` removing `lib/` — the running-as-root
daemon leaves root-owned files in the venv exactly as
`scripts/deploy-test-host.sh`'s own usage comment warns; `sudo chown -R
eric:eric <venv>` before re-running the script resolved it cleanly each
time (no `rm -rf` needed). `braeburn` had no `mbregistry` process running
at all at session start (unlike `docs/acceptance/003-hardware.md`'s
finding of one already up) — redeployed and started fresh under `nohup`
at the same `--socket /tmp/mbregistry/api.sock --db
~/.local/state/mbregistry/devices.db` paths `docs/acceptance/001-hardware.md`
established. All five `mbregistry.service`/processes confirmed `active`
on the current build (`mbtools==0.20260923.3`) before any scenario below.

## Scenario 1 — firmware flashed onto designated boards

**PASS.** `hodr`'s spare board (`togov`, uid `fe9a0254` — no announcing
firmware at session start, exactly CLAUDE.md's "use a spare board"
guidance) flashed with `League-Robotics/microbit-radio-relay@v0.20260913.2`
(newest versioned release, `MICROBIT.hex` asset, `--force-relay`) —
re-announced `RADIOBRIDGE` cleanly. `meili`'s dedicated robot board
(`gitev`, already `NEZHA2/robot` from earlier sprint work) reflashed with
`League-Microbit/nezha-robot-template@v0.20260919.7` (newest versioned
release) — re-announced `NEZHA2` cleanly, `flash_count=1`. Neither board
is a robot in active use (CLAUDE.md's constraint) — both are this
project's own dedicated per-host test boards. `togov` was reflashed a
second time mid-session to recover from an unrelated identification issue
(see "New finding" below) — `flash_count=2` on that board by the end of
the run.

## Scenario 2 — `mbrelay connect` cross-host, `PING`→pong over radio

**PASS, after two wrong turns worth recording.** All three attempts below
were run from `loki` — a third host, neither the relay's host (`hodr`) nor
the robot's host (`meili`) — satisfying "remote relay" per the ticket.

**First attempt — wrong channel/group.** `nezha-robot-template`'s own
release notes (`.github/workflows/release.yml` in that repo) state "The
robot answers on the radio (channel 55 / group 114)", so `mbrelay names
set gitev 55 114` was used to override the registry (mbtools' own derived
default for `gitev` is channel 21 / group 185 —
`naming.name_to_radio("gitev")` — which does not match that stated value).
`mbrelay connect gitev@hodr --send PING --expect pong` tuned correctly to
55/114 (confirmed via the relay's own `# channel: 55 group: 114` ack) but
got **no answer**. A follow-up low-level diagnostic — driving the
already-locked relay channel directly with `mbtools.relay.channel`/
`mbtools.relay.protocol` primitives, skipping `RelayControl.normalize()`'s
hardcoded `!MODE RAW250` step and sending `!MODE MAKECODE` by hand instead
(no CLI flag exists for this; `relay.protocol.NORMALIZE_STEPS` is
RAW250-only, matching legacy `mbrelay`'s own `relay.py` byte-for-byte —
not a sprint-004 regression) — also got no reply. Source review at the
time (`pxt-nezha-diffdrive`'s `RadioTransport::onDatagram()`) suggested
RAW250 was actually the framing-compatible mode for this firmware's
custom C++ radio layer, not MAKECODE, but that didn't explain why the
*first*, RAW250, attempt had already failed either.

**Second attempt — this is what actually mattered: wrong channel/group,
still.** `mbserial gitev STATUS` (once non-root access was set up — see
Scenario 5) printed an extra, not-in-the-currently-checked-out-source
field set: `wifi=0 radio=1 channel=21 group=185` — the board's *own*,
already-running radio is self-tuned to its own derived address
(`naming.name_to_radio("gitev")` again — 21/185, exactly), not the
55/114 the release notes describe. (`loki`'s own robot board, `vitut`,
independently confirmed the same pattern: `channel=41 group=30`, exactly
`naming.name_to_radio("vitut")`. Both boards were flashed with the exact
same released image, `id diffdrive calibration-0.20260919.7 ...` on
both — this is a property of the *released* firmware's runtime behavior,
not a per-board fluke.) The checked-out `pxt-nezha-diffdrive` source's
`test/boot.ts` reads `diffDrive.setupRadio(55, 114)` literally, so either
that file changed after the tag this release was actually built from (the
`v0.20260919.7` git tag was not present in the local checkout to diff
against directly — `git tag -l` came back empty for it, a shallow-clone
limitation this session didn't chase further) or some later
identity-derived re-tune this session didn't locate overrides it at
runtime. **Not root-caused** — flagged honestly, matching this project's
own precedent for an evidenced-but-not-fully-explained firmware finding
(`docs/acceptance/004-hardware.md`'s own ticket-010 section, "New
finding", above).

**Third attempt — success.** `mbrelay names clear gitev` (drop the wrong
override), then `mbrelay names set gitev 21 185` (the board's own actual
running address), then:

```
$ mbrelay connect gitev@hodr --send PING --expect pong --timeout 6
gitev: channel 21 group 185 (registry)
mbrelay: remote relay togov on hodr: relay togov reset and normalized, firmware 0.20260913.2
mbrelay: tuned to gitev: channel 21 group 185 (source: registry)
mbrelay: gitev answered PING
```

`mbrelay`'s own internal liveness probe (`_tune()`'s `PING`→`\bpong\b`
match, sent automatically after `!GO`) matched — a confirmed `PING`→pong
round trip over radio, relay on `hodr`, robot on `meili`, driven from
`loki`. (The *second*, `--send`/`--expect`-scripted `PING` in the same
session, sent moments later over the now-open data-plane byte pipe,
raced against one of `gitev`'s own periodic `DBG:wifi ...` debug lines
and timed out on the literal word match within its 6s window — a script
timing/multiplexing artifact of firing a second probe manually, not a
failure of the underlying link; the *first*, automatic probe already
proved the round trip.)

## Scenario 3 — remote relay reset over the remote stream

**PASS.** `togov` (on `hodr`) was deliberately left stranded in the data
plane by a local script on `hodr` that ran `!GO` and then closed the port
without any `!DEFAULTS`/BREAK recovery — reproducing "a board parked in
the data plane... nothing short of a reflash recovers it [without BREAK]"
(`microbit-radio-relay/docs/radio-relay-protocol.md`). A subsequent
`mbrelay connect gitev@hodr` **from `loki`** (a third host, over
`RemoteRelayChannel`/`RemoteStream`, never a local BREAK) printed `relay
togov reset and normalized` and proceeded normally — ticket 004's
`RemoteRelayChannel.send_break()` recovered a genuinely stranded board
over the network, real hardware, cross-host.

## Bug found and fixed: remote `stream` op rejected a `relay`-kind lock

While chasing Scenario 2/3 above, the very first `mbrelay connect
gitev@hodr` from `loki` crashed with an uncaught traceback instead of a
clean CLI error:

```
mbtools.registry.client.RegistryClientError: ...fe9a0254...: stream requires
a serial-kind lock held by this connection (call 'lock' first)
```

Root cause: `registry.remote_api.RemoteAPIServer._op_stream_precheck`
(ticket 007, written for `mbserial`'s own remote passthrough) only ever
accepted a `KIND_SERIAL` lock — but `relay.channel.RemoteRelayChannel`
(ticket 004) was always built to lock a remote relay `relay`-kind first,
then call this exact same `stream` op. Every unit/integration test on
both sides of this RPC uses a fake peer
(`tests/relay/test_cli.py`'s `FakeRemoteRegistryClient`,
`tests/registry/remote_api/test_remote_stream.py`'s own real-server tests
only ever locked `serial`-kind), so this cross-module mismatch was
invisible until two real, different-host `mbregistry`s actually talked to
each other. **Fixed**: `_op_stream_precheck` now accepts `KIND_SERIAL`
*or* `KIND_RELAY` (still excludes `flash`/`debug`-kind — neither has any
business opening a raw byte stream). See
`src/mbtools/registry/remote_api.py`'s updated docstring,
`docs/design/registry-api.md`'s "Stream sub-protocol" section, and the
new regression test
`tests/registry/remote_api/test_remote_stream.py::test_stream_with_a_relay_kind_lock_is_accepted`.
Scoped tests (`tests/registry/remote_api/ tests/relay/`, 144 tests) and
the full suite (776 passed, 2 skipped) both pass after the fix.

## New finding (not root-caused, flagged honestly): a relay mid-data-plane can have its registry identity corrupted by the radio traffic it's forwarding

Discovered by accident, immediately after the Scenario 2 success above:
`hodr`'s own `mbregistry list` briefly showed uid `fe9a0254` (physically
`togov`, the `RADIOBRIDGE` relay) as **`gitev`, firmware `NEZHA2/robot`**
— i.e. the *relay's own database row* had been overwritten with the
*robot's* identity. Directly re-querying the physical board
(`mbserial fe9a0254 --reset HELLO`, which forces a BREAK-based reset back
to the command plane first) confirmed the board itself never changed:
`DEVICE:RADIOBRIDGE:relay:togov:2108549556`, exactly as flashed. A plain
`sudo systemctl restart mbregistry.service` (forcing a fresh
attach-time probe) recovered the correct row immediately.

**Working theory** (not confirmed with packet-level evidence, per this
project's own "gather evidence rather than guessing, and say so when the
root cause isn't nailed down" precedent): `togov` had, moments earlier,
been left tuned to `gitev`'s own channel/group (21/185) in RAW250 data-plane
mode by the just-completed `mbrelay connect` session above. `dmesg` on
`hodr` shows the already-documented (CLAUDE.md) `dwc_otg` "Timed out
waiting for FSM NP transfer to complete" USB timing warnings during this
exact window, which is known to flap a board's attach state between "gone"
and its real value. `identity.probe()` (`src/mbtools/registry/identity.py`)
opens the port, writes `HELLO\n`, and accepts *whatever line arrives and
parses* against either the relay or robot announcement dialect within its
read window — it has no way to know the port currently belongs to a relay
that is transparently bridging live radio traffic (RAW250 data plane), so
a radio-forwarded fragment of `gitev`'s own periodic identity/debug
chatter, arriving in that same window, is indistinguishable at the byte
level from the relay's own genuine banner. `identity.is_relay()` already
exists in that module but the daemon's attach/reattach probe pipeline
(`registry.daemon`) never calls it — there is no special-casing today for
"this device's *last known* role was a relay, so reset it (BREAK) before
trusting a fresh HELLO reply as its own."

**Not fixed in this ticket.** This is a real defect, but a proper fix
touches the daemon's shared, heavily-tested attach/reprobe pipeline (used
identically for every device, robot and relay alike) and needs a
reliable repro to validate against — this session's one observation came
from a specific, hard-to-script race (a relay mid-radio-forward at the
exact moment a `dwc_otg`-flapped reattach triggers a reprobe) rather than
a repeatable trigger. Given the daemon pipeline's blast radius, a rushed
fix here risked doing more damage fleet-wide than leaving this
documented. **Recommend a follow-up ticket**: have the daemon's reprobe
path send a BREAK (mirroring `relay.protocol.RelayControl.hello`'s own
already-proven recovery mechanism) before HELLO whenever a device's
*stored* role indicates a relay/bridge, so a reprobe can never observe
anything but the board's own genuine command-plane banner. Fleet state
was left healthy (a service restart on `hodr` cleanly restored the
correct `togov`/`RADIOBRIDGE` row; confirmed before moving on).

## Scenario 4 — robot-console compatibility, against `robot-console`'s own source

Verified directly against the endpoints, the same requests/shapes
`packages/host/src/discovery/mdnsDiscovery.ts`, `mbrelayRegistry.ts`, and
`connect/relayBridger.ts` use (see those files' own doc comments, read in
full this session) — **not** a full headless run of the `@robot-console/host`
package itself: that monorepo has no `node_modules` installed
(`npm install` from scratch, with two native-compiled dependencies —
`node-hid`, `serialport` — plus wiring a full `server.ts`/`cli.ts`
composition root against a real registry, all inside a ticket whose own
subject is `mbtools`, not `robot-console`) was judged out of proportion to
this ticket's timebox and risk budget; the direct-endpoint verification
below exercises literally the same wire contract that package's own code
would.

- **`_mbrelay._tcp` advertisement + TXT `registry=<port>`** — PASS.
  `avahi-browse -rt _mbrelay._tcp` from `loki` (a different host) shows
  all five hosts advertising, instance name = hostname (matching
  `mdnsDiscovery.ts`'s live-verified legacy example, instance `torture` —
  also a *hostname*, not a five-letter board name), port `7444` (the pool
  port), TXT `registry=7445` — parses cleanly against
  `parseRegistryPort`'s `/^\d+$/` check. A legacy `mbrelay` instance
  (`torture`, port `8760`, TXT `registry=8761`) is simultaneously visible
  on the same LAN with no port collision, confirming architecture
  Decision 6's port choice.
- **Pool-port reset-by-reconnect** — PASS. Raw `nc hodr 7444` from `loki`:
  tune to `!CG 55 114` (confirmed via `# channel: 55 group: 114` ack),
  disconnect, reconnect, query `?` — fresh banner
  (`DEVICE:RADIOBRIDGE:relay:togov:...`) and `# channel: 0 group: 10 mode:
  RAW250 power: 7` (factory defaults), every time. Exactly matches
  `relayBridger.ts`'s own documented assumption for an `mbrelay`-transport
  relay: "opening a *fresh* stream for every candidate attempt already
  performs the reconnect" (no BREAK needed/possible over TCP). This test
  needed several retries around the same `dwc_otg` USB flakiness noted
  above (an intermittent "no relay available" while `togov`'s attach state
  flapped) — not a pool-port defect, the same pre-existing, documented
  hardware quirk.
- **`GET`/`PUT`/`DELETE /names/<name>`** — PASS. `curl` from `loki` against
  `hodr:7445`: `GET` on a never-seen name derives-and-persists
  (`{"channel": 35, "group": 97, "source": "derived"}`, HTTP 200,
  idempotent on repeat), `PUT` with a JSON body sets an override
  (`source: "registry"`), `DELETE` re-derives and returns the fresh value,
  a malformed name and an out-of-range channel both come back `400` with
  a clear message. Response shape (`{channel: number, group: number,
  source: string}`) matches `mbrelayRegistry.ts`'s `parseResolvedAddress`
  exactly; mbtools only ever emits `"derived"`/`"registry"` for `source`,
  both of which are in that module's `KNOWN_SOURCES` set (which also
  tolerates a legacy-only `"config"` value mbtools never sends).

## Scenario 5 — non-root USB access, including idempotent re-install

**PASS**, on all four Nolanet nodes (the ticket requires one; all four
were done for genuine fleet value going into sprint 005). Per host:
`mbregistry install-service` (writes the systemd unit + the three-rule
udev file — tty/CDC-ACM, raw USB, `hidraw*`, all `GROUP="plugdev"
MODE="0660" TAG+="uaccess"`), `udevadm control --reload-rules && udevadm
trigger`, `usermod -aG plugdev eric`, then **a fresh SSH session**
(ticket 008's own documented caveat — an already-open session does not
pick up the new group). Confirmed in the new session: `groups` shows
`plugdev`; `mbserial <board> STATUS`/`"?"` and `mbregistry list` all work
with **no `sudo`** on `meili`, `loki`, `hodr`, `magni`.

**Idempotent re-run**, on `loki` (service already `active`): re-running
`install-service` rewrote both files with byte-identical content (confirmed
`md5sum` unchanged across the two runs), never touched
`mbregistry.service`'s running state (`active` before and after, no
restart), and non-root `mbserial` access kept working immediately after.

**pyOCD fail-fast**, real permission failure (not simulated): on `hodr`,
*before* `install-service` had been run there, `eric` (not yet in
`plugdev`) ran `mbdeploy deploy togov --hex <local .hex> --force-relay`
with no `sudo`:

```
Error: permission denied opening /dev/ttyACM0 -- this user has no read/write
access to the device. Install the udev rule (run 'mbregistry install-service'
as root -- ticket 008) then start a new session ... or run this command with
sudo.
mbdeploy: flash failed (exit 1)

real  0m1.408s
```

1.4 seconds, no pyOCD invocation at all (the pre-check catches it first),
a clear actionable message — exactly ticket 009's "report within seconds"
criterion. (`mbdeploy debug`'s own fail-fast is deliberately out of that
ticket's scope, per its own Implementation Notes — not re-tested here for
the same reason.)

## Scenario 6 — braeburn mDNS discovery without `--peer`

**PASS at this session's uptime; the multi-hour degradation itself
neither reproduced nor ruled out.** `braeburn`'s `mbregistry` was started
fresh this session (none was running at all at session start, unlike
`docs/acceptance/003-hardware.md`'s finding of one already up) and stayed
discoverable from a Nolanet node throughout: `avahi-browse -rt
_mbregistry._tcp` on `loki`, **no `--peer` flag**, found `braeburn`
immediately at session start and again ~27 minutes later (this session's
own real-time budget did not extend to the ~3.5-hour window
`docs/acceptance/004-hardware.md`'s own ticket-010 section measured the
degradation at) — `mbregistry list` on `loki` shows `braeburn`'s board
(`zugit`) with `HOST: braeburn`, unprompted. Ticket 010's self-check
diagnostic (a `WARNING` log the moment `braeburn`'s own mDNS
re-resolution fails) is the intended way a future session catches a
recurrence without repeating this multi-hour investigation; nothing in
`braeburn`'s own log during this run's ~27-minute window triggered it.
The `--peer braeburn:7440` workaround was not applied as a standing
config change this session (matching `docs/acceptance/004-hardware.md`'s
own note not to assume it is set without checking).

## Full test suite

`uv run pytest -q`: **776 passed, 2 skipped** (post-fix; the ticket's own
pre-flight, pre-fix run was the 218-test scoped subset above). Scoped
regression run for the `remote_api` fix,
`tests/registry/remote_api/ tests/relay/`: 144 passed.

## Summary

| Scenario | Result |
|---|---|
| Relay + robot firmware flashed onto designated (spare/dedicated) boards | PASS |
| `mbrelay connect` cross-host, confirmed `PING`→pong over radio | PASS (after correcting to the robot's own self-addressed channel/group — see Scenario 2) |
| Remote relay reset over the remote stream | PASS |
| robot-console compatibility (mDNS/TXT, pool-port reset-by-reconnect, `/names`) | PASS, verified by direct endpoint exercise against `robot-console`'s own source (full headless run not attempted — see Scenario 4) |
| Non-root `mbdeploy`/`mbserial`, including idempotent re-install | PASS (4/4 Nolanet nodes) |
| pyOCD fail-fast under a real permission failure | PASS (~1.4s) |
| braeburn mDNS discovery without `--peer` | PASS at this session's (~27 min) uptime; multi-hour degradation neither reproduced nor newly ruled out |

**One real bug found and fixed this ticket** (remote `stream` op rejected
a `relay`-kind lock — see above, with a regression test). **One real bug
found and documented, not fixed** (a relay mid-data-plane can have its
registry identity corrupted by the radio traffic it's transparently
forwarding, if a reprobe races it — see "New finding" above; recommend a
follow-up ticket). Every other scenario passed on real hardware as
designed. No changes were needed to `docs/wiki/` (this repository still
has none — ticket 005/006/007/008's own Implementation Notes already
established `docs/design/specification.md` and `docs/design/registry-api.md`
as this project's documentation home in its absence) beyond the
`registry-api.md` update accompanying the `stream` op fix above.
