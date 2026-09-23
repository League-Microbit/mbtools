---
id: '003'
title: 'Distribution: registry peering and remote streams'
status: roadmap
branch: sprint/003-distribution-registry-peering-and-remote-streams
use-cases: []
issues:
- mbregistry-peering-mdns-and-zeromq.md
- mbdeploy-flash-by-name-remote.md
- mbserial-raw-serial-access-remote.md
---
<!-- CLASI: Before changing code or making plans, review the SE process in CLAUDE.md -->

# Sprint 003: Distribution: registry peering and remote streams

## Goals

Make `mbregistry` distributed — mDNS peer discovery plus a ZeroMQ
attach/detach/identity-change event network — and extend `mbdeploy` and
`mbserial` to reach a device on a peer host through *that* host's
registry, never directly.

## Problem

The one-daemon principle only pays off across a fleet if a client on any
host can see and act on devices attached to any other host, mediated
entirely by the owning host's own registry (brief §3.7, §3.9). Sprints 001
and 002 built the local-only daemon and its local-only clients; this
sprint is what turns a set of independent per-host registries into one
logical fleet-wide view.

## Solution

- `mbregistry-peering-mdns-and-zeromq.md`: each `mbregistry` advertises
  and browses `_mbregistry._tcp` via mDNS (used only for peer discovery,
  never device-level announcements); peers connect over ZeroMQ; every
  attach/detach/identity-change event is published and applied into every
  peer's own database, tagged with the owning host; a newly-joining peer
  gets a snapshot then the live stream; `--peer HOST[:PORT]` lets a
  registry or client cross a network mDNS can't reach; the remote stream
  protocol carries control operations (reset/BREAK, DTR) out-of-band —
  the exact mechanism (framed protocol, WebSocket control messages, or
  RFC 2217) is this sprint's to decide or explicitly defer.
- `mbdeploy-flash-by-name-remote.md`: `mbdeploy deploy <name>` resolves a
  peer-owned device via the peer event stream, locks and flashes through
  the *owning* host's registry, and relays the post-flash re-probe back.
- `mbserial-raw-serial-access-remote.md`: `mbserial <name>` opens a
  stream to the owning host's registry carrying both data and the
  out-of-band control operations peering's stream protocol defines.

## Success Criteria

- Two `mbregistry` instances on the same LAN discover each other via
  mDNS and converge to the same combined device view within a bounded
  time.
- `mbregistry list` on either host shows the other's devices, tagged by
  owning host.
- A vanished peer's devices are handled per this sprint's cleanup policy
  (marked unreachable or removed — not left as stale "connected" rows).
- `mbdeploy deploy <name>` and `mbserial <name>` both work end-to-end
  against a peer-owned device, with no client opening a port or SWD
  connection to a board that isn't local to the registry it's talking to.
- A remote `mbserial` session can deliver a reset/BREAK to a relay board
  across the network stream.

## Scope

### In Scope

- `mbregistry-peering-mdns-and-zeromq.md`.
- `mbdeploy-flash-by-name-remote.md`.
- `mbserial-raw-serial-access-remote.md`.

### Out of Scope

- `mbrelay` and robot-console compatibility (sprint 004) — those build on
  top of this sprint's peering and remote-stream work but are scoped
  separately.
- Windows platform work (sprint 004).

## Test Strategy

(Deferred to Phase 2 detail planning — this is a roadmap-only entry. Will
need at least two-host or two-process integration coverage for peering,
not just unit tests.)

## Architecture

(Deferred to Phase 2 detail planning — this is a roadmap-only entry.
Expected to size as substantial: new cross-host dependency, a new
event-bus subsystem, and a new remote-stream protocol with a control
channel — all new cross-module and cross-host composition.)

### Architecture Overview

(Deferred to Phase 2 detail planning.)

### Design Rationale

(Deferred to Phase 2 detail planning. Notably: the remote-stream control
channel mechanism (framed protocol vs. WebSocket vs. RFC 2217) is an open
decision the brief leaves unresolved — this sprint's architecture must
either make that call or explicitly flag it for stakeholder input before
ticketing.)

### Migration Concerns

(Deferred to Phase 2 detail planning.)

## Use Cases

(Deferred to Phase 2 detail planning. Relevant use cases from
`docs/design/usecases.md`: UC-005 (list across peers), UC-009 (flash by
name, remote), UC-011 (mbserial connect, remote device), UC-013 (peer
discovery via mDNS), UC-014 (explicit `--peer` across networks).)

## GitHub Issues

(GitHub issues linked to this sprint's tickets. Format: `owner/repo#N`.)

## Definition of Ready

Before tickets can be created, all of the following must be true:

- [ ] Sprint planning document is complete (sprint.md, including its
      Architecture and Use Cases sections)
- [ ] Architecture review passed (or skipped, for changes with no
      architectural impact)
- [ ] Stakeholder has approved the sprint plan

## Tickets

| # | Title | Depends On |
|---|-------|------------|

Tickets execute serially in the order listed.
