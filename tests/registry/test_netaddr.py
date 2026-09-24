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
