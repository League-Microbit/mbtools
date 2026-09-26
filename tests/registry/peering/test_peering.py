"""Tests for mbtools.registry.peering (sprint 003 ticket 004) -- mDNS
advertise/browse for peer discovery.

Most tests drive ``PeerDiscovery``/``_BrowseListener`` against a fake
``zeroconf``-module-like namespace (``_FakeZeroconfNamespace`` below) so
the TXT-parsing and store-recording logic under test never opens a real
mDNS socket, per this ticket's own acceptance criteria. One narrow
integration test at the bottom uses the real ``zeroconf`` package on
loopback, per sprint.md's Test Strategy note about needing real coverage
somewhere in this sprint -- the bulk of this ticket's tests use the fake.
"""

from __future__ import annotations

import socket
import time

import pytest

from mbtools.registry import peering as peering_mod
from mbtools.registry.peering import (
    DEFAULT_PUB_PORT,
    DEFAULT_REMOTE_PORT,
    DEFAULT_SNAPSHOT_PORT,
    SERVICE_TYPE,
    TXT_PUB_PORT,
    TXT_REMOTE_PORT,
    TXT_SNAPSHOT_PORT,
    PeerDiscovery,
)
from mbtools.registry.store import Store


@pytest.fixture
def store(tmp_path):
    return Store(tmp_path / "devices.db")


# ---------------------------------------------------------------------------
# Fake zeroconf -- a small module-like namespace exposing just the three
# names PeerDiscovery calls (Zeroconf/ServiceInfo/ServiceBrowser), so
# start()/stop() can be exercised with no real socket opened.
# ---------------------------------------------------------------------------


class _FakeServiceInfo:
    def __init__(self, type_, name, *, addresses, port, properties, server):
        self.type_ = type_
        self.name = name
        self.addresses = addresses
        self.port = port
        self.properties = properties
        self.server = server

    def parsed_addresses(self):
        return [socket.inet_ntoa(a) for a in self.addresses]


class _FakeZeroconf:
    def __init__(self):
        self.registered: list = []
        self.unregistered: list = []
        self.closed = False
        #: ticket 010: controls PeerDiscovery._run_self_check's own
        #: get_service_info() call -- None (default) means "resolves
        #: fine" (echoes the just-registered info back), matching a
        #: healthy responder; a test sets this to True to simulate the
        #: braeburn-style "own registration no longer resolves" failure.
        self.self_check_fails = False

    def register_service(self, info, allow_name_change=False):
        self.registered.append(info)

    def unregister_service(self, info):
        self.unregistered.append(info)

    def close(self):
        self.closed = True

    def get_service_info(self, type_, name, timeout=3000):
        if self.self_check_fails:
            return None
        for info in self.registered:
            if info.name == name:
                return info
        return None


class _FakeServiceBrowser:
    def __init__(self, zc, type_, listener=None):
        self.zc = zc
        self.type_ = type_
        self.listener = listener
        self.cancelled = False

    def cancel(self):
        self.cancelled = True


class _FakeZeroconfNamespace:
    """Stands in for the ``zeroconf`` module: ``.Zeroconf()`` always
    returns the same instance (``.instance``) so a test can inspect what
    ``PeerDiscovery.start()``/``.stop()`` did to it, without this class
    itself being a real ``zeroconf.Zeroconf``.
    """

    ServiceInfo = staticmethod(_FakeServiceInfo)
    ServiceBrowser = staticmethod(_FakeServiceBrowser)

    def __init__(self):
        self.instance = _FakeZeroconf()

    def Zeroconf(self):
        return self.instance


def _make_info(*, address="192.168.2.50", port=7440, txt=None, server="magni.local."):
    txt = (
        txt
        if txt is not None
        else {
            TXT_REMOTE_PORT: str(port),
            TXT_PUB_PORT: "7442",
            TXT_SNAPSHOT_PORT: "7443",
        }
    )
    return _FakeServiceInfo(
        SERVICE_TYPE,
        "magni." + SERVICE_TYPE,
        addresses=[socket.inet_aton(address)],
        port=port,
        properties=peering_mod._encode_txt(txt),
        server=server,
    )


class _StaticZc:
    """A minimal ``zc`` stand-in whose ``get_service_info`` always
    resolves to one canned ``info``, for driving ``_BrowseListener``
    callbacks directly."""

    def __init__(self, info):
        self._info = info

    def get_service_info(self, type_, name, timeout=3000):
        return self._info


# ---------------------------------------------------------------------------
# TXT encode/decode round trip
# ---------------------------------------------------------------------------


def test_txt_round_trip():
    original = {"remote_port": "7440", "pub_port": "7442", "snapshot_port": "7443"}
    encoded = peering_mod._encode_txt(original)
    assert encoded == {
        b"remote_port": b"7440",
        b"pub_port": b"7442",
        b"snapshot_port": b"7443",
    }
    assert peering_mod._decode_txt(encoded) == original


def test_txt_decode_handles_missing_and_none_values():
    assert peering_mod._decode_txt(None) == {}
    assert peering_mod._decode_txt({}) == {}
    assert peering_mod._decode_txt({b"role": None}) == {"role": ""}


def test_txt_decode_handles_non_bytes_values():
    # zeroconf can hand back already-decoded str keys/values in some
    # code paths (e.g. a caller-constructed properties dict in a test) --
    # confirm the decoder tolerates that mix, not just bytes/bytes.
    assert peering_mod._decode_txt({"role": "peer", b"n": b"1"}) == {"role": "peer", "n": "1"}


# ---------------------------------------------------------------------------
# _peer_host_from_name / _resolve_address helpers
# ---------------------------------------------------------------------------


def test_peer_host_from_name_strips_service_type_suffix():
    assert peering_mod._peer_host_from_name("loki." + SERVICE_TYPE, SERVICE_TYPE) == "loki"


def test_peer_host_from_name_falls_back_to_stripping_trailing_dot():
    assert (
        peering_mod._peer_host_from_name("loki.other.local.", SERVICE_TYPE)
        == "loki.other.local"
    )


def test_resolve_address_prefers_parsed_addresses():
    info = _make_info(address="192.168.2.50")
    assert peering_mod._resolve_address(info) == "192.168.2.50"


def test_resolve_address_falls_back_to_server_name():
    class _NoAddresses:
        server = "magni.local."

        def parsed_addresses(self):
            return []

    assert peering_mod._resolve_address(_NoAddresses()) == "magni.local"


def test_resolve_address_empty_when_nothing_available():
    class _Nothing:
        server = None

        def parsed_addresses(self):
            return []

    assert peering_mod._resolve_address(_Nothing()) == ""


# ---------------------------------------------------------------------------
# _BrowseListener -- discovery -> store.record_peer_seen
# ---------------------------------------------------------------------------


def _listener(store, *, own_address="192.168.1.149", own_port=7440, own_host=None):
    return peering_mod._BrowseListener(
        store=store,
        service_type=SERVICE_TYPE,
        own_address=own_address,
        own_port=own_port,
        own_host=own_host,
    )


def test_add_service_records_discovered_peer(store):
    listener = _listener(store)
    info = _make_info(address="192.168.2.50", port=7440)

    listener.add_service(_StaticZc(info), SERVICE_TYPE, "magni." + SERVICE_TYPE)

    peer = store.get_peer("magni")
    assert peer is not None
    assert peer.endpoint == "192.168.2.50:7440"
    assert peer.reachable is True


def test_add_service_excludes_own_advertisement(store):
    listener = _listener(store, own_address="192.168.1.149", own_port=7440)
    info = _make_info(address="192.168.1.149", port=7440, server="loki.local.")

    listener.add_service(_StaticZc(info), SERVICE_TYPE, "loki." + SERVICE_TYPE)

    assert store.list_peers() == []


def test_add_service_excludes_own_advertisement_by_hostname_even_when_address_differs(store):
    # Ticket 014's hardware pass (docs/acceptance/003-hardware.md): a
    # multi-homed host (eth0 + wlan0 on the same LAN, the Nolanet nodes'
    # actual configuration) advertises its own service on one address
    # but a browsing zeroconf instance resolving that *same* service back
    # can hand back the *other* interface's address -- so the old
    # address+port-only check silently failed to recognize the host's
    # own advertisement, and the host peered with itself. Here, "hodr"
    # discovers a service named "hodr" (its own name) resolving to an
    # address that does NOT match its own_address -- reproducing exactly
    # that mismatch -- and the hostname check must still exclude it.
    on_ready_calls = []
    listener = _listener(
        store,
        own_address="192.168.2.148",
        own_port=7440,
        own_host="hodr",
    )
    listener._on_peer_ready = on_ready_calls.append
    info = _make_info(address="192.168.1.148", port=7440, server="hodr.local.")

    listener.add_service(_StaticZc(info), SERVICE_TYPE, "hodr." + SERVICE_TYPE)

    assert store.list_peers() == []
    assert on_ready_calls == []


def test_add_service_does_not_exclude_a_peer_sharing_only_the_address(store):
    # A peer on a different port at the same address is not "us" --
    # exclusion requires both address AND port to match (a host could
    # plausibly run more than one registry-like thing on one IP in test
    # setups, e.g. two PeerDiscovery instances on loopback).
    listener = _listener(store, own_address="192.168.1.149", own_port=7440)
    info = _make_info(address="192.168.1.149", port=9999, server="other.local.")

    listener.add_service(_StaticZc(info), SERVICE_TYPE, "other." + SERVICE_TYPE)

    assert store.get_peer("other") is not None


def test_add_service_skips_when_service_info_does_not_resolve(store):
    listener = _listener(store)
    listener.add_service(_StaticZc(None), SERVICE_TYPE, "ghost." + SERVICE_TYPE)
    assert store.list_peers() == []


def test_add_service_skips_when_txt_missing_remote_port(store):
    listener = _listener(store)
    info = _make_info(address="192.168.2.50", port=7440, txt={"pub_port": "7442"})

    listener.add_service(_StaticZc(info), SERVICE_TYPE, "magni." + SERVICE_TYPE)

    assert store.list_peers() == []


def test_add_service_skips_when_remote_port_is_not_numeric(store):
    listener = _listener(store)
    info = _make_info(
        address="192.168.2.50", port=7440, txt={TXT_REMOTE_PORT: "not-a-port"}
    )

    listener.add_service(_StaticZc(info), SERVICE_TYPE, "magni." + SERVICE_TYPE)

    assert store.list_peers() == []


def test_add_service_skips_when_no_address_resolves(store):
    listener = _listener(store)

    class _NoAddressInfo:
        server = None
        properties = peering_mod._encode_txt({TXT_REMOTE_PORT: "7440"})
        port = 7440

        def parsed_addresses(self):
            return []

    listener.add_service(_StaticZc(_NoAddressInfo()), SERVICE_TYPE, "magni." + SERVICE_TYPE)

    assert store.list_peers() == []


def test_update_service_upserts_like_add_service(store):
    listener = _listener(store)
    info = _make_info(address="192.168.2.50", port=7440)
    zc = _StaticZc(info)

    listener.add_service(zc, SERVICE_TYPE, "magni." + SERVICE_TYPE)
    listener.update_service(zc, SERVICE_TYPE, "magni." + SERVICE_TYPE)

    assert len(store.list_peers()) == 1


def test_remove_service_is_a_discovery_only_no_op(store):
    listener = _listener(store)
    info = _make_info(address="192.168.2.50", port=7440)
    zc = _StaticZc(info)

    listener.add_service(zc, SERVICE_TYPE, "magni." + SERVICE_TYPE)
    listener.remove_service(zc, SERVICE_TYPE, "magni." + SERVICE_TYPE)

    # Still present, still reachable -- an mDNS goodbye is not this
    # ticket's "peer unreachable" signal (that's ticket 005's ZMQ-link
    # drop, per the module docstring).
    peer = store.get_peer("magni")
    assert peer is not None
    assert peer.reachable is True


# ---------------------------------------------------------------------------
# PeerDiscovery.start()/stop() against a fake zeroconf module
# ---------------------------------------------------------------------------


def test_start_registers_own_service_with_txt_ports(store):
    # ticket 008-007: pub_port/snapshot_port must never be the real
    # DEFAULT_PUB_PORT/DEFAULT_SNAPSHOT_PORT (7442/7443) -- start() binds
    # real ZMQ sockets at these values even though zeroconf itself is
    # faked, and a real mbregistry daemon (e.g. the dev Mac's LaunchAgent)
    # may already hold those ports. High, test-only ports sidestep the
    # conflict; see this ticket's Implementation Notes.
    ns = _FakeZeroconfNamespace()
    pd = PeerDiscovery(
        store=store,
        host="loki",
        advertise_address="192.168.1.149",
        remote_port=18440,
        pub_port=18442,
        snapshot_port=18443,
        zeroconf=ns,
    )
    pd.start()

    assert len(ns.instance.registered) == 1
    info = ns.instance.registered[0]
    assert info.name == "loki." + SERVICE_TYPE
    assert info.port == 18440
    assert info.parsed_addresses() == ["192.168.1.149"]
    assert peering_mod._decode_txt(info.properties) == {
        "remote_port": "18440",
        "pub_port": "18442",
        "snapshot_port": "18443",
        "addrs": "192.168.1.149",
    }
    pd.stop()


def test_start_uses_default_ports_when_not_overridden(store):
    # ticket 008-007: this test's whole point is confirming the
    # constructor falls back to DEFAULT_REMOTE_PORT/DEFAULT_PUB_PORT/
    # DEFAULT_SNAPSHOT_PORT (7440/7442/7443) when the caller omits them --
    # exactly the real production ports a live mbregistry daemon may
    # already hold. Asserting on the constructor-time attributes directly
    # proves the same fallback without ever calling start() (which would
    # bind real 7442/7443 sockets and collide with that daemon).
    pd = PeerDiscovery(
        store=store, host="loki", advertise_address="192.168.1.149"
    )
    assert pd._remote_port == DEFAULT_REMOTE_PORT
    assert pd._pub_port == DEFAULT_PUB_PORT
    assert pd._snapshot_port == DEFAULT_SNAPSHOT_PORT


def test_start_creates_browser_for_service_type(store):
    ns = _FakeZeroconfNamespace()
    pd = PeerDiscovery(
        store=store,
        host="loki",
        advertise_address="192.168.1.149",
        pub_port=18452,
        snapshot_port=18453,
        zeroconf=ns,
    )
    pd.start()

    assert pd._browser is not None
    assert pd._browser.type_ == SERVICE_TYPE
    assert pd._browser.zc is ns.instance
    pd.stop()


def test_stop_unregisters_and_closes(store):
    ns = _FakeZeroconfNamespace()
    pd = PeerDiscovery(
        store=store,
        host="loki",
        advertise_address="192.168.1.149",
        pub_port=18462,
        snapshot_port=18463,
        zeroconf=ns,
    )
    pd.start()
    browser = pd._browser
    info = ns.instance.registered[0]

    pd.stop()

    assert browser.cancelled is True
    assert info in ns.instance.unregistered
    assert ns.instance.closed is True


def test_stop_before_start_is_a_no_op(store):
    ns = _FakeZeroconfNamespace()
    pd = PeerDiscovery(
        store=store, host="loki", advertise_address="192.168.1.149", zeroconf=ns
    )
    pd.stop()  # must not raise
    assert ns.instance.registered == []


def test_start_is_idempotent(store):
    ns = _FakeZeroconfNamespace()
    pd = PeerDiscovery(
        store=store,
        host="loki",
        advertise_address="192.168.1.149",
        pub_port=18472,
        snapshot_port=18473,
        zeroconf=ns,
    )
    pd.start()
    pd.start()
    assert len(ns.instance.registered) == 1
    pd.stop()


def test_stop_is_idempotent(store):
    ns = _FakeZeroconfNamespace()
    pd = PeerDiscovery(
        store=store,
        host="loki",
        advertise_address="192.168.1.149",
        pub_port=18482,
        snapshot_port=18483,
        zeroconf=ns,
    )
    pd.start()
    pd.stop()
    pd.stop()  # must not raise, must not double-close/double-unregister
    assert ns.instance.unregistered.count(ns.instance.registered[0]) == 1


def test_self_check_ok_when_own_registration_resolves(store, caplog):
    """ticket 010: the common/healthy case -- _run_self_check re-resolves
    this instance's own just-registered record through its own fake
    Zeroconf (which echoes it back by default -- see _FakeZeroconf.
    get_service_info) and logs no WARNING.
    """
    ns = _FakeZeroconfNamespace()
    pd = PeerDiscovery(
        store=store,
        host="loki",
        advertise_address="192.168.1.149",
        pub_port=18492,
        snapshot_port=18493,
        zeroconf=ns,
    )
    pd.start()
    caplog.set_level("WARNING", logger="mbtools.registry.peering")

    pd._run_self_check()

    assert "self-check failed" not in caplog.text
    pd.stop()


def test_self_check_warns_when_own_registration_stops_resolving(store, caplog):
    """ticket 010: the braeburn failure mode this diagnostic exists for
    -- this host's own mDNS responder can no longer resolve its own
    registration (docs/acceptance/004-hardware.md's finding: a
    long-running daemon on that host stopped answering
    ``_mbregistry._tcp`` queries at all after some hours of uptime).
    ``_run_self_check`` must log a WARNING naming the service, not raise
    and not stay silent.
    """
    ns = _FakeZeroconfNamespace()
    pd = PeerDiscovery(
        store=store,
        host="loki",
        advertise_address="192.168.1.149",
        pub_port=18502,
        snapshot_port=18503,
        zeroconf=ns,
    )
    pd.start()
    ns.instance.self_check_fails = True
    caplog.set_level("WARNING", logger="mbtools.registry.peering")

    pd._run_self_check()

    assert "self-check failed" in caplog.text
    assert "loki." + SERVICE_TYPE in caplog.text
    pd.stop()


def test_self_check_thread_is_started_and_stopped_with_peer_discovery(store):
    """The self-check thread (default 60s interval, never fires during
    this test) must still be a real, joinable thread that start()/stop()
    own -- per this ticket's own "any thread started must be stoppable
    and joined" requirement, same as every other background thread this
    module owns.
    """
    ns = _FakeZeroconfNamespace()
    pd = PeerDiscovery(
        store=store,
        host="loki",
        advertise_address="192.168.1.149",
        pub_port=18512,
        snapshot_port=18513,
        zeroconf=ns,
    )
    assert pd._self_check_thread is None
    pd.start()
    assert pd._self_check_thread is not None
    assert pd._self_check_thread.is_alive()

    pd.stop()

    assert pd._self_check_thread is None


def test_default_host_and_advertise_address_are_not_empty(store):
    """When omitted, host/advertise_address default to real values (this
    host's own hostname / best-effort LAN IP) -- confirms the escape
    hatches are optional, not required, for production use."""
    ns = _FakeZeroconfNamespace()
    pd = PeerDiscovery(
        store=store, zeroconf=ns, pub_port=18522, snapshot_port=18523
    )
    pd.start()

    info = ns.instance.registered[0]
    assert info.name
    assert info.parsed_addresses()[0]
    pd.stop()


def test_browser_discovery_records_peer_end_to_end(store):
    """The browser's listener, driven through PeerDiscovery.start(), is
    wired to the real store -- simulates a discovery event exactly the
    way zeroconf.ServiceBrowser would invoke it."""
    ns = _FakeZeroconfNamespace()
    pd = PeerDiscovery(
        store=store,
        host="loki",
        advertise_address="192.168.1.149",
        remote_port=18530,
        pub_port=18532,
        snapshot_port=18533,
        zeroconf=ns,
    )
    pd.start()
    listener = pd._browser.listener

    peer_info = _make_info(address="192.168.2.50", port=7440, server="magni.local.")
    listener.add_service(_StaticZc(peer_info), SERVICE_TYPE, "magni." + SERVICE_TYPE)

    peer = store.get_peer("magni")
    assert peer is not None
    assert peer.endpoint == "192.168.2.50:7440"
    pd.stop()


def test_browser_discovery_excludes_self_end_to_end(store):
    ns = _FakeZeroconfNamespace()
    pd = PeerDiscovery(
        store=store,
        host="loki",
        advertise_address="192.168.1.149",
        remote_port=18540,
        pub_port=18542,
        snapshot_port=18543,
        zeroconf=ns,
    )
    pd.start()
    listener = pd._browser.listener
    own_info = ns.instance.registered[0]

    listener.add_service(_StaticZc(own_info), SERVICE_TYPE, "loki." + SERVICE_TYPE)

    assert store.list_peers() == []
    pd.stop()


# ---------------------------------------------------------------------------
# Real zeroconf on loopback -- one narrow integration test (this ticket's
# own allowance), proving PeerDiscovery works against the real library,
# not just the fake, per sprint.md's Test Strategy note about needing
# real coverage somewhere in this sprint.
# ---------------------------------------------------------------------------


def test_real_zeroconf_loopback_two_registries_discover_each_other(tmp_path):
    # ticket 008-007: this is the one test in the package that uses the
    # real ``zeroconf`` package rather than the fake namespace above, so
    # it necessarily performs real mDNS multicast on the LAN, not just
    # loopback-local traffic (mDNS/UDP 5353 is not loopback-scoped). Left
    # on ``SERVICE_TYPE`` (the real ``_mbregistry._tcp.local.``), a real
    # mbregistry daemon on the same LAN discovers these ephemeral
    # "peer-a"/"peer-b" test hosts and logs them as if they were real
    # peers -- confirmed via the dev Mac's LaunchAgent log. A dedicated,
    # non-production service type keeps this test's real-zeroconf
    # coverage while a production daemon (which only browses
    # ``SERVICE_TYPE``) never sees or logs it.
    _TEST_SERVICE_TYPE = "_mbregistry-test._tcp.local."

    store_a = Store(tmp_path / "a.db")
    store_b = Store(tmp_path / "b.db")

    pd_a = PeerDiscovery(
        store=store_a,
        host="peer-a",
        advertise_address="127.0.0.1",
        remote_port=19440,
        pub_port=19442,
        snapshot_port=19443,
        service_type=_TEST_SERVICE_TYPE,
    )
    pd_b = PeerDiscovery(
        store=store_b,
        host="peer-b",
        advertise_address="127.0.0.1",
        remote_port=29440,
        pub_port=29442,
        snapshot_port=29443,
        service_type=_TEST_SERVICE_TYPE,
    )
    pd_a.start()
    pd_b.start()
    try:
        deadline = time.time() + 10.0
        peer_of_a = None
        peer_of_b = None
        while time.time() < deadline and (peer_of_a is None or peer_of_b is None):
            peer_of_a = store_a.get_peer("peer-b")
            peer_of_b = store_b.get_peer("peer-a")
            if peer_of_a is None or peer_of_b is None:
                time.sleep(0.2)

        assert peer_of_a is not None, "host A never discovered host B via real mDNS"
        assert peer_of_a.endpoint == "127.0.0.1:29440"
        assert peer_of_b is not None, "host B never discovered host A via real mDNS"
        assert peer_of_b.endpoint == "127.0.0.1:19440"
    finally:
        pd_a.stop()
        pd_b.stop()


# ---------------------------------------------------------------------------
# Multi-homed advertising: every LAN address, in the addrs TXT order
# ---------------------------------------------------------------------------


def test_add_service_connects_to_the_advertised_address_on_our_subnet(store, monkeypatch):
    # meili advertises eth0 (192.168.1.150) and wlan0 (10.9.0.150); this
    # host is only on 192.168.1.0/24, so it must pick eth0 regardless of
    # which address zeroconf resolved first.
    monkeypatch.setattr(
        peering_mod,
        "pick_reachable",
        lambda cands: next((c for c in cands if c.startswith("192.168.1.")), None),
    )
    ready = []
    listener = _listener(store)
    listener._on_peer_ready = lambda *args: ready.append(args)
    info = _make_info(
        address="10.9.0.150",
        txt={
            TXT_REMOTE_PORT: "7440",
            TXT_PUB_PORT: "7442",
            TXT_SNAPSHOT_PORT: "7443",
            peering_mod.TXT_ADDRS: "10.9.0.150,192.168.1.150",
        },
    )

    listener.add_service(_StaticZc(info), SERVICE_TYPE, "meili." + SERVICE_TYPE)

    assert store.get_peer("meili").endpoint == "192.168.1.150:7440"
    assert ready == [("meili", "192.168.1.150", 7442, 7443)]


def test_add_service_excludes_own_advertisement_on_any_own_address(store):
    listener = peering_mod._BrowseListener(
        store=store,
        service_type=SERVICE_TYPE,
        own_address="192.168.1.149",
        own_addresses=("192.168.1.149", "192.168.2.149"),
        own_port=7440,
    )
    info = _make_info(address="192.168.2.149", port=7440, server="loki.local.")

    listener.add_service(_StaticZc(info), SERVICE_TYPE, "loki2." + SERVICE_TYPE)

    assert store.list_peers() == []
