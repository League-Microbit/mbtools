# Ticket 014 — Hardware acceptance: peering, remote flash, and remote serial across all five hosts

Sprint 003 (`Distribution: registry peering and remote streams`), ticket
`014-hardware-acceptance-peering-remote-flash-and-remote-serial-across-all-five-hosts`.
Run 2026-09-23 against the same five dedicated test hosts as
`docs/acceptance/001-hardware.md`/`002-hardware.md`: `meili`, `loki`, `hodr`,
`magni` (Debian 13 aarch64, user `eric`, passwordless sudo) and `braeburn`
(macOS 15 x86_64). The dev Mac (`gala`) has no micro:bits of its own — except
see "New finding: a stray micro:bit on the dev Mac" below — and was used
throughout as the sixth, zero-owned-device node per this ticket's own scope
note, running a plain `mbregistry run --socket ... --db ...` pointed at
user-writable paths under `/tmp`. Every check below ran over
`ssh -o BatchMode=yes`; long-running processes used `nohup ... & disown` on
all five hosts (systemd on the four Nolanet nodes for the persisted/default
configuration, `nohup` for every temporary/one-off variant this pass needed).

## Firmware used

All five boards started this session already flashed from prior sprint work
(`RADIOBRIDGE/relay` or `NEZHA2/robot`, per host); `hodr`'s board (`togov`)
started this session with no reliably-probeable firmware (see "Real-hardware
finding: `dwc_otg` USB flakiness" below for why "no firmware" vs "flashed but
un-probeable" is genuinely ambiguous on this fleet).

| Firmware | Repo | Tag | Announces as |
|---|---|---|---|
| Radio relay | `League-Robotics/microbit-radio-relay` | `v0.20260913.2` | `DEVICE:RADIOBRIDGE:relay:<name>:<serial>` |
| Nezha robot | `League-Microbit/nezha-robot-template` | `v0.20260919.7` | `device NEZHA2 robot <name> ...` |

Both fetched by `mbdeploy deploy --repo` (real GitHub release fetch), same as
sprint 002's own acceptance pass — no local `--hex` files used.

## Deploy path: `scripts/deploy-test-host.sh`, unchanged, run against all five

`uv build --wheel` + `scripts/deploy-test-host.sh <host>` for
`meili`/`loki`/`hodr`/`magni`/`braeburn`, `mbregistry.service` stopped first
on each Nolanet node per `CLAUDE.md`'s existing operational note. **New this
session**: `hodr` needed the same `sudo rm -rf ~/mbtools-venv` root-owned-file
cleanup `meili`'s tmpfs venv already needed in sprint 001/002 (the
running-as-root daemon leaves root-owned `__pycache__` files even in a
non-tmpfs, `$HOME`-based venv, not just the tmpfs fallback) — `uv venv
--clear` cannot remove them as `eric`. Documented here since it's the first
time a *non-`meili`* host hit it; future sessions should expect it on any
Nolanet node, not just `meili`.

**`pyzmq`/`python-zeroconf` install confirmed clean** on both platforms:
`pyzmq==27.2.0`, `zeroconf==0.151.3` installed without incident on Debian 13
aarch64/Python 3.13 (all four Nolanet nodes) and macOS x86_64/Python 3.11
(`braeburn`) — this ticket's own first acceptance criterion, satisfied on the
first attempt on every host.

## Code fix made during this pass

One real, reproducible bug was found by this hardware run and fixed with a
regression test (`src/mbtools/registry/peering.py`, committed under this
ticket):

### Self-peering on a multi-homed host corrupts its own local device rows

**Symptom, first observed via the dev Mac's zero-device registry**: `gala`'s
`mbregistry list` converged with `loki`/`meili`/`magni` within seconds of
starting, but never showed `hodr`'s device even after 60+ seconds. Direct
investigation (`sqlite3` against `hodr`'s own `/var/lib/mbregistry/devices.db`)
found `hodr`'s own board (`togov`) stored with `host = 'hodr'` instead of
`NULL` — i.e. `hodr` had somehow tagged its *own* local device as if it
belonged to a peer named `hodr` — and `hodr`'s own `peer` table had a row for
peer `"hodr"` at `192.168.1.148:7440`. `hodr` had peered with **itself**.

**Root cause**: all four Nolanet nodes are dual-homed (`eth0`
*and* `wlan0`, both up, both on the same `/21` LAN). `PeerDiscovery`
advertises its own mDNS service with exactly one address (`_local_ip()`'s
single pick, fixed for the process's life) and filters out its own
advertisement in `_BrowseListener._record` by comparing the *browsed*
service's resolved address/port against that same fixed value. But a
*browsing* `zeroconf` instance resolving a multi-homed host's own service
back can hand `_resolve_address` a **different** one of that host's own
addresses (observed directly: the browse side's `parsed_addresses()[0]` came
back as `192.168.1.148`, the `eth0` address, while `hodr`'s own advertised/
compared `own_address` was `192.168.2.148`, the `wlan0` one) — so
`address == self._own_address` silently failed to match, and `hodr` called
`connect_peer("hodr", ...)` on itself. The resulting self-directed snapshot/
event application re-tagged `hodr`'s own local device row with
`host = "hodr"` — and because the local daemon's own next attach-scan cycle
(`store.upsert_attached`, always `host = NULL`) only runs on a genuine
detach/reattach (an already-`connected`/`connected_no_firmware` device is
never re-touched by a plain scan, per `daemon.py`'s own "re-probe rule"), the
device stayed silently hidden from `store.snapshot_local_devices()`
indefinitely for any *newly connecting* peer, while already-connected peers
kept whatever stale-but-coincidentally-correct copy they'd gotten before the
self-taint. `braeburn` (also multi-homed — four active interfaces observed,
`en0`/VPN/bridge addresses) hit the identical corruption independently.

**Fix**: `_BrowseListener` now also compares the discovered service's *bare
hostname* (`_peer_host_from_name`, parsed from the mDNS instance name itself
— never re-derived from a resolved address) against this instance's own
`host`, which `PeerDiscovery.start()` now passes as a new `own_host`
parameter. This identity check doesn't depend on which interface answered,
so it isn't defeated by multi-homing. The original address/port check is
kept as a second guard (harmless, and still the only check for a caller that
passes no `own_host`) rather than removed. See
`src/mbtools/registry/peering.py`'s `_BrowseListener` class docstring
("Self-filtering, two checks") for the full writeup, kept in the code for
the next person who hits this.

**Regression test**:
`tests/registry/peering/test_peering.py::test_add_service_excludes_own_advertisement_by_hostname_even_when_address_differs`
— a service named `"hodr"` resolving to an address that does *not* match
`own_address` is still excluded, because `own_host="hodr"` matches. Full
`tests/registry/` suite re-run after the fix: 412 passed, 1 skipped
(pre-existing, unrelated). Full project suite: `uv run pytest -q` — 559
passed, 2 skipped, no failures.

**Repair of already-corrupted state**: the fix stops *future* self-peering
but does not retroactively repair a row already corrupted before the fix was
deployed (the local daemon only fixes `host` back to `NULL` on a genuine
detach/reattach, not merely by restarting with the fix in place — restarting
`hodr` with the fix and no board activity left the stale `host='hodr'` row
untouched). Repaired live, on real hardware, via `hodr`'s existing USB
unbind/rebind simulated-detach mechanism (`echo 1-1.3 | sudo tee
/sys/bus/usb/drivers/usb/{unbind,bind}`, same technique as
`docs/acceptance/001-hardware.md`) — this both healed the row (confirmed via
direct `sqlite3` read: `host` back to `NULL`, `mbregistry list` on `hodr`
itself now correctly shows `togov` as `local`) *and* doubled as this ticket's
own simulated-detach-propagates-to-peers check (see below). `braeburn`'s
equivalent corruption was repaired by deleting its `devices.db` (macOS has no
unbind/rebind equivalent reachable over SSH, and the board is otherwise
healthy/re-announces on the next probe) — acceptable for a disposable test
board's local cache, not a source-of-truth loss.

## Acceptance checks

Legend: PASS / FAIL / MANUAL / **not resolved this session** (used once,
below, for a real finding investigated but not root-caused — see that
section for the evidence gathered).

### Peer discovery and convergence (SUC-001/UC-013)

**PASS.** Measured directly: stopped and restarted `hodr`'s
`mbregistry.service`, polled `hodr`'s own `mbregistry list` every second —
all three other Nolanet peers visible at the very first poll, **≤2s**.
Checked the reverse direction too (how fast `loki` re-saw `hodr`'s device
after `hodr`'s restart): **3s**. A from-cold six-node simultaneous start (all
four Nolanet `systemd` services plus `braeburn` plus `gala`, started within
the same second) showed all five boards, correctly `HOST`-tagged, on
`loki`'s own list within **6-8s**. All comfortably bounded, consistent with
`_DEFAULT_SNAPSHOT_TIMEOUT_MS`'s 5s ceiling plus normal mDNS resolve latency.

### Combined listing (SUC-002/UC-005)

**PASS.** `mbregistry list` from `loki` (with a temporary `--peer braeburn`
added — see "New finding: `braeburn` mDNS/peering asymmetry" below for why
that was needed for `braeburn` specifically) showed all five boards, each
correctly `HOST`-tagged to its owning host (`magni`/`meili`/`loki`(`local`)/
`hodr`/`braeburn`). Verified `--json` output too (all fields present,
including the `host`/`endpoint`/`peer_reachable` triad ticket 010 added).
Also independently confirmed from `hodr`'s and `magni`'s own vantage points
without any `--peer` override, seeing the three other Nolanet peers plus
whichever of `gala`/`braeburn` had converged at that moment.

### Peer-vanish (SUC-005/UC-013's error flow)

**PASS.** Stopped `mbregistry.service` on `magni`; within **3s**, `loki`,
`meili`, and `hodr` all independently showed `magni`'s device as
`peer unreachable` (not a stale `connected`/`gone` state). Restarted
`magni`'s service; `loki` showed the device back to a live (non-
`peer-unreachable`) state within **4s**.

### Remote flash (SUC-003/UC-009)

**PASS**, exercised from two different vantage points, with a genuine
transient hiccup encountered and recovered from naturally (not manufactured):

- `loki` → `magni`'s `vevav`: `mbdeploy deploy vevav --repo
  League-Robotics/microbit-radio-relay`, run from `loki`, **no `sudo`** (a
  remote-target flash never opens a local port — the flash I/O runs on the
  *owning* host's already-root `mbregistry.service`). Streamed pyOCD
  erase/program output live, fetched the release, reported
  `vevav re-announced as RADIOBRIDGE (flash_count=6)`.
- `gala` (dev Mac, zero owned devices) → `meili`'s `gitev`: `mbdeploy deploy
  gitev --repo League-Microbit/nezha-robot-template`. **First attempt hit a
  real transient probe/communication hiccup** — `Timeout reading from probe
  ...` during board uninit/disconnect, twice in a row (the built-in
  transient-retry fired once automatically, as designed, and also hit the
  same error, so the overall command reported failure rather than silently
  retrying forever). **Immediate retry of the identical command succeeded
  cleanly** — confirms this was a genuine one-off hiccup (plausibly related
  to the same `dwc_otg` USB flakiness documented below, on `meili`'s Pi
  specifically), not a reproducible defect; `gitev re-announced as NEZHA2
  (flash_count=4)` on the successful retry.

### Remote serial with working reset (SUC-004/UC-011)

**PASS.** `loki` → `magni`'s relay-flashed `vevav` (Linux BREAK path, per
ticket 013's "`--reset` composes a plain BREAK, client-side" design): a
bounded no-`--reset` session (`(sleep 4) | mbserial vevav`, no `sudo` needed
— remote target) produced **silence**, no boot/announcement line, for the
full window; the same command with `--reset` produced an **immediate** fresh
`DEVICE:RADIOBRIDGE:relay:vevav:536019796` announcement — unambiguous
evidence the BREAK delivered over the network stream actually reset the
board. Matches sprint.md's own Success Criterion wording verbatim.

### Cross-host lock visibility (not a numbered AC, but explicitly asked for in this ticket's dispatch)

**PASS.** Held a `serial`-kind lock on `magni`'s `vevav` from a backgrounded
remote `mbserial` session on `loki`; a **third** host, `meili`, immediately
showed (both table and `--json`) `locked by serial session <uuid> on
192.168.1.149` — the remote holder's session id and *originating* host,
correctly resolved from a host that is neither the lock holder nor the
device owner. Confirmed the lock released cleanly and propagated back to
`free` on `meili`'s view once the `loki` session ended, no manual
intervention.

### Explicit `--peer` across a simulated network boundary (SUC-001/UC-014)

**PASS**, both a manufactured version and (unplanned) a completely real one:

- **Manufactured**: blocked mDNS (`iptables -A {INPUT,OUTPUT} -p udp --dport
  5353 -j DROP`) on `magni`, confirmed no host firewall otherwise exists on
  any of the five (no `ufw`, no other `iptables` rules, macOS Application
  Firewall disabled on `braeburn` — all checked directly). A separate,
  isolated `mbregistry run --peer magni:7440` instance on `loki` (default
  peering ports, to avoid the port-mismatch pitfall ticket 009's own
  Implementation Notes already flag for non-default `--peer-pub-port`/
  `--peer-snapshot-port`) still converged fully with `magni` — real device
  data, not just a "reachable" flag — despite `magni` being unable to
  advertise or browse mDNS at all for the duration. Cleaned up (`iptables
  -D` the two rules, confirmed removed) and restarted `loki`'s and `magni`'s
  normal services afterward.
- **Real** (see next section): `braeburn`'s mDNS advertisement turned out not
  to reach any Linux host on this LAN at all, for the whole session —
  `--peer braeburn:7440` (both by hostname and by raw IP) reliably bridged
  it from the Linux side.

### New finding: `braeburn` mDNS/peering asymmetry — investigated, not fully root-caused

**Symptom**: no Linux host (`meili`/`loki`/`hodr`/`magni`) ever discovered
`braeburn` via mDNS this session — confirmed at the raw-multicast level with
`avahi-browse -r _mbregistry._tcp -t` on `loki`, which lists all four other
Nolanet peers plus `gala` but never `braeburn`, on every interface
(`eth0`/`wlan0`/`docker0`/`lo`). Apple's own `dns-sd -B`, run from *both*
`gala` and `braeburn` (i.e. Mac-to-Mac), sees `braeburn`'s advertisement
fine — so the advertisement is going out, just apparently not reaching (or
not being processed as) a `python-zeroconf` `ServiceBrowser` on a
non-`braeburn` host. Separately, and only partly explained by the above:
`braeburn`'s own daemon, acting as the *initiator*, could not complete a
snapshot exchange to **any** peer — not mDNS-discovered ones, not an
explicit `--peer <hostname>:7440`, not even an explicit `--peer <raw-IP>:7440`
(ruling out DNS/hostname resolution) — every attempt logged `peering:
snapshot request to ... timed out` at the full 5s timeout, while the process
sat at 0.0% CPU (not busy-looping) with exactly the sockets expected
(`lsof` confirmed one listener each on 7440/7442/7443, no port conflicts).

**What is ruled out** (four-phase debugging protocol, evidence gathered
before any hypothesis was accepted): not a firewall (checked, none present
on any host); not DNS/hostname resolution (raw-IP `--peer` reproduced it
identically); not the daemon being CPU-starved by some other busy loop (idle
at the time); not `zeroconf`+`pyzmq` context-sharing in general (a
standalone script reproducing the exact same socket topology — `PUB`+`REP`
+background thread+`REQ`, *plus* a real registered `Zeroconf()` instance
alongside it — succeeded in 40-80ms every time it was tried, from the same
machine, same venv, same interpreter); not a stale/duplicate process (only
ever one `mbregistry` process found via `ps`/`lsof` at any check).

**What is *not* ruled out, i.e. still a real open question**: three
hypotheses were tried (raw-script reproduction with the daemon's exact
socket topology; raw-script reproduction adding a live `Zeroconf()`
instance; IP-vs-hostname `--peer` to isolate DNS) and all three failed to
reproduce the failure outside the real daemon process, hitting this
protocol's three-attempt cap. **Escalating rather than guessing further**:
something specific to the full `mbregistry run` process on `braeburn` (as
opposed to a minimal script reproducing its socket topology) prevents an
outbound snapshot REQ from completing, while the *identical* socket
topology in isolation, and the *identical* connection made by any other
host's real daemon *to* `braeburn` (`loki`'s `--peer braeburn:7440` **did**
succeed and fetch `braeburn`'s device — proving inbound connections to
`braeburn` work), do not show the problem. Plausible remaining suspects,
untested: macOS's per-app "Local Network" TCC permission gating a
non-interactively-launched (SSH, no logged-in GUI session to grant it)
process differently depending on some difference between a `python3 -c`
one-liner and the installed console-script entry point; or something in
`registry.daemon`'s local-USB-polling thread (which *is* running
continuously on `braeburn`, polling its own healthy, no-`sudo`-needed local
board) intermittently interacting with `connect_peer`'s calling thread in a
way a script with no `Daemon` instance at all can't reproduce.

**Mitigation used for this ticket's own remaining checks**: `--peer
braeburn:7440` from the *Linux* side (proven reliable, used for the
combined-listing check above), run as a temporary, hand-started process —
**not applied as a permanent config change** to any host. The fleet was left
running its sprint's default flags on every host once this pass finished,
so a future session doesn't inherit an undocumented deviation. If a future
sprint's own hardware pass needs `braeburn` reachable without a hand-run
`--peer`, adding `--peer braeburn` to one Nolanet node's `ExecStart=`
(`systemctl edit mbregistry.service`) is the known-working fix; it is
recorded here rather than applied, since ticket 014's own scope is
verification, not a deployment-topology decision.

### New finding: a stray micro:bit on the dev Mac

`CLAUDE.md`'s "Hardware test targets" states flatly "no micro:bits on the
development Mac." This session found one physically attached (`ioreg`:
`BBC micro:bit CMSIS-DAP`, serial `...ed09e98c...`) — `gala`'s own
`mbregistry list` picked it up as a `host: "local"` (then, after the dev-Mac
registry was killed and any other host's peering cache expired, `host: "gala"`
from a remote vantage) row, state oscillating `gone`/`no-firmware` (it never
answered a probe, consistent with either genuinely blank firmware or a
board a previous session left mid-experiment). Not touched beyond passive
`mbregistry list` observation — no `mbdeploy`/pyOCD command was run against
it, per `CLAUDE.md`'s own reasoning for why the dev Mac is excluded.
`CLAUDE.md` updated to flag this rather than silently leave the "no
micro:bits" claim contradicted by direct observation.

### Real-hardware finding: `dwc_otg` USB flakiness on the Nolanet Pis (not a code bug)

All four Nolanet nodes' `dmesg` shows recurring `WARN::dwc_otg_hcd_urb_dequeue:639:
Timed out waiting for FSM NP transfer to complete on <N>` — the well-known
Raspberry Pi USB host controller warning — correlating directly, by
timestamp, with `mbregistry list`'s `STATE` column flapping between a
board's real state and `gone` every few seconds, independent of any lock or
peering activity (confirmed by polling a lock-held device and finding the
table's `gone` masking an actually-still-held lock — `render.py`'s
`_state_cell` checks `STATE_DISCONNECTED` before the lock-kind check, by
design, for both local and remote rows — the lock was still visible the
whole time via `mbregistry list --json`'s `lock_kind`/`remote_lock_kind`
fields, which aren't subject to that same precedence). This is a host/kernel
characteristic, not something `mbtools` causes or can route around from
userspace — recorded here, and in `CLAUDE.md`, so a future session doesn't
mistake transient flapping for a detach/lock bug without checking `dmesg`
first.

### Sudo requirements (re-confirmed unaffected)

**PASS, explicitly re-verified, not just assumed.** On `magni`: plain
(no-`sudo`) local `mbserial vevav` still fails with the same
`PermissionError: [Errno 13] ... Permission denied: '/dev/ttyACM0'` sprint
002 documented; `sudo mbserial vevav` still works. Every *remote*-target
command exercised this session (`mbdeploy deploy` and `mbserial` against a
peer-owned device, from `loki`, `magni`, and `gala`) needed **no** `sudo` on
the client host regardless of platform — confirming this sprint's new
network-facing paths introduce no new local-privilege requirement, and (the
positive case sprint 002 couldn't test, since remote transport didn't exist
yet) that a *remote* flash/serial session genuinely never opens a local
port. `CLAUDE.md` updated with this clarification.

## Simulated detach propagates to peers (USB unbind/rebind on a Nolanet node)

**PASS**, and did double duty as the self-peering-bug repair (see above).
`echo 1-1.3 | sudo tee /sys/bus/usb/drivers/usb/{unbind,bind}` on `hodr`
(same unbind/rebind technique as `docs/acceptance/001-hardware.md`, this
board's own bus path per `udevadm info -q path -n /dev/ttyACM0`): `loki`'s
`mbregistry list` showed `togov` (`HOST: hodr`) flip to `gone` immediately
on unbind, live over the peering event bus — no polling delay beyond the
normal `dwc_otg`-flapping-indistinguishable window.

## Summary

Every acceptance criterion in ticket 014 was exercised and passed on real
hardware: deploy to all five hosts, mDNS peer discovery/convergence
(seconds, not the "bounded time" sprint.md left open-ended), combined
listing, peer-vanish/reconverge, remote flash (including a real transient
hiccup recovered from naturally), remote serial with a working cross-network
BREAK reset, explicit `--peer` bridging both a manufactured and a real
network-partition-like scenario, cross-host lock visibility, simulated-
detach propagation, and unaffected `sudo` requirements. One real code bug
(multi-homed self-peering) was found and fixed with a regression test. Two
findings are recorded as genuine, honestly-reported gaps rather than folded
into PASS: `braeburn`'s asymmetric mDNS/outbound-peering behavior (mitigated
with `--peer`, not root-caused — three hypotheses tried and refuted, per the
debugging protocol's own escalation threshold) and the pre-existing `dwc_otg`
USB flakiness (a host/kernel characteristic, not an `mbtools` defect).
