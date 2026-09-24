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
