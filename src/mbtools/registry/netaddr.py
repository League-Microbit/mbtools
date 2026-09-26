"""mbtools.registry.netaddr — pick the LAN IPv4 address this host advertises
to peers (``_mbregistry._tcp``) and to robot-console (``_mbrelay._tcp``).

The first choice is the address the kernel would use to reach the
internet (connect a UDP socket, read back the local end; no packet is
sent). A host with no default route cannot do that, and resolving its own
hostname is no fallback: Debian maps the hostname to ``127.0.1.1`` in
``/etc/hosts``, which peers cannot reach. So without a default route we
take the first real interface address instead, skipping loopback,
link-local and container bridges.

A multi-homed host (a Nolanet Pi has ``eth0`` and ``wlan0`` on the same
LAN) advertises *every* real LAN address -- :func:`local_ipv4s`, wired
first -- rather than only the default-route one, and a browsing peer uses
:func:`pick_reachable` to connect to whichever advertised address is on
one of its own directly-attached subnets.
"""

from __future__ import annotations

import ipaddress
import socket

__all__ = ["local_ip", "local_ipv4s", "pick_reachable"]

#: Interface-name prefixes that are never the LAN: container and VM bridges.
_VIRTUAL_PREFIXES = (
    "docker", "br-", "veth", "virbr", "vmnet", "bridge", "utun", "tun", "tap",
    "tailscale", "wg", "zt",
)

#: Interface-name prefixes for Wi-Fi (Linux ``wlan0``/``wlp2s0``) -- ordered
#: after wired interfaces in :func:`local_ipv4s`.
_WIRELESS_PREFIXES = ("wl",)

#: Carrier-grade NAT space -- Tailscale and similar overlay VPNs, never the LAN.
_CGNAT = ipaddress.IPv4Network("100.64.0.0/10")


def _via_default_route() -> str | None:
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("8.8.8.8", 80))
        return s.getsockname()[0]
    except OSError:
        return None
    finally:
        s.close()


def _usable(ip: str) -> bool:
    addr = ipaddress.IPv4Address(ip)
    return not (
        addr.is_loopback or addr.is_link_local or addr.is_unspecified or addr in _CGNAT
    )


def _via_interfaces() -> str | None:
    try:
        import ifaddr
    except ImportError:
        return None
    for adapter in ifaddr.get_adapters():
        name = adapter.nice_name or adapter.name
        if name.startswith(_VIRTUAL_PREFIXES):
            continue
        for ip in adapter.ips:
            if isinstance(ip.ip, str) and _usable(ip.ip):
                return ip.ip
    return None


def local_ip() -> str:
    """Best-effort LAN IPv4 address for this host, never a loopback one
    unless the host has no other address at all."""
    ip = _via_default_route()
    if ip and _usable(ip):
        return ip
    ip = _via_interfaces()
    if ip:
        return ip
    return socket.gethostbyname(socket.gethostname())


def _lan_interfaces() -> list[tuple[str, ipaddress.IPv4Interface]]:
    """``(interface name, address/prefix)`` for every real LAN IPv4
    address on this host, in the OS's interface order."""
    try:
        import ifaddr
    except ImportError:
        return []
    found = []
    for adapter in ifaddr.get_adapters():
        name = adapter.nice_name or adapter.name
        if name.startswith(_VIRTUAL_PREFIXES):
            continue
        for ip in adapter.ips:
            if isinstance(ip.ip, str) and _usable(ip.ip):
                found.append((name, ipaddress.IPv4Interface(f"{ip.ip}/{ip.network_prefix}")))
    return found


def local_ipv4s() -> list[str]:
    """Every real LAN IPv4 address on this host, most-preferred first:
    wired before Wi-Fi, and the default-route address before other
    addresses of the same kind. Falls back to ``[local_ip()]`` when no
    interface can be enumerated."""
    default = _via_default_route()
    interfaces = _lan_interfaces()
    ranked = sorted(
        range(len(interfaces)),
        key=lambda i: (
            interfaces[i][0].startswith(_WIRELESS_PREFIXES),
            str(interfaces[i][1].ip) != default,
            i,
        ),
    )
    addresses: list[str] = []
    for i in ranked:
        ip = str(interfaces[i][1].ip)
        if ip not in addresses:
            addresses.append(ip)
    return addresses or [local_ip()]


def pick_reachable(candidates: list[str]) -> str | None:
    """The first of ``candidates`` (a peer's advertised addresses, in its
    own preference order) that is on one of this host's directly-attached
    subnets; else the first candidate; ``None`` when there are none."""
    networks = [iface.network for _, iface in _lan_interfaces()]
    for candidate in candidates:
        try:
            addr = ipaddress.IPv4Address(candidate)
        except ValueError:
            continue
        if any(addr in network for network in networks):
            return candidate
    return candidates[0] if candidates else None
