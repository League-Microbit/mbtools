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

    def register_service(self, info, allow_name_change=False):
        self.registered.append(info)

    def unregister_service(self, info):
        self.unregistered.append(info)

    def close(self):
        self.closed = True


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
    ns = _FakeZeroconfNamespace()
    pd = PeerDiscovery(
        store=store,
        host="loki",
        advertise_address="192.168.1.149",
        remote_port=7440,
        pub_port=7442,
        snapshot_port=7443,
        zeroconf=ns,
    )
    pd.start()

    assert len(ns.instance.registered) == 1
    info = ns.instance.registered[0]
    assert info.name == "loki." + SERVICE_TYPE
    assert info.port == 7440
    assert info.parsed_addresses() == ["192.168.1.149"]
    assert peering_mod._decode_txt(info.properties) == {
        "remote_port": "7440",
        "pub_port": "7442",
        "snapshot_port": "7443",
    }
    pd.stop()


def test_start_uses_default_ports_when_not_overridden(store):
    ns = _FakeZeroconfNamespace()
    pd = PeerDiscovery(
        store=store, host="loki", advertise_address="192.168.1.149", zeroconf=ns
    )
    pd.start()

    info = ns.instance.registered[0]
    assert peering_mod._decode_txt(info.properties) == {
        "remote_port": str(DEFAULT_REMOTE_PORT),
        "pub_port": str(DEFAULT_PUB_PORT),
        "snapshot_port": str(DEFAULT_SNAPSHOT_PORT),
    }
    pd.stop()


def test_start_creates_browser_for_service_type(store):
    ns = _FakeZeroconfNamespace()
    pd = PeerDiscovery(
        store=store, host="loki", advertise_address="192.168.1.149", zeroconf=ns
    )
    pd.start()

    assert pd._browser is not None
    assert pd._browser.type_ == SERVICE_TYPE
    assert pd._browser.zc is ns.instance
    pd.stop()


def test_stop_unregisters_and_closes(store):
    ns = _FakeZeroconfNamespace()
    pd = PeerDiscovery(
        store=store, host="loki", advertise_address="192.168.1.149", zeroconf=ns
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
        store=store, host="loki", advertise_address="192.168.1.149", zeroconf=ns
    )
    pd.start()
    pd.start()
    assert len(ns.instance.registered) == 1
    pd.stop()


def test_stop_is_idempotent(store):
    ns = _FakeZeroconfNamespace()
    pd = PeerDiscovery(
        store=store, host="loki", advertise_address="192.168.1.149", zeroconf=ns
    )
    pd.start()
    pd.stop()
    pd.stop()  # must not raise, must not double-close/double-unregister
    assert ns.instance.unregistered.count(ns.instance.registered[0]) == 1


def test_default_host_and_advertise_address_are_not_empty(store):
    """When omitted, host/advertise_address default to real values (this
    host's own hostname / best-effort LAN IP) -- confirms the escape
    hatches are optional, not required, for production use."""
    ns = _FakeZeroconfNamespace()
    pd = PeerDiscovery(store=store, zeroconf=ns)
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
        remote_port=7440,
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
        remote_port=7440,
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
    store_a = Store(tmp_path / "a.db")
    store_b = Store(tmp_path / "b.db")

    pd_a = PeerDiscovery(
        store=store_a,
        host="peer-a",
        advertise_address="127.0.0.1",
        remote_port=17440,
        pub_port=17442,
        snapshot_port=17443,
    )
    pd_b = PeerDiscovery(
        store=store_b,
        host="peer-b",
        advertise_address="127.0.0.1",
        remote_port=27440,
        pub_port=27442,
        snapshot_port=27443,
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
        assert peer_of_a.endpoint == "127.0.0.1:27440"
        assert peer_of_b is not None, "host B never discovered host A via real mDNS"
        assert peer_of_b.endpoint == "127.0.0.1:17440"
    finally:
        pd_a.stop()
        pd_b.stop()
