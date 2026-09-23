---
id: '004'
title: 'registry.peering: mDNS advertise and browse for peer discovery'
status: open
use-cases: [SUC-001]
depends-on: ['001']
github-issue: ''
issue: mbregistry-peering-mdns-and-zeromq.md
completes_issue: true
---
<!-- CLASI: Before changing code or making plans, review the SE process in CLAUDE.md -->

# registry.peering: mDNS advertise and browse for peer discovery

## Description

Per sprint.md's Architecture (module `registry.peering`, Step 5), add
mDNS advertise/browse using `python-zeroconf`: each `mbregistry`
advertises `_mbregistry._tcp.local.` with a TXT record naming its
remote-API/PUB/snapshot ports (Decision 7's defaults, all overridable),
and browses for the same service type. This ticket covers discovery
only — recording a discovered peer's `host`/`endpoint` into `store`'s new
`peer` table (ticket 001) via `record_peer_seen`. The ZeroMQ snapshot
exchange and live event bus that actually *use* a discovered peer are
ticket 005, kept separate since discovery and replication change for
different reasons (sprint.md Step 1-2).

Add `python-zeroconf` to `pyproject.toml`'s `dependencies`.

## Acceptance Criteria

- [ ] New module `mbtools/registry/peering.py` (or a `peering/` package
      if the file grows large — implementer's judgment) with a
      `PeerDiscovery` (or similar) class: `start()`/`stop()` lifecycle
      matching `RegistryAPIServer`'s own convention (background thread(s),
      non-blocking `start()`).
- [ ] On `start()`, registers an mDNS service
      (`zeroconf.ServiceInfo`/`zeroconf.Zeroconf.register_service`) for
      `_mbregistry._tcp.local.` naming this host, with a TXT record
      carrying `remote_port`, `pub_port`, `snapshot_port` (Decision 7's
      default values, each overridable via constructor args mirroring
      every other module's flag/env-var convention).
- [ ] Simultaneously browses for the same service type
      (`zeroconf.ServiceBrowser`); on discovering a peer (excluding this
      host's own advertisement — matched by comparing the discovered
      service's address/port against what this instance itself
      registered), calls `store.record_peer_seen(host, endpoint)`.
- [ ] A discovered peer's TXT-record ports are parsed into the
      `endpoint` string (`host:remote_port`) stored in `store`'s `peer`
      table — this is what ticket 001's `endpoint` field means in
      practice.
- [ ] `zeroconf` is injectable (constructor parameter, mirroring every
      other module's escape-hatch convention — e.g.
      `identity.probe`'s `serial_factory`) so unit tests exercise the
      TXT-parsing and store-recording logic against a fake
      `Zeroconf`/`ServiceInfo` without opening real mDNS sockets. One
      narrow integration test may use a real `Zeroconf` on loopback if
      the implementer judges it valuable, matching sprint.md's Test
      Strategy note about needing real two-process coverage somewhere in
      this sprint — but the bulk of this ticket's tests use the fake.
- [ ] `pyproject.toml`: `python-zeroconf` added to `dependencies`, and
      confirmed (in this ticket's own notes, not necessarily an
      automated test) to install cleanly via `uv sync` on this
      project's target platforms (Debian 13 aarch64 Python 3.13,
      macOS x86_64) — full hardware confirmation is ticket 014's job,
      but a `uv sync`/`uv build` dry run on the dev Mac at minimum should
      happen in this ticket.

## Testing

- **Existing tests to run**: none affected (new module, no existing
  caller yet).
- **New tests to write**: TXT-record construction/parsing round-trip;
  peer-discovered callback records the right `host`/`endpoint` into a
  `tmp_path` `Store`; this host's own advertisement is correctly excluded
  from being recorded as a "discovered peer" of itself.
- **Verification command**: `uv run pytest tests/registry/peering/`
