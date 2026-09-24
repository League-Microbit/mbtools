"""mbtools.registry.peering — mDNS advertise/browse for peer discovery.

Per sprint.md's Architecture (module ``registry.peering``, Step 5), this
is the only module in mbtools that imports ``zeroconf``. Each
``mbregistry`` advertises ``_mbregistry._tcp.local.`` with a TXT record
naming its remote-API/PUB/snapshot ports (Decision 7's defaults, all
overridable), and simultaneously browses for the same service type; on
discovering a peer (never itself) it records ``host``/``endpoint`` into
``store``'s ``peer`` table via :meth:`~mbtools.registry.store.Store.
record_peer_seen`.

Ticket 004 was discovery only. Ticket 005 (this revision) adds the
ZeroMQ half in the same module (Step 1-2 groups "peer discovery" and
"event replication" as one module, ``registry.peering``, "since neither
is independently useful — discovery without replication finds a peer and
does nothing with it"): one PUB socket every registry binds (publishing
its own local attach/detach/identity-change events, via
:class:`~mbtools.registry.daemon.Daemon`'s ``event_callback`` hook, plus
lock-acquire/lock-release *display* events via
:class:`~mbtools.registry.locks.LockManager`'s ``lock_display_callback``
hook), a REQ/REP snapshot endpoint a newly-discovered peer queries once
before subscribing, and the code that applies an incoming peer's
snapshot/events into ``store`` via ticket 001's
``upsert_remote_attached``/``mark_remote_detached``/``apply_remote_probe``/
``apply_remote_lock_state``. Also implements the peer-vanish policy
(Decision 5): a detected link drop calls
``store.mark_peer_unreachable(host)`` — never a device-row mutation — and
a later reconnect re-runs the snapshot exchange and calls
``store.mark_peer_reachable(host)``.

**Subscribe-before-snapshot ordering**: :class:`_PeerLink` connects and
subscribes its SUB socket *before* sending the REQ snapshot request —
the classic ZeroMQ "Clone pattern" ordering — so an event the peer
publishes in the gap between subscribing and the snapshot reply arriving
is queued by the SUB socket's own buffer rather than lost, instead of
being silently missed by connecting SUB only after the snapshot already
arrived.

**Peer-vanish detection**: each peer's SUB socket carries a ZeroMQ
monitor socket (``get_monitor_socket()``) watched on its own thread for
``zmq.EVENT_DISCONNECTED`` — a real socket-level event, not a guessed
timeout, per this ticket's acceptance criteria. Heartbeat options
(``ZMQ_HEARTBEAT_IVL``/``TIMEOUT``/``TTL``, best-effort — not fatal if
unsupported) are also set on the SUB socket so a hard network drop (not
just a peer's graceful socket close) still surfaces as
``EVENT_DISCONNECTED`` within a bounded time rather than only detecting
a clean shutdown.

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

**Peering handshake auth (ticket 009, sprint.md Decision 6)**: when
``auth_token`` is set, every snapshot REQ/REP exchange — the one true
"handshake" this module has, per Decision 6's own wording — carries it:
:class:`_PeerLink` sends ``{"token": "<value>"}`` instead of the bare
``b"snapshot"`` ticket 005 always sent, and :meth:`PeerDiscovery._rep_loop`
replies ``{"error": "unauthorized"}`` (never the snapshot payload) for a
request whose ``token`` field is missing or doesn't match. A rejected
snapshot is treated exactly like a timed-out one — logged, no
``on_reachable`` callback fires, no peer row is left half-updated — so a
misconfigured/mismatched-token peer degrades the same silent-no-op way an
old, non-peering daemon already does per sprint.md's Migration Concerns.
When ``auth_token`` is unset (the default, matching every other trust
boundary in this sprint), the REP handler never inspects the request body
at all — reproducing ticket 005's exact wire behavior, which is why every
ticket-004/005 test (raw ``req.send(b"snapshot")``, no token) still
passes unchanged. The live PUB/SUB event stream itself is not
authenticated — it is one-way and has no request to attach a token to;
only the snapshot handshake and, separately, ``registry.remote_api``'s
own per-connection first-line handshake (``docs/design/registry-api.md``'s
"Auth" section) carry ``auth_token``, per Decision 6's "forwarded to both
``remote_api`` and ``peering``'s handshake."

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
:func:`mbtools.registry.identity.probe`'s ``serial_factory``). The new
``zmq`` constructor parameter (ticket 005) mirrors the same convention
and defaults to the real ``pyzmq`` package, but every test in this
ticket's own test module exercises it against real loopback sockets
(ephemeral, ``tcp://127.0.0.1:0``) rather than a fake -- per this
ticket's own testing guidance, "at least one two-process integration
test" is required for peering, and unlike zeroconf's discovery-only
concern, the ZeroMQ half's whole job (snapshot convergence, live event
application, vanish detection) is only meaningfully proven against real
sockets.
"""

from __future__ import annotations

import json
import logging
import socket as _socket
import threading
from typing import Any, Callable

import zeroconf as _real_zeroconf
import zmq as _real_zmq
from zmq.utils.monitor import recv_monitor_message

from mbtools.registry.identity import ProbeResult
from mbtools.registry.store import (
    STATE_CONNECTED,
    STATE_CONNECTED_NO_FIRMWARE,
    DeviceRecord,
    Store,
)

__all__ = [
    "PeerDiscovery",
    "SERVICE_TYPE",
    "DEFAULT_REMOTE_PORT",
    "DEFAULT_PUB_PORT",
    "DEFAULT_SNAPSHOT_PORT",
    "TXT_REMOTE_PORT",
    "TXT_PUB_PORT",
    "TXT_SNAPSHOT_PORT",
    "EVENT_ATTACH",
    "EVENT_DETACH",
    "EVENT_IDENTITY",
    "EVENT_LOCK_STATE",
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

# Event-bus message ``"type"`` values (Step 5: "Publishes this host's own
# attach/detach/identity-change events ... and lock-acquire/lock-release
# display events"). ``EVENT_LOCK_STATE`` is deliberately distinct from
# the other three per this ticket's acceptance criteria.
EVENT_ATTACH = "attach"
EVENT_DETACH = "detach"
EVENT_IDENTITY = "identity"
EVENT_LOCK_STATE = "lock_state"

#: How long a snapshot REQ waits for its peer's REP reply before giving up.
_DEFAULT_SNAPSHOT_TIMEOUT_MS = 5000

#: Poll/receive timeout (ms) used throughout this module's background
#: loops (REP handler, SUB receiver, monitor watcher) -- bounds how
#: quickly each thread notices its own stop event without busy-waiting.
_LOOP_POLL_TIMEOUT_MS = 200

# Best-effort ZMQ heartbeat options (see the module docstring's
# "Peer-vanish detection" note) -- (attribute name on the ``zmq`` module,
# value in milliseconds).
_HEARTBEAT_OPTS_MS = (
    ("HEARTBEAT_IVL", 2000),
    ("HEARTBEAT_TIMEOUT", 5000),
    ("HEARTBEAT_TTL", 6000),
)


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


# ---------------------------------------------------------------------------
# ticket 005: snapshot payload construction and event/snapshot application
# ---------------------------------------------------------------------------
#
# Two related but distinct shapes flow through this module:
#
# - A *snapshot* device dict (:func:`_snapshot_payload` /
#   :func:`_apply_snapshot_device`) carries a locally-owned row's full
#   identity, including ``port``/``vid_pid`` -- it is what a newly-joined
#   peer needs to bootstrap a remote-owned row from nothing.
# - A live *event* dict (:func:`_apply_event`, published by
#   :meth:`PeerDiscovery.publish_daemon_event`/``publish_lock_event``)
#   carries only what changed for that one event type. In particular an
#   "identity" event never repeats ``port``/``vid_pid`` -- applying it
#   must not overwrite those fields on a row an earlier "attach" event
#   (or the initial snapshot) already established, which is why identity
#   application is *not* routed through the same "upsert with port"
#   helper the snapshot path uses.


def _snapshot_payload(store: Store) -> list[dict[str, Any]]:
    """This host's own devices (``store.snapshot_local_devices()``),
    shaped for the wire -- what the REP handler sends back to a peer's
    snapshot request, and what :func:`_apply_snapshot_device` consumes on
    the receiving end.
    """
    return [
        {
            "uid": record.uid,
            "port": record.port,
            "vid_pid": record.vid_pid,
            "state": record.state,
            "role": record.role,
            "common_name": record.common_name,
            "device_name": record.device_name,
            "serial_payload": record.serial_payload,
            "raw_announcement": record.raw_announcement,
        }
        for record in store.snapshot_local_devices()
    ]


def _probe_result_from_dict(data: dict[str, Any]) -> ProbeResult:
    """Reconstruct a :class:`~mbtools.registry.identity.ProbeResult` from
    a snapshot/event dict's announcement fields -- shared by
    :func:`_apply_snapshot_device` and :func:`_apply_event`'s "identity"
    case. Blank fields round-trip as blank strings, matching
    ``identity.probe``'s own "malformed announcement" case (a line
    arrived but didn't parse -- fields blank, not missing).
    """
    return ProbeResult(
        role=data.get("role") or "",
        common_name=data.get("common_name") or "",
        device_name=data.get("device_name") or "",
        serial=data.get("serial_payload") or "",
        raw=data.get("raw_announcement") or "",
    )


def _apply_snapshot_device(store: Store, host: str, data: dict[str, Any]) -> None:
    """Apply one device dict from a peer's snapshot reply into ``store``,
    tagged with ``host`` -- ticket 005's acceptance criterion "every
    device in the snapshot is applied via ``upsert_remote_attached``/
    ``apply_remote_probe``, tagged with the peer's host".

    Always upserts first (establishing ``port``/``vid_pid`` even for a
    uid this store has never seen), then layers the announcement/state on
    top -- mirroring exactly what a local ``daemon.run_once()`` cycle
    would have done to reach this same state.
    """
    store.upsert_remote_attached(
        data["uid"], host, data.get("port"), data.get("vid_pid")
    )
    state = data.get("state")
    if state == STATE_CONNECTED:
        store.apply_remote_probe(data["uid"], _probe_result_from_dict(data))
    elif state == STATE_CONNECTED_NO_FIRMWARE:
        store.apply_remote_probe(data["uid"], None)
    elif state == "disconnected":
        store.mark_remote_detached(data["uid"])
    # "attached_unprobed": upsert_remote_attached above already covers it.


def _apply_event(store: Store, host: str, event: dict[str, Any]) -> None:
    """Apply one live event dict (from a peer's PUB stream) into
    ``store``, tagged with ``host`` -- ticket 005's acceptance criterion
    "applies each subsequent attach/detach/identity/lock_state event the
    same way [as the snapshot]".

    A reference to a uid this store has never heard of (an "identity"/
    "detach"/"lock_state" event arriving before the "attach" that should
    have preceded it -- possible in principle if a peer's own event
    ordering is ever violated, though the subscribe-before-snapshot
    ordering this module uses is designed to avoid it in practice) is
    logged and dropped rather than raised: a single missed/reordered
    event must not crash the receive loop it arrived on, and the peer's
    next full snapshot (a fresh discovery, or a reconnect after a vanish)
    naturally repairs any resulting gap.
    """
    event_type = event.get("type")
    uid = event.get("uid")
    try:
        if event_type == EVENT_ATTACH:
            store.upsert_remote_attached(
                uid, host, event.get("port"), event.get("vid_pid")
            )
        elif event_type == EVENT_DETACH:
            store.mark_remote_detached(uid)
        elif event_type == EVENT_IDENTITY:
            state = event.get("state")
            if state == STATE_CONNECTED:
                store.apply_remote_probe(uid, _probe_result_from_dict(event))
            elif state == STATE_CONNECTED_NO_FIRMWARE:
                store.apply_remote_probe(uid, None)
        elif event_type == EVENT_LOCK_STATE:
            store.apply_remote_lock_state(uid, event.get("kind"), event.get("display"))
        else:
            logger.warning("peering: unknown event type %r from %s", event_type, host)
    except KeyError:
        logger.warning(
            "peering: %s event for unknown uid %s from %s; dropping",
            event_type,
            uid,
            host,
        )


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

    ``on_peer_ready`` (ticket 005, optional -- ``None`` reproduces ticket
    004's discovery-only behavior exactly, which is what every ticket-004
    test still exercises) is called with ``(host, address, pub_port,
    snapshot_port)`` after ``record_peer_seen`` succeeds, whenever the
    discovered TXT record's ``pub_port``/``snapshot_port`` both parse --
    this is :meth:`PeerDiscovery.connect_peer`, wired by
    :meth:`PeerDiscovery.start`.

    **Self-filtering, two checks (ticket 014's hardware pass found the
    address/port check alone insufficient).** A multi-homed host --
    ``eth0``/``wlan0`` both up on the same LAN, an ordinary Nolanet-node
    configuration -- advertises its own service with exactly one address
    (``PeerDiscovery``'s own ``_local_ip()`` pick, fixed for the process's
    life), but a *browsing* ``zeroconf`` instance resolving that same
    service back can hand ``_resolve_address`` a *different* one of the
    host's own addresses (observed: the browse side's
    ``parsed_addresses()[0]`` came back as the other interface's IP, not
    the one actually registered) -- so ``address == self._own_address``
    silently fails to match and the host connects to *itself* as a peer.
    That is not a harmless no-op: the self-directed snapshot/event
    application (``_apply_snapshot_device``/``_apply_event``, both keyed
    by the discovered service's *name*, which for a self-connection is
    this host's own name) re-tags this host's own local device rows with
    ``host = <this host's own name>`` instead of leaving them ``NULL`` --
    and since the local daemon's own next attach-scan cycle (``store.
    upsert_attached``, ``host`` always ``NULL``) races the same rows
    against the self-peering link's re-tagging on every subsequent event,
    the two fight indefinitely and the device flickers between "local"
    and "owned by a peer that is actually itself", permanently hiding it
    from ``store.snapshot_local_devices()`` (``WHERE host IS NULL``)
    roughly half the time -- which is what a *newly joining* real peer's
    snapshot request can land on, receiving an empty list for a host that
    demonstrably has a device attached. Fixed by comparing the
    discovered service's bare hostname (``_peer_host_from_name``, parsed
    from the mDNS instance name itself, never re-derived from a
    resolved address) against this instance's own ``host`` -- identity
    that does not depend on which interface answered. The address/port
    check is kept as a second guard (harmless, and still correct for a
    single-homed host), not removed, so a caller that does not pass
    ``own_host`` -- ``tests/registry/peering/test_peering.py``'s own
    ``_listener`` fixture, deliberately left unchanged -- still filters
    correctly by address alone, same as before this fix.
    """

    def __init__(
        self,
        *,
        store: Store,
        service_type: str,
        own_address: str,
        own_port: int,
        own_host: str | None = None,
        resolve_timeout_ms: int = _DEFAULT_RESOLVE_TIMEOUT_MS,
        on_peer_ready: Callable[[str, str, int, int], None] | None = None,
    ) -> None:
        self._store = store
        self._service_type = service_type
        self._own_address = own_address
        self._own_port = own_port
        self._own_host = own_host
        self._resolve_timeout_ms = resolve_timeout_ms
        self._on_peer_ready = on_peer_ready

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
        host = _peer_host_from_name(name, self._service_type)
        if self._own_host is not None and host == self._own_host:
            return  # this instance's own advertisement -- never a peer of itself
        address = _resolve_address(info)
        port = getattr(info, "port", None)
        if address == self._own_address and port == self._own_port:
            return  # same check, by address/port -- kept as a second guard
            # for a caller that doesn't pass own_host (e.g. an older test
            # fixture); see the hostname check above for why address/port
            # alone is not reliable enough on its own.
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
        endpoint = f"{address}:{remote_port_str}"
        self._store.record_peer_seen(host, endpoint)

        if self._on_peer_ready is None:
            return
        pub_port_str = txt.get(TXT_PUB_PORT)
        snapshot_port_str = txt.get(TXT_SNAPSHOT_PORT)
        try:
            pub_port = int(pub_port_str) if pub_port_str is not None else None
            snapshot_port = int(snapshot_port_str) if snapshot_port_str is not None else None
        except ValueError:
            pub_port = None
            snapshot_port = None
        if pub_port is None or snapshot_port is None:
            logger.warning(
                "peering: discovered %s with no resolvable %s/%s TXT value; "
                "peer recorded but no live link established",
                name,
                TXT_PUB_PORT,
                TXT_SNAPSHOT_PORT,
            )
            return
        self._on_peer_ready(host, address, pub_port, snapshot_port)


class _PeerLink:
    """One peer's live ZeroMQ link: SUB-subscribe first, REQ snapshot
    fetch, then ongoing SUB consumption plus a monitor-socket watch for
    ``zmq.EVENT_DISCONNECTED`` (see the module docstring's "Subscribe-
    before-snapshot ordering" and "Peer-vanish detection" notes).

    Owned exclusively by :class:`PeerDiscovery` (constructed and started
    by :meth:`PeerDiscovery.connect_peer`, never directly by a caller
    outside this module) -- ``start()``/``stop()`` mirror every other
    module's thread-lifecycle convention: stoppable, every thread joined
    with a timeout before ``stop()`` returns, both idempotent.
    """

    def __init__(
        self,
        *,
        host: str,
        store: Store,
        zmq_module: Any,
        context: Any,
        pub_address: str,
        snapshot_address: str,
        on_unreachable: Callable[[str], None] | None,
        on_reachable: Callable[[str], None] | None,
        snapshot_timeout_ms: int = _DEFAULT_SNAPSHOT_TIMEOUT_MS,
        lock: threading.RLock | None = None,
        auth_token: str | None = None,
    ) -> None:
        self._host = host
        self._store = store
        self._zmq = zmq_module
        self._ctx = context
        self._pub_address = pub_address
        self._snapshot_address = snapshot_address
        self._on_unreachable = on_unreachable
        self._on_reachable = on_reachable
        self._snapshot_timeout_ms = snapshot_timeout_ms
        self._lock = lock if lock is not None else threading.RLock()
        self._auth_token = auth_token

        self._sub_socket: Any = None
        self._monitor_socket: Any = None
        self._stop_event = threading.Event()
        self._recv_thread: threading.Thread | None = None
        self._monitor_thread: threading.Thread | None = None
        self._started = False

    def start(self) -> None:
        if self._started:
            return
        self._started = True
        self._stop_event.clear()

        self._sub_socket = self._ctx.socket(self._zmq.SUB)
        self._sub_socket.setsockopt(self._zmq.SUBSCRIBE, b"")
        self._sub_socket.setsockopt(self._zmq.RCVTIMEO, _LOOP_POLL_TIMEOUT_MS)
        self._set_heartbeat_opts(self._sub_socket)
        self._sub_socket.connect(self._pub_address)

        self._monitor_socket = self._sub_socket.get_monitor_socket()
        self._monitor_socket.setsockopt(self._zmq.RCVTIMEO, _LOOP_POLL_TIMEOUT_MS)
        self._monitor_thread = threading.Thread(target=self._monitor_loop, daemon=True)
        self._monitor_thread.start()

        # Snapshot fetch happens *after* the SUB connect+subscribe above
        # -- see the module docstring's "Subscribe-before-snapshot
        # ordering" note.
        self._fetch_snapshot()

        self._recv_thread = threading.Thread(target=self._recv_loop, daemon=True)
        self._recv_thread.start()

    def _set_heartbeat_opts(self, sock: Any) -> None:
        for opt_name, value_ms in _HEARTBEAT_OPTS_MS:
            opt = getattr(self._zmq, opt_name, None)
            if opt is None:
                continue  # older libzmq without heartbeat support
            try:
                sock.setsockopt(opt, value_ms)
            except Exception:
                logger.debug("peering: %s unsupported on this SUB socket", opt_name)

    def _fetch_snapshot(self) -> None:
        req = self._ctx.socket(self._zmq.REQ)
        req.setsockopt(self._zmq.LINGER, 0)
        req.setsockopt(self._zmq.RCVTIMEO, self._snapshot_timeout_ms)
        req.setsockopt(self._zmq.SNDTIMEO, self._snapshot_timeout_ms)
        # ticket 009: when this side has an auth token configured, send
        # it as {"token": ...} instead of ticket 005's bare b"snapshot"
        # -- see the module docstring's "Peering handshake auth" note.
        # When unset (the default), the wire is byte-for-byte what
        # ticket 005 always sent, so an unauthenticated peer's REP
        # handler (which never inspects the request body at all in that
        # case) sees no difference.
        request = (
            json.dumps({"token": self._auth_token}).encode("utf-8")
            if self._auth_token is not None
            else b"snapshot"
        )
        try:
            req.connect(self._snapshot_address)
            req.send(request)
            reply = req.recv()
        except self._zmq.error.Again:
            logger.warning(
                "peering: snapshot request to %s (%s) timed out",
                self._host,
                self._snapshot_address,
            )
            return
        finally:
            req.close(linger=0)

        try:
            devices = json.loads(reply.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            logger.exception("peering: malformed snapshot reply from %s", self._host)
            return

        if isinstance(devices, dict):
            # {"error": "unauthorized"} (ticket 009) or any other
            # object-shaped reply -- never a valid snapshot (always a
            # list, see _snapshot_payload) -- treated like a timeout:
            # logged, no on_reachable, no peer row left half-updated.
            logger.warning(
                "peering: snapshot request to %s (%s) refused: %r",
                self._host,
                self._snapshot_address,
                devices,
            )
            return

        with self._lock:
            for device in devices:
                try:
                    _apply_snapshot_device(self._store, self._host, device)
                except Exception:
                    logger.exception(
                        "peering: failed applying snapshot device %r from %s",
                        device.get("uid"),
                        self._host,
                    )

        if self._on_reachable is not None:
            self._on_reachable(self._host)

    def _recv_loop(self) -> None:
        while not self._stop_event.is_set():
            try:
                message = self._sub_socket.recv()
            except self._zmq.error.Again:
                continue
            except self._zmq.error.ZMQError:
                break  # socket closed by stop()
            try:
                event = json.loads(message.decode("utf-8"))
                with self._lock:
                    _apply_event(self._store, self._host, event)
            except Exception:
                logger.exception("peering: failed applying event from %s", self._host)

    def _monitor_loop(self) -> None:
        while not self._stop_event.is_set():
            try:
                event = recv_monitor_message(self._monitor_socket)
            except self._zmq.error.Again:
                continue
            except self._zmq.error.ZMQError:
                break  # socket closed by stop()
            if event.get("event") == self._zmq.EVENT_DISCONNECTED:
                logger.warning(
                    "peering: link to %s (%s) dropped", self._host, self._pub_address
                )
                if self._on_unreachable is not None:
                    self._on_unreachable(self._host)

    def stop(self) -> None:
        """Stop both background threads (joined, with a timeout) before
        closing any socket -- a thread must never be left reading from a
        socket another thread is in the middle of closing. Idempotent.
        """
        if not self._started:
            return
        self._stop_event.set()
        if self._recv_thread is not None:
            self._recv_thread.join(timeout=2.0)
            self._recv_thread = None
        if self._monitor_thread is not None:
            self._monitor_thread.join(timeout=2.0)
            self._monitor_thread = None
        if self._sub_socket is not None:
            try:
                self._sub_socket.disable_monitor()
            except Exception:
                pass
            self._sub_socket.close(linger=0)
            self._sub_socket = None
        if self._monitor_socket is not None:
            self._monitor_socket.close(linger=0)
            self._monitor_socket = None
        self._started = False


class PeerDiscovery:
    """mDNS advertise/browse (ticket 004) plus the ZeroMQ PUB/REP event
    bus and per-peer SUB/REQ links (ticket 005) for ``_mbregistry._tcp``
    peering.

    ``start()``/``stop()`` lifecycle matches
    :class:`~mbtools.registry.api.RegistryAPIServer`'s own convention:
    ``start()`` is non-blocking, and ``stop()`` tears everything down --
    every peer link, the REP handler thread, the PUB/REP sockets and
    owned ``zmq.Context``, then (as ticket 004 already did) the browser
    and the injected/owned ``Zeroconf`` instance -- and joins every
    thread it owns before returning, per this ticket's own "any
    thread/socket started must be stoppable and joined in tests"
    requirement. Both are idempotent (a second ``start()``/``stop()`` is
    a no-op).

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
        zmq: Any = None,
        lock: threading.RLock | None = None,
        auth_token: str | None = None,
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
        self._zmq_module = zmq if zmq is not None else _real_zmq
        # ticket 009: the same threading.RLock `assemble_registry` shares
        # across Daemon/RegistryAPIServer/RemoteAPIServer -- guards every
        # store mutation this module makes on a peer's behalf (snapshot
        # apply, live-event apply, the REP handler's own snapshot read),
        # consistent with every other module's "every store/locks access
        # goes through the shared lock" convention, even though `Store`
        # is independently thread-safe on its own (see store.py's
        # "Thread safety" note) -- this is defense in depth/consistency,
        # not a correctness requirement peering has on its own. Defaults
        # to a private RLock, matching every other module here.
        self._lock = lock if lock is not None else threading.RLock()
        #: Optional shared-secret (sprint.md Decision 6) required of a
        #: peer's snapshot request -- see the module docstring's "Peering
        #: handshake auth" note below and :meth:`_rep_loop`/
        #: :class:`_PeerLink`'s own use of it.
        self._auth_token = auth_token

        self._zc: Any = None
        self._own_info: Any = None
        self._browser: Any = None
        self._started = False

        # -- ticket 005: ZeroMQ event bus state --
        self._zmq_ctx: Any = None
        self._pub_socket: Any = None
        self._pub_lock = threading.Lock()
        self._rep_socket: Any = None
        self._rep_thread: threading.Thread | None = None
        self._rep_stop_event = threading.Event()
        self._peer_links: dict[str, _PeerLink] = {}
        self._peer_links_lock = threading.Lock()

    # -- lifecycle -----------------------------------------------------

    def start(self) -> None:
        """Bind the PUB/REP sockets and start the REP handler thread,
        then register this host's own mDNS advertisement and start
        browsing. Returns immediately -- see the class docstring.

        The ZeroMQ half starts first because ``_BrowseListener`` (wired
        below with ``on_peer_ready=self.connect_peer``) can, in
        principle, fire a discovery callback the instant the browser
        starts -- :meth:`connect_peer` must have a live ``zmq.Context``
        to use by then.
        """
        if self._started:
            return

        self._zmq_ctx = self._zmq_module.Context()
        self._pub_socket = self._zmq_ctx.socket(self._zmq_module.PUB)
        self._pub_socket.bind(f"tcp://*:{self._pub_port}")
        self._rep_socket = self._zmq_ctx.socket(self._zmq_module.REP)
        self._rep_socket.bind(f"tcp://*:{self._snapshot_port}")
        self._rep_socket.setsockopt(self._zmq_module.RCVTIMEO, _LOOP_POLL_TIMEOUT_MS)
        self._rep_stop_event.clear()
        self._rep_thread = threading.Thread(target=self._rep_loop, daemon=True)
        self._rep_thread.start()

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
            own_host=self._host,
            on_peer_ready=self.connect_peer,
        )
        self._browser = self._zc_module.ServiceBrowser(
            self._zc, self._service_type, listener=listener
        )
        self._started = True

    def stop(self) -> None:
        """Cancel the browser, unregister this host's own advertisement,
        and close the ``Zeroconf`` instance (ticket 004), then stop every
        peer link, the REP handler thread, and close the PUB/REP sockets
        and owned ``zmq.Context`` (ticket 005). Idempotent.
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

        with self._peer_links_lock:
            links = list(self._peer_links.values())
            self._peer_links.clear()
        for link in links:
            link.stop()

        self._rep_stop_event.set()
        if self._rep_thread is not None:
            self._rep_thread.join(timeout=2.0)
            self._rep_thread = None
        if self._rep_socket is not None:
            self._rep_socket.close(linger=0)
            self._rep_socket = None
        if self._pub_socket is not None:
            self._pub_socket.close(linger=0)
            self._pub_socket = None
        if self._zmq_ctx is not None:
            self._zmq_ctx.term()
            self._zmq_ctx = None

        self._started = False

    def __enter__(self) -> "PeerDiscovery":
        self.start()
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.stop()

    # -- ticket 005: snapshot REP handler --------------------------------

    def _rep_loop(self) -> None:
        """Answer every snapshot request with this host's own current
        devices (:func:`_snapshot_payload`) -- or, when ``auth_token``
        (ticket 009) is set and the request's token is missing/wrong, an
        ``{"error": "unauthorized"}`` reply instead (see the module
        docstring's "Peering handshake auth" note).

        Runs on its own thread against its own socket -- never the PUB
        socket -- so a slow or stalled requester can never block this
        host's own event publishing (this ticket's explicit "REP never
        blocks PUB" acceptance criterion, true by construction: two
        sockets, two code paths, nothing shared but ``store`` itself,
        which is already thread-safe -- see ``store.py``'s own "Thread
        safety" docstring note).
        """
        while not self._rep_stop_event.is_set():
            try:
                message = self._rep_socket.recv()
            except self._zmq_module.error.Again:
                continue
            except self._zmq_module.error.ZMQError:
                break  # socket closed by stop()
            if self._auth_token is not None and not self._token_ok(message):
                payload = json.dumps({"error": "unauthorized"}).encode("utf-8")
            else:
                with self._lock:
                    payload = json.dumps(_snapshot_payload(self._store)).encode("utf-8")
            self._rep_socket.send(payload)

    def _token_ok(self, message: bytes) -> bool:
        """Does ``message`` (a snapshot REQ's raw payload) carry a
        ``{"token": ...}`` matching :attr:`_auth_token`? Only called when
        :attr:`_auth_token` is set (see :meth:`_rep_loop`) -- a
        non-JSON or JSON-but-wrong-shape message (e.g. ticket 005's
        legacy bare ``b"snapshot"``) is simply not-ok, never an
        exception.
        """
        try:
            data = json.loads(message.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            return False
        return isinstance(data, dict) and data.get("token") == self._auth_token

    # -- ticket 005: peer link lifecycle ---------------------------------

    def connect_peer(
        self,
        host: str,
        address: str,
        pub_port: int,
        snapshot_port: int,
        *,
        remote_port: int | None = None,
    ) -> None:
        """Establish (or re-establish) a live peering link to ``host`` at
        ``address``'s ``pub_port``/``snapshot_port``.

        This is what a discovered peer's TXT record triggers (wired as
        ``_BrowseListener``'s ``on_peer_ready`` in :meth:`start`), and
        what ticket 009's ``--peer HOST:PORT`` CLI flag and a test
        exercising "no mDNS involved" both call directly -- the one
        method every path into a live peer link funnels through.

        **``remote_port`` and the record-before-connect pitfall.** Ticket
        004's mDNS path never has to think about this: ``_BrowseListener``
        already calls ``store.record_peer_seen(host, endpoint)`` itself,
        from the TXT record's own advertised remote port, *before* it
        ever invokes this method as ``on_peer_ready`` -- so by the time a
        link's snapshot exchange succeeds and fires ``on_reachable`` ->
        :meth:`_on_peer_reachable` -> ``store.mark_peer_reachable(host)``,
        a ``peer`` row already exists for ``host`` to mark. A caller with
        no such prior step -- concretely, ticket 009's ``--peer
        HOST[:PORT]`` CLI flag, which has no ``_BrowseListener`` doing
        this for it -- must pass ``remote_port`` here so this method does
        the recording itself, in the same record-then-connect order, before
        the link is established. Omitting it when no prior
        ``record_peer_seen`` has happened is exactly the bug this
        parameter exists to prevent: the link still connects and even
        snapshot-syncs successfully, but ``_on_peer_reachable``'s
        ``store.mark_peer_reachable`` then raises ``KeyError`` for a host
        with no ``peer`` row -- caught and logged (see
        :meth:`_on_peer_reachable`), never raised to the caller -- so the
        peer's own reachability silently never appears in ``store``,
        even though devices from its snapshot/events keep flowing in.
        Defaults to ``None`` (skip the recording here), which reproduces
        ticket 005's exact behavior for the mDNS path -- every existing
        ticket-004/005 test that calls this method directly, having
        already called ``record_peer_seen`` itself first, is unaffected.

        Safe to call more than once for the same ``host`` (an mDNS
        ``update_service`` refresh, or an explicit reconnect after a
        vanish per Decision 5, "a later reconnect ... re-runs the
        snapshot exchange"): any existing link for that host is stopped
        first, then a fresh one is started, which always re-does the
        snapshot exchange and marks the peer reachable again on success.
        """
        if remote_port is not None:
            self._store.record_peer_seen(host, f"{address}:{remote_port}")

        with self._peer_links_lock:
            existing = self._peer_links.pop(host, None)
        if existing is not None:
            existing.stop()

        link = _PeerLink(
            host=host,
            store=self._store,
            zmq_module=self._zmq_module,
            context=self._zmq_ctx,
            pub_address=f"tcp://{address}:{pub_port}",
            snapshot_address=f"tcp://{address}:{snapshot_port}",
            on_unreachable=self._on_peer_unreachable,
            on_reachable=self._on_peer_reachable,
            lock=self._lock,
            auth_token=self._auth_token,
        )
        with self._peer_links_lock:
            self._peer_links[host] = link
        link.start()

    def _on_peer_unreachable(self, host: str) -> None:
        try:
            self._store.mark_peer_unreachable(host)
        except KeyError:
            logger.warning("peering: unreachable callback for unknown peer %s", host)

    def _on_peer_reachable(self, host: str) -> None:
        try:
            self._store.mark_peer_reachable(host)
        except KeyError:
            logger.warning("peering: reachable callback for unknown peer %s", host)

    # -- ticket 005: publishing (this host's own event bus) -------------

    def publish_event(self, event_type: str, payload: dict[str, Any]) -> None:
        """Publish one event of ``event_type`` on this host's PUB socket,
        merged with ``{"type": event_type, "host": self._host}``.

        A no-op if :meth:`start` has not been called (or after
        :meth:`stop`) -- lets a caller hold a reference to an
        as-yet-unstarted/already-stopped ``PeerDiscovery`` without
        needing its own None-check. Guarded by :attr:`_pub_lock` since a
        PUB socket, like every ZMQ socket, is not safe to use
        concurrently from more than one thread, and this method is
        called from whichever thread ``daemon``/``locks`` fire their
        hooks on.
        """
        if self._pub_socket is None:
            return
        message = json.dumps({"type": event_type, "host": self._host, **payload}).encode(
            "utf-8"
        )
        with self._pub_lock:
            self._pub_socket.send(message)

    def publish_daemon_event(self, event_type: str, record: DeviceRecord) -> None:
        """Adapter matching :class:`~mbtools.registry.daemon.Daemon`'s
        ``event_callback`` signature exactly -- ticket 009's assembly
        wires this in directly (``Daemon(..., event_callback=peering.
        publish_daemon_event)``), no glue code needed at the call site.

        ``event_type`` is one of ``EVENT_ATTACH``/``EVENT_DETACH``/
        ``EVENT_IDENTITY``; the payload fields sent are exactly what
        :func:`_apply_event`'s matching branch reads back out on the
        receiving side.
        """
        if event_type == EVENT_ATTACH:
            payload = {"uid": record.uid, "port": record.port, "vid_pid": record.vid_pid}
        elif event_type == EVENT_DETACH:
            payload = {"uid": record.uid}
        else:
            payload = {
                "uid": record.uid,
                "state": record.state,
                "role": record.role,
                "common_name": record.common_name,
                "device_name": record.device_name,
                "serial_payload": record.serial_payload,
                "raw_announcement": record.raw_announcement,
            }
        self.publish_event(event_type, payload)

    def publish_lock_event(self, uid: str, kind: str | None, display: str | None) -> None:
        """Adapter matching :class:`~mbtools.registry.locks.LockManager`'s
        ``lock_display_callback`` signature exactly -- ticket 009's
        assembly wires this in directly (``LockManager(...,
        lock_display_callback=peering.publish_lock_event)``).
        """
        self.publish_event(EVENT_LOCK_STATE, {"uid": uid, "kind": kind, "display": display})
