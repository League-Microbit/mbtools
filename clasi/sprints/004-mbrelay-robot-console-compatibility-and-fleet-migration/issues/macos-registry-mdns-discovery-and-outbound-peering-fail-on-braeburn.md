---
status: in-progress
sprint: '004'
tickets:
- 004-010
- 004-011
---

# macOS registry (braeburn): not discoverable by mDNS, and cannot reach peers

## Description

Found in sprint 003 hardware acceptance (`docs/acceptance/003-hardware.md`),
not root-caused:

- **Nobody discovers braeburn.** No Linux peer ever discovered braeburn's
  `_mbregistry._tcp` advertisement. Raw `avahi-browse` on the nodes never saw
  it.
- **braeburn's snapshot requests hang.** braeburn's own `mbregistry` daemon
  could not complete an outbound ZeroMQ snapshot request to any peer. A bare
  script on the same machine succeeded every time.
- **Inbound works.** Peers that used `--peer braeburn` reached it reliably.

Hypotheses already refuted: the host firewall, DNS resolution, and sharing
one zeroconf/zmq context.

Candidates still to check:
- the macOS Application Firewall or local-network privacy prompt, which
  applies per binary (the daemon runs from a venv's python under `nohup`,
  which may be treated differently from an interactive script);
- which interface or address zeroconf binds or advertises on macOS;
- whether the daemon's REQ socket is created before the event loop starts.

## Done when

braeburn peers with the Nolanet nodes over mDNS with no `--peer`, in both
directions, confirmed on hardware.
