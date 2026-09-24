---
id: '004'
title: 'registry.peering: mDNS advertise and browse for peer discovery'
status: done
use-cases:
- SUC-001
depends-on:
- '001'
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

- [x] New module `mbtools/registry/peering.py` (or a `peering/` package
      if the file grows large — implementer's judgment) with a
      `PeerDiscovery` (or similar) class: `start()`/`stop()` lifecycle
      matching `RegistryAPIServer`'s own convention (background thread(s),
      non-blocking `start()`).
- [x] On `start()`, registers an mDNS service
      (`zeroconf.ServiceInfo`/`zeroconf.Zeroconf.register_service`) for
      `_mbregistry._tcp.local.` naming this host, with a TXT record
      carrying `remote_port`, `pub_port`, `snapshot_port` (Decision 7's
      default values, each overridable via constructor args mirroring
      every other module's flag/env-var convention).
- [x] Simultaneously browses for the same service type
      (`zeroconf.ServiceBrowser`); on discovering a peer (excluding this
      host's own advertisement — matched by comparing the discovered
      service's address/port against what this instance itself
      registered), calls `store.record_peer_seen(host, endpoint)`.
- [x] A discovered peer's TXT-record ports are parsed into the
      `endpoint` string (`host:remote_port`) stored in `store`'s `peer`
      table — this is what ticket 001's `endpoint` field means in
      practice.
- [x] `zeroconf` is injectable (constructor parameter, mirroring every
      other module's escape-hatch convention — e.g.
      `identity.probe`'s `serial_factory`) so unit tests exercise the
      TXT-parsing and store-recording logic against a fake
      `Zeroconf`/`ServiceInfo` without opening real mDNS sockets. One
      narrow integration test may use a real `Zeroconf` on loopback if
      the implementer judges it valuable, matching sprint.md's Test
      Strategy note about needing real two-process coverage somewhere in
      this sprint — but the bulk of this ticket's tests use the fake.
- [x] `pyproject.toml`: `python-zeroconf` added to `dependencies`, and
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

## Implementation Notes

- **Module**: new `mbtools/registry/peering.py` (single file — stayed
  well short of needing a `peering/` package). `PeerDiscovery` is the
  one public class; `_BrowseListener` and the TXT/address helper
  functions (`_encode_txt`/`_decode_txt`/`_resolve_address`/
  `_peer_host_from_name`/`_local_ip`/`_short_hostname`) are private,
  ported (not imported — `mbdeploy` is a separate installable project)
  from `mbdeploy/mdns.py`'s equivalent helpers, confirmed against that
  module and `docs/spikes/002-avahi-coexistence.md`'s avahi-coexistence
  finding (no design change needed here as a result).
- **Injectable `zeroconf`**: the constructor's `zeroconf` parameter takes
  anything exposing `Zeroconf`/`ServiceInfo`/`ServiceBrowser` (defaults
  to the real `zeroconf` package) rather than a single-instance or
  factory-callable injection — this was the shape that let the fake used
  throughout `tests/registry/peering/test_peering.py`
  (`_FakeZeroconfNamespace`) stand in for the whole `zeroconf` module
  with three small fake classes, none of which open a socket.
- **Self-exclusion** compares the discovered `ServiceInfo`'s resolved
  address *and* port against the single address/port this instance
  itself registered (`_BrowseListener._record`), never the discovered
  instance *name* — `allow_name_change=True` means zeroconf can rename a
  colliding instance (`"loki (2)"`), so a name comparison would be
  unreliable. Confirmed by `test_add_service_does_not_exclude_a_peer_
  sharing_only_the_address` that address-only isn't sufficient either
  (two `PeerDiscovery` instances sharing one loopback address on
  different ports, as the real-zeroconf integration test below does,
  must not false-exclude each other).
- **`host` vs `endpoint`**: `store.record_peer_seen(host, endpoint)` is
  called with `host` = the bare hostname recovered from the discovered
  instance name (stripping the `.{service_type}` suffix — e.g.
  `"loki"`), and `endpoint` = `f"{resolved_address}:{remote_port}"`
  where `resolved_address` is the peer's actual reachable IPv4 (from
  `ServiceInfo.parsed_addresses()`), not a `.local.` name needing a
  second resolution step — this is what makes a discovered peer directly
  connectable per Decision 8 ("a client needs network reachability to
  the peer host directly"). `host` is `store`'s `peer.host` primary key
  and what `name@host` resolution (ticket 001) expects as a bare name;
  `endpoint`'s `host:port` half is deliberately the IP, not that same
  bare name, since a bare mDNS `.local.` name doesn't cross-resolve
  reliably off the originating subnet, and the programmer brief's own
  note (loki/magni each resolving to two different IPv4s on two
  different subnets) makes an IP-based endpoint the only one guaranteed
  reachable by a peer on the other subnet.
- **Advertising a single IPv4**: `advertise_address` (constructor
  parameter, defaults to `_local_ip()`'s best-effort LAN IPv4 via the
  UDP-connect trick ported from `mbdeploy/mdns.py`) is the one address
  put in the registered `ServiceInfo`'s `addresses=[...]` — never every
  local interface address — per the programmer brief's explicit
  instruction to advertise one reachable address rather than letting a
  multi-homed host (loki/magni) advertise an address on a subnet a given
  peer can't reach.
- **`remove_service` is a deliberate no-op**: an mDNS goodbye packet is
  not wired to `mark_peer_unreachable` in this ticket — sprint.md's
  Decision 5 assigns "peer unreachable" to the losing side of the ZMQ
  link dropping (ticket 005), consistent with this ticket's own scope
  statement ("discovery only"). `update_service` does re-run
  `add_service`'s logic (an upsert via `record_peer_seen`, safe to call
  again with the same host/endpoint).
- **`pyproject.toml`**: `zeroconf>=0.140` added to `dependencies` (the
  PyPI package name is `zeroconf`, not `python-zeroconf` — that's the
  project's display/repo name; `import zeroconf` is what both `mbdeploy`
  and this module use). `uv sync` on the dev Mac (macOS, Python 3.13.7)
  installed cleanly: `zeroconf==0.151.3`, `ifaddr==0.2.0`, no compile
  step (matches the avahi-coexistence spike's aarch64/Bookworm wheel
  finding — this is also a prebuilt wheel on macOS). `uv build` was also
  tried per the acceptance criterion's "uv sync/uv build dry run"
  wording, but fails for a reason unrelated to this ticket or to
  `zeroconf`: an existing absolute symlink at
  `.claude/skills/architecture-authoring/SKILL.md` (pointing outside the
  sdist root) trips `uv build`'s "external symlinks are not allowed"
  check when building the source distribution. Confirmed pre-existing
  (the symlink predates this ticket's changes — `git log` shows it
  untouched by this diff) and out of this ticket's scope to fix; `uv
  sync`, the acceptance criterion's actual installability check, is
  unaffected since it doesn't build an sdist. Full hardware confirmation
  (Debian 13 aarch64) remains ticket 014's job, unchanged.
- **Real-zeroconf integration test**: took the "may use a real Zeroconf
  on loopback if valuable" allowance — `test_real_zeroconf_loopback_
  two_registries_discover_each_other` runs two real `PeerDiscovery`
  instances (default all-interfaces `Zeroconf()`, both advertising
  `127.0.0.1` on different port trios) and polls each other's `store`
  for up to 10s, confirming genuine two-process-shaped mDNS
  advertise/browse convergence, not just the fake. Verified manually
  before committing that this passes reliably (multiple runs, no
  flakiness observed) on the dev Mac; if it proves flaky in CI down the
  line, the fix is narrowing to `Zeroconf(interfaces=["127.0.0.1"])` (no
  constructor parameter for that exists yet — would need adding).
- **Tests**: new `tests/registry/peering/test_peering.py`, 28 tests —
  TXT round-trip/edge cases (3), `_peer_host_from_name`/
  `_resolve_address` helpers (5), `_BrowseListener` discovery/exclusion/
  malformed-TXT/no-op-remove cases driven directly against a `tmp_path`
  `Store` (9), `PeerDiscovery.start()`/`stop()` lifecycle including
  idempotency against the fake zeroconf namespace (9), and 2 end-to-end
  tests wiring `PeerDiscovery`'s real browser-constructed listener back
  to the fake, plus the one real-zeroconf loopback test. Basename
  checked unique across `tests/` before creating it.
- **Test run**: `uv run pytest tests/registry/peering/ -q` → 28 passed.
  Full suite `uv run pytest -q` → 386 passed, 2 skipped (the
  platform-gated `SO_PEERCRED`/`LOCAL_PEERPID` tests, unchanged from
  ticket 001) — no regressions.
- **No deviations** from the ticket's acceptance criteria. `host`/
  `advertise_address`/`service_type` were added as additional optional
  constructor parameters beyond the three ports the acceptance criteria
  named explicitly — needed for the module to be usable/testable at all
  (something has to supply this host's own identity and reachable
  address), and kept consistent with "every other module's flag/env-var
  convention" by defaulting to real values (`socket.gethostname()`/
  `_local_ip()`) rather than requiring a caller to always supply them.
