"""mbtools.registry.peering — mDNS advertise/browse for peer discovery.

Per sprint.md's Architecture (module ``registry.peering``, Step 5), this
is the only module in mbtools that imports ``zeroconf``. Each
``mbregistry`` advertises ``_mbregistry._tcp.local.`` with a TXT record
naming its remote-API/PUB/snapshot ports (Decision 7's defaults, all
overridable), and simultaneously browses for the same service type; on
discovering a peer (never itself) it records ``host``/``endpoint`` into
``store``'s ``peer`` table via :meth:`~mbtools.registry.store.Store.
record_peer_seen`.

This ticket (004) is discovery only. The ZeroMQ snapshot exchange and
live event bus that actually *use* a discovered peer are ticket 005's
job, kept in a separate module since discovery and replication change for
different reasons (sprint.md Step 1-2) — this module never opens a ZMQ
socket, and nothing here decides what a peer's devices look like, only
that it exists and how to reach it.

**Advertising a single IPv4** (per the programmer brief for this ticket:
two of this project's hardware hosts, loki/magni, each resolve to two
different IPv4 addresses on two different subnets): rather than
advertise every interface address, this module picks one best-effort LAN
IPv4 (the same "connect a UDP socket, read back the local endpoint" trick
``mbdeploy/mdns.py`` uses — ``mbdeploy`` is a separate installable
project, so this is a deliberate, small port rather than a cross-project
import) and advertises only that one, overridable via
``advertise_address`` for a host where the default guess is wrong.

**``host`` naming**: the bare hostname (``"loki"``, not ``"loki.lan"`` or
``"loki.local."``) is what ``store``'s ``peer.host``/``device.host``
columns and ``Store.find``'s ``name@host`` suffix (ticket 001) expect --
this module strips zeroconf's service-type suffix from a discovered
instance name to recover it, and defaults its own advertisement to
:func:`socket.gethostname` with any domain suffix stripped the same way.

**Self-exclusion**: a registry's own ``ServiceBrowser`` typically also
sees its own advertisement (mDNS has no built-in "don't tell me about
myself"). This module never uses the discovered instance *name* to
detect that case (zeroconf's ``allow_name_change=True`` can rename it on
a collision) -- it compares the discovered service's resolved
address/port against the single address/port this instance itself
registered, per this ticket's acceptance criteria.

**Coexistence with ``avahi-daemon``**: confirmed clean on real Nolanet
hardware by ``mbdeploy``'s own spike
(``docs/spikes/002-avahi-coexistence.md`` there) -- no design change
needed here as a result; this module uses plain ``python-zeroconf``
throughout, no ``avahi-publish`` fallback.

**Injectability**: the ``zeroconf`` constructor parameter takes anything
exposing the three names this module calls (``Zeroconf``, ``ServiceInfo``,
``ServiceBrowser``) -- defaults to the real ``zeroconf`` package. A test
passes a small fake module-like object exposing fakes of those three
names, exercising the TXT-parsing and store-recording logic
(:class:`_BrowseListener`, driven directly via its ``add_service``
callback) without opening any real mDNS socket, mirroring every other
module's injectable-dependency convention (e.g.
:func:`mbtools.registry.identity.probe`'s ``serial_factory``).
"""

from __future__ import annotations

import logging
import socket as _socket
from typing import Any

import zeroconf as _real_zeroconf

from mbtools.registry.store import Store

__all__ = [
    "PeerDiscovery",
    "SERVICE_TYPE",
    "DEFAULT_REMOTE_PORT",
    "DEFAULT_PUB_PORT",
    "DEFAULT_SNAPSHOT_PORT",
    "TXT_REMOTE_PORT",
    "TXT_PUB_PORT",
    "TXT_SNAPSHOT_PORT",
]

logger = logging.getLogger(__name__)

#: mDNS service type this whole sprint's peering is scoped to -- never
#: used for device-level announcements (sprint.md's Solution section is
#: explicit about that boundary).
SERVICE_TYPE = "_mbregistry._tcp.local."

# Decision 7 (sprint.md Step 6): fixed default ports, all overridable via
# these constructor arguments -- a future CLI layer (ticket 009) resolves
# flag > env var > these defaults and passes the result in here, the same
# precedence pattern ``registry.cli``'s ``_resolve_path`` already
# establishes for paths.
DEFAULT_REMOTE_PORT = 7440
DEFAULT_PUB_PORT = 7442
DEFAULT_SNAPSHOT_PORT = 7443

#: TXT record keys carrying this host's port trio -- what lets
#: ``--peer HOST`` alone (no ports) still work once mDNS has supplied
#: them (Decision 7).
TXT_REMOTE_PORT = "remote_port"
TXT_PUB_PORT = "pub_port"
TXT_SNAPSHOT_PORT = "snapshot_port"

#: How long a discovered instance's full ``ServiceInfo`` (address/port/TXT)
#: is given to resolve before giving up on that one discovery event.
_DEFAULT_RESOLVE_TIMEOUT_MS = 3000


def _local_ip() -> str:
    """Best-effort LAN IPv4 address (not ``127.0.0.1``) for this host.

    Ported from ``mbdeploy/mdns.py``'s ``_local_ip`` -- the standard
    "connect a UDP socket, read back the local endpoint" trick (no packets
    actually sent). Falls back to resolving this host's own name if that
    fails (e.g. no route to the public internet).
    """
    s = _socket.socket(_socket.AF_INET, _socket.SOCK_DGRAM)
    try:
        s.connect(("8.8.8.8", 80))
        return s.getsockname()[0]
    except OSError:
        return _socket.gethostbyname(_socket.gethostname())
    finally:
        s.close()


def _short_hostname() -> str:
    """This host's bare hostname, with any domain suffix stripped -- see
    the module docstring's "``host`` naming" note.
    """
    return _socket.gethostname().split(".")[0]


def _encode_txt(txt: dict[str, str]) -> dict[bytes, bytes]:
    """UTF-8-encode a plain ``str`` TXT dict into zeroconf's byte form.

    Ported from ``mbdeploy/mdns.py``'s ``_encode_txt``.
    """
    return {
        str(key).encode("utf-8"): str(value if value is not None else "").encode("utf-8")
        for key, value in txt.items()
    }


def _decode_txt(properties: dict[Any, Any] | None) -> dict[str, str]:
    """UTF-8-decode a zeroconf TXT ``properties`` dict back to ``str``.

    Ported from ``mbdeploy/mdns.py``'s ``_decode_txt``. Handles a missing
    dict, and a value of ``None`` (zeroconf's representation of a TXT key
    with no ``=value``) -- both round-trip to an empty string rather than
    raising or losing the key.
    """
    if not properties:
        return {}
    decoded: dict[str, str] = {}
    for key, value in properties.items():
        k = key.decode("utf-8") if isinstance(key, bytes) else str(key)
        if value is None:
            v = ""
        elif isinstance(value, bytes):
            v = value.decode("utf-8")
        else:
            v = str(value)
        decoded[k] = v
    return decoded


def _resolve_address(info: Any) -> str:
    """Best-effort reachable address string for a discovered service --
    the ``host`` half of the ``host:remote_port`` endpoint string this
    module records (a directly connectable IP, not a ``.local.`` name
    that needs a second resolution step).

    Ported from ``mbdeploy/mdns.py``'s ``_resolve_host``. Prefers a
    resolved IP address; falls back to the advertised ``.local.`` server
    hostname if no address resolved.
    """
    parsed_addresses = getattr(info, "parsed_addresses", None)
    if callable(parsed_addresses):
        addresses = parsed_addresses()
        if addresses:
            return addresses[0]
    server = getattr(info, "server", None)
    if server:
        return str(server).rstrip(".")
    return ""


def _peer_host_from_name(name: str, service_type: str) -> str:
    """``"loki._mbregistry._tcp.local."`` -> ``"loki"`` -- strip zeroconf's
    service-type suffix to recover the bare hostname ``store.
    record_peer_seen`` expects as its ``host`` argument.
    """
    suffix = f".{service_type}"
    if name.endswith(suffix):
        return name[: -len(suffix)]
    return name.rstrip(".")


class _BrowseListener:
    """``zeroconf.ServiceBrowser``'s callback target.

    Duck-typed (``add_service``/``update_service``/``remove_service``),
    not a ``zeroconf.ServiceListener`` subclass -- mirrors ``mbdeploy/
    mdns.py``'s own ``_BrowseListener`` so a test can drive it directly
    (calling ``add_service`` with a fake ``zc``/``info``) without
    importing zeroconf's ABC or opening a real socket.

    This is where the actual discovery -> store work happens: resolve
    the full ``ServiceInfo``, exclude this instance's own advertisement,
    parse the TXT record's ``remote_port``, and call
    ``store.record_peer_seen(host, endpoint)``.
    """

    def __init__(
        self,
        *,
        store: Store,
        service_type: str,
        own_address: str,
        own_port: int,
        resolve_timeout_ms: int = _DEFAULT_RESOLVE_TIMEOUT_MS,
    ) -> None:
        self._store = store
        self._service_type = service_type
        self._own_address = own_address
        self._own_port = own_port
        self._resolve_timeout_ms = resolve_timeout_ms

    def add_service(self, zc: Any, type_: str, name: str) -> None:
        info = zc.get_service_info(type_, name, timeout=self._resolve_timeout_ms)
        if info is None:
            logger.warning("peering: %s did not resolve within timeout; skipping", name)
            return
        self._record(name, info)

    def update_service(self, zc: Any, type_: str, name: str) -> None:
        # A TXT/address change for an already-known instance is handled
        # identically to a fresh discovery -- ``record_peer_seen`` is an
        # upsert (store.py), so re-recording is always safe.
        self.add_service(zc, type_, name)

    def remove_service(self, zc: Any, type_: str, name: str) -> None:
        # An mDNS goodbye is not this module's signal for "peer
        # unreachable" (sprint.md Decision 5): that's the losing side of
        # the ZMQ link (ticket 005) observing the drop. Discovery-only
        # here, by this ticket's own scope statement.
        pass

    def _record(self, name: str, info: Any) -> None:
        address = _resolve_address(info)
        port = getattr(info, "port", None)
        if address == self._own_address and port == self._own_port:
            return  # this instance's own advertisement -- never a peer of itself
        txt = _decode_txt(getattr(info, "properties", None))
        remote_port_str = txt.get(TXT_REMOTE_PORT)
        if not address or not remote_port_str:
            logger.warning(
                "peering: discovered %s with no resolvable address/%s TXT value; skipping",
                name,
                TXT_REMOTE_PORT,
            )
            return
        try:
            int(remote_port_str)  # validate, but keep the endpoint string form
        except ValueError:
            logger.warning(
                "peering: discovered %s with non-numeric %s TXT value %r; skipping",
                name,
                TXT_REMOTE_PORT,
                remote_port_str,
            )
            return
        host = _peer_host_from_name(name, self._service_type)
        endpoint = f"{address}:{remote_port_str}"
        self._store.record_peer_seen(host, endpoint)


class PeerDiscovery:
    """mDNS advertise + browse for ``_mbregistry._tcp`` peer discovery.

    ``start()``/``stop()`` lifecycle matches
    :class:`~mbtools.registry.api.RegistryAPIServer`'s own convention:
    ``start()`` is non-blocking (registration and browsing both run on
    zeroconf's own internal engine/threads -- this class does not spawn
    an extra thread of its own to poll it), and ``stop()`` unregisters
    this host's own advertisement, cancels the browser, and closes the
    injected/owned ``Zeroconf`` instance before returning. Both are
    idempotent (a second ``start()``/``stop()`` is a no-op).

    ``store`` is an injected reference (the same instance a caller's
    ``mbregistry run`` assembly, or a test, already constructed), never
    built by this class -- mirrors every other module's "don't construct
    a second one of these" convention.
    """

    def __init__(
        self,
        *,
        store: Store,
        host: str | None = None,
        advertise_address: str | None = None,
        remote_port: int = DEFAULT_REMOTE_PORT,
        pub_port: int = DEFAULT_PUB_PORT,
        snapshot_port: int = DEFAULT_SNAPSHOT_PORT,
        service_type: str = SERVICE_TYPE,
        zeroconf: Any = None,
    ) -> None:
        self._store = store
        self._host = host if host is not None else _short_hostname()
        self._advertise_address = (
            advertise_address if advertise_address is not None else _local_ip()
        )
        self._remote_port = remote_port
        self._pub_port = pub_port
        self._snapshot_port = snapshot_port
        self._service_type = service_type
        self._zc_module = zeroconf if zeroconf is not None else _real_zeroconf

        self._zc: Any = None
        self._own_info: Any = None
        self._browser: Any = None
        self._started = False

    # -- lifecycle -----------------------------------------------------

    def start(self) -> None:
        """Register this host's own advertisement and start browsing.
        Returns immediately -- see the class docstring.
        """
        if self._started:
            return
        self._zc = self._zc_module.Zeroconf()

        txt = {
            TXT_REMOTE_PORT: str(self._remote_port),
            TXT_PUB_PORT: str(self._pub_port),
            TXT_SNAPSHOT_PORT: str(self._snapshot_port),
        }
        self._own_info = self._zc_module.ServiceInfo(
            self._service_type,
            f"{self._host}.{self._service_type}",
            addresses=[_socket.inet_aton(self._advertise_address)],
            port=self._remote_port,
            properties=_encode_txt(txt),
            server=f"{self._host}.local.",
        )
        self._zc.register_service(self._own_info, allow_name_change=True)

        listener = _BrowseListener(
            store=self._store,
            service_type=self._service_type,
            own_address=self._advertise_address,
            own_port=self._remote_port,
        )
        self._browser = self._zc_module.ServiceBrowser(
            self._zc, self._service_type, listener=listener
        )
        self._started = True

    def stop(self) -> None:
        """Cancel the browser, unregister this host's own advertisement,
        and close the ``Zeroconf`` instance. Idempotent.
        """
        if not self._started:
            return
        if self._browser is not None:
            cancel = getattr(self._browser, "cancel", None)
            if callable(cancel):
                cancel()
        if self._zc is not None:
            if self._own_info is not None:
                try:
                    self._zc.unregister_service(self._own_info)
                except Exception:
                    logger.exception("peering: error unregistering own service")
            self._zc.close()
        self._zc = None
        self._own_info = None
        self._browser = None
        self._started = False

    def __enter__(self) -> "PeerDiscovery":
        self.start()
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.stop()
