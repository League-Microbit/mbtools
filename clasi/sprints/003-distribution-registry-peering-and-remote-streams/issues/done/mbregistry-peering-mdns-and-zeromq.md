---
status: done
sprint: '003'
tickets:
- 003-001
- 003-002
- 003-004
- 003-005
- 003-006
- 003-009
- 003-010
- 003-011
- 003-014
---

# mbregistry peering: mDNS discovery plus a ZeroMQ attach/detach event network

## Description

Make `mbregistry` distributed, so a client on any host can see and use
micro:bits on every host in the network (brief §3.7).

## Scope

- **Advertise and browse.** Each registry advertises one mDNS service (e.g.
  `_mbregistry._tcp`) and browses for the others. mDNS is used **only** to
  find peers. Devices are **not** advertised on mDNS.
- **Peering.** When a registry sees a peer, the two connect over ZeroMQ.
  - Every attach, detach and identity change (e.g. after a re-probe
    following a flash) is published to the network.
  - Every registry stores remote devices in its own database, tagged with
    the owning host.
- **Joining late.** A new peer receives a snapshot, then the event stream.
  When a peer vanishes, its devices are marked unreachable or removed.
- **Explicit peers (nice to have).** A `--peer HOST[:PORT]` option on
  `mbregistry` and on the client tools joins the network that peer belongs
  to. This lets a network cross subnets that mDNS cannot span.
- **Remote access.** Clients act on a remote device through **that host's**
  registry: lock, open a stream, flash. The remote stream protocol must
  carry control operations (reset/BREAK, DTR), because a plain TCP pipe
  cannot (brief §9.5).

## Open questions

- What is replicated besides attach and detach? Lock and busy state?
  (brief §9.9)
- Security on an open LAN: token or none, to begin with.
