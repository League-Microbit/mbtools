"""mbtools.registry.netaddr — pick the LAN IPv4 address this host advertises
to peers (``_mbregistry._tcp``) and to robot-console (``_mbrelay._tcp``).

The first choice is the address the kernel would use to reach the
internet (connect a UDP socket, read back the local end; no packet is
sent). A host with no default route cannot do that, and resolving its own
hostname is no fallback: Debian maps the hostname to ``127.0.1.1`` in
``/etc/hosts``, which peers cannot reach. So without a default route we
take the first real interface address instead, skipping loopback,
link-local and container bridges.
"""

from __future__ import annotations

import ipaddress
import socket

__all__ = ["local_ip"]

#: Interface-name prefixes that are never the LAN: container and VM bridges.
_VIRTUAL_PREFIXES = ("docker", "br-", "veth", "virbr", "vmnet", "bridge", "utun", "tun", "tap")


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
    return not (addr.is_loopback or addr.is_link_local or addr.is_unspecified)


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
