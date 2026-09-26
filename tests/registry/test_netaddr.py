"""Tests for mbtools.registry.netaddr.local_ip -- the address advertised to
peers must never be loopback just because the host has no default route
(hodr: no gateway, and /etc/hosts maps the hostname to 127.0.1.1)."""

from __future__ import annotations

import sys
import types

import pytest

from mbtools.registry import netaddr


def _adapter(name: str, *ips: str):
    return types.SimpleNamespace(
        name=name, nice_name=name, ips=[types.SimpleNamespace(ip=ip) for ip in ips]
    )


@pytest.fixture
def fake_ifaddr(monkeypatch):
    def _install(*adapters):
        mod = types.SimpleNamespace(get_adapters=lambda: list(adapters))
        monkeypatch.setitem(sys.modules, "ifaddr", mod)

    return _install


def test_uses_default_route_address_when_there_is_one(monkeypatch, fake_ifaddr):
    monkeypatch.setattr(netaddr, "_via_default_route", lambda: "192.168.1.240")
    fake_ifaddr(_adapter("eth0", "10.0.0.5"))
    assert netaddr.local_ip() == "192.168.1.240"


def test_no_default_route_picks_real_interface_not_loopback_or_docker(monkeypatch, fake_ifaddr):
    monkeypatch.setattr(netaddr, "_via_default_route", lambda: None)
    monkeypatch.setattr(netaddr.socket, "gethostbyname", lambda _name: "127.0.1.1")
    fake_ifaddr(
        _adapter("lo", "127.0.0.1"),
        _adapter("docker_gwbridge", "172.18.0.1"),
        _adapter("docker0", "172.17.0.1"),
        _adapter("eth0", "192.168.1.148", ("fe80::1", 0, 2)),
    )
    assert netaddr.local_ip() == "192.168.1.148"


def test_skips_link_local(monkeypatch, fake_ifaddr):
    monkeypatch.setattr(netaddr, "_via_default_route", lambda: None)
    fake_ifaddr(_adapter("eth0", "169.254.3.4"), _adapter("wlan0", "192.168.2.149"))
    assert netaddr.local_ip() == "192.168.2.149"


def test_last_resort_is_hostname_lookup(monkeypatch, fake_ifaddr):
    monkeypatch.setattr(netaddr, "_via_default_route", lambda: None)
    monkeypatch.setattr(netaddr.socket, "gethostbyname", lambda _name: "127.0.1.1")
    fake_ifaddr(_adapter("lo", "127.0.0.1"))
    assert netaddr.local_ip() == "127.0.1.1"


# ---------------------------------------------------------------------------
# local_ipv4s / pick_reachable -- multi-homed hosts (eth0 + wlan0)
# ---------------------------------------------------------------------------


def _adapter_p(name: str, *ips_with_prefix: tuple[str, int]):
    return types.SimpleNamespace(
        name=name,
        nice_name=name,
        ips=[types.SimpleNamespace(ip=ip, network_prefix=p) for ip, p in ips_with_prefix],
    )


def test_local_ipv4s_orders_wired_before_wifi_even_when_wifi_is_default_route(
    monkeypatch, fake_ifaddr
):
    # A Nolanet Pi: wlan0's route wins the default-route pick, but eth0
    # must still be advertised first.
    monkeypatch.setattr(netaddr, "_via_default_route", lambda: "192.168.2.150")
    fake_ifaddr(
        _adapter_p("lo", ("127.0.0.1", 8)),
        _adapter_p("wlan0", ("192.168.2.150", 21)),
        _adapter_p("eth0", ("192.168.1.150", 21)),
    )
    assert netaddr.local_ipv4s() == ["192.168.1.150", "192.168.2.150"]


def test_local_ipv4s_skips_vpn_and_cgnat(monkeypatch, fake_ifaddr):
    monkeypatch.setattr(netaddr, "_via_default_route", lambda: "192.168.1.240")
    fake_ifaddr(
        _adapter_p("en0", ("192.168.1.240", 21)),
        _adapter_p("utun0", ("100.64.0.3", 32)),
        _adapter_p("tailscale0", ("100.101.1.2", 32)),
        _adapter_p("en5", ("100.64.9.9", 10)),
    )
    assert netaddr.local_ipv4s() == ["192.168.1.240"]


def test_local_ipv4s_puts_default_route_first_among_wired(monkeypatch, fake_ifaddr):
    monkeypatch.setattr(netaddr, "_via_default_route", lambda: "10.0.0.7")
    fake_ifaddr(
        _adapter_p("en0", ("192.168.1.240", 24)),
        _adapter_p("en1", ("10.0.0.7", 24)),
    )
    assert netaddr.local_ipv4s() == ["10.0.0.7", "192.168.1.240"]


def test_pick_reachable_prefers_an_address_on_a_local_subnet(fake_ifaddr):
    fake_ifaddr(_adapter_p("en0", ("192.168.1.240", 24)))
    assert netaddr.pick_reachable(["192.168.2.150", "192.168.1.150"]) == "192.168.1.150"


def test_pick_reachable_keeps_advertiser_order_when_several_are_local(fake_ifaddr):
    fake_ifaddr(_adapter_p("en0", ("192.168.1.240", 21)))
    assert netaddr.pick_reachable(["192.168.1.150", "192.168.2.150"]) == "192.168.1.150"


def test_pick_reachable_falls_back_to_first_candidate(fake_ifaddr):
    fake_ifaddr(_adapter_p("en0", ("10.0.0.2", 24)))
    assert netaddr.pick_reachable(["192.168.2.150", "192.168.1.150"]) == "192.168.2.150"
    assert netaddr.pick_reachable([]) is None
