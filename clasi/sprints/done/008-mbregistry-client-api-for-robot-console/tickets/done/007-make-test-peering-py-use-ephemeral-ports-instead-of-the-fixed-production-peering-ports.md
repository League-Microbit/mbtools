---
id: '007'
title: Make test_peering.py use ephemeral ports instead of the fixed production peering
  ports
status: done
use-cases: []
depends-on: []
github-issue: ''
issue: mbregistry-api-for-robot-console-watch-lock-label-unlock-force-local-stream.md
completes_issue: false
---
<!-- CLASI: Before changing code or making plans, review the SE process in CLAUDE.md -->

# Make test_peering.py use ephemeral ports instead of the fixed production peering ports

## Description

`tests/registry/peering/test_peering.py` binds the real default ZMQ
peering ports (7442/7443, possibly 7440). That fails with "Address
already in use" whenever a real `mbregistry` is running on the dev
machine — which it now is, via a per-user LaunchAgent on the dev Mac.
12 tests currently fail this way, which blocks `close_sprint`'s
full-suite run.

Change the tests (and any fixture they share) to bind ephemeral/
OS-assigned ports, or high random free ports if the code needs a port
number up front before binding. Add a minimal test seam in
`PeerDiscovery` only if strictly needed to make this possible.

Separately, check whether any test's `PeerDiscovery` advertises over
real mDNS on the LAN rather than a mocked/local-only responder — the
Mac's real daemon log shows it discovering test peers `peer-a`/`peer-b`
at `tcp://127.0.0.1:17442`/`27442`, meaning test mDNS advertisements
are leaking onto the LAN. If that's cheaply fixable (mock `zeroconf`,
or use a non-production mDNS service type in tests), fix it as part of
this ticket. If it isn't cheap, leave it and document the gap in this
ticket's Implementation Notes instead of fixing it.

## Acceptance Criteria

- [x] `uv run pytest tests/registry/peering -q` passes while a real
      `mbregistry` holds ports 7440-7445 on the machine.
- [x] `grep`ing `tests/` confirms no test binds a port in the
      7440-7445 range.
- [x] The real-mDNS-leak question is checked and the outcome (fixed, or
      why not cheaply fixable) is recorded in this ticket's
      Implementation Notes.

## Testing

- **Existing tests to run**: `uv run pytest tests/registry/peering -q`
  with a real `mbregistry` running locally (holding 7440-7445), to
  confirm the fix actually addresses the reported conflict.
- **New tests to write**: none expected — this is a fixture/test-only
  change; existing peering tests should continue to pass unmodified in
  behavior, just on different ports.
- **Verification command**: `uv run pytest tests/registry/peering -q`

## Implementation Notes

**Root cause.** `PeerDiscovery.start()` always binds two *real* ZMQ
sockets (`self._pub_socket.bind(f"tcp://*:{self._pub_port}")` and
`self._rep_socket.bind(f"tcp://*:{self._snapshot_port}")`) regardless of
whether the injected `zeroconf` module is faked — faking `zeroconf` (the
pattern every non-integration test in this file already used) only
avoids a real mDNS socket, it does nothing about the ZMQ PUB/REP binds.
12 of `test_peering.py`'s tests constructed `PeerDiscovery` with no
`pub_port`/`snapshot_port` override (or passed the literal defaults
7442/7443 explicitly), so all 12 tried to bind the real production
ports the dev Mac's `org.jointheleague.mbregistry` LaunchAgent already
holds, and failed with "Address already in use". This exactly matches
the 12-test count and port numbers reported in this ticket.

**Fix.** Gave each of those 12 tests its own unique, non-production
`pub_port`/`snapshot_port` (and `remote_port` where present, for
cleanliness — `remote_port` itself is never bound by this module, only
advertised) in the 18440-18543 range, so no two tests contend for a
port even though pytest runs them serially in one process. One test,
`test_start_uses_default_ports_when_not_overridden`, existed
specifically to prove the constructor falls back to
`DEFAULT_REMOTE_PORT`/`DEFAULT_PUB_PORT`/`DEFAULT_SNAPSHOT_PORT`
(7440/7442/7443) when the caller omits them — by definition it cannot
use different ports and still test that. Rewrote it to assert directly
on the constructor-time `pd._remote_port`/`pd._pub_port`/
`pd._snapshot_port` attributes instead of calling `start()`, which
proves the same fallback without ever binding a socket. No source
changes to `PeerDiscovery` were needed — the existing `zeroconf=`/`zmq=`
constructor seams were sufficient once ports were the actual problem
being solved, and `zmq=` wasn't needed at all since the fix is "use a
free port", not "fake the socket".

**mDNS-leak finding — fixed, not just documented.** Checked which test
advertises over real LAN mDNS: `test_real_zeroconf_loopback_two_
registries_discover_each_other` is the package's one deliberate
real-`zeroconf` integration test (every other test injects the fake
`zeroconf` namespace). It registers hosts `peer-a`/`peer-b` under the
real, unparameterized `SERVICE_TYPE` ("_mbregistry._tcp.local."), which
is exactly what a real `mbregistry` daemon on the same LAN browses for
— confirmed directly: before any fix, running this test caused the dev
Mac's LaunchAgent to log `peering: link to peer-a (tcp://127.0.0.1:
17442) dropped` / same for peer-b. This was cheap to fix without
losing the test's real-zeroconf coverage: `PeerDiscovery` already
accepts a `service_type` override (added for other reasons, unused by
this test). Passed a dedicated, non-production
`"_mbregistry-test._tcp.local."` type to both `PeerDiscovery` instances
in this one test. Verified empirically, not just by inspection: with
the real LaunchAgent running throughout, ran only this test repeatedly
against fresh, previously-never-used ports (19440s/29440s, chosen to
rule out reacting to a stale pre-fix peer link at the old 17442/27442
addresses) and watched `~/Library/Logs/mbregistry.log`'s line count —
it did not grow across two consecutive runs, whereas the original
17442/27442 numbers kept producing "dropped" log lines run after run
(a stale `_peer_links` entry the real daemon had already established
against those exact addresses *before* this fix existed; `_BrowseListener.
remove_service` is a documented no-op so that stale in-memory link
persists for the LaunchAgent's remaining uptime regardless of this
fix — confirmed by static reasoning, not something restarting the
LaunchAgent to verify was an option, since the ticket's own hardware
rule for this session forbids stopping/restarting it). The real
daemon's browser only ever watches the real `SERVICE_TYPE`, so it
structurally cannot see a `_mbregistry-test._tcp.local.` advertisement
going forward.

**Verification.** `uv run pytest tests/registry/peering -q` (87 tests,
all pass) and the full suite (`uv run pytest -q`, 1218 passed / 3
skipped) both ran with the real LaunchAgent up throughout; its log
stayed at the same line count before and after both runs. `grep -rn
"pub_port=744\|snapshot_port=744" tests/` returns nothing, and every
remaining `74[0-5]` hit across `tests/` (checked file by file) is
inert metadata — a constant assertion, CLI-arg-parsing input, or a
`Store.record_peer_seen` endpoint string — never an actual socket
bind.
