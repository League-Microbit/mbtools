"""mbtools.registry.console_compat.relay_pool — the ``_mbrelay._tcp``
pool-port TCP listener (sprint 004, ticket 006, architecture Decision 1).

Per sprint.md's Step 3 module table ("Give a raw TCP client a
freshly-reset, normalized *local* relay per connection, matching today's
``mbrelay`` pool port") and SUC-002, this is what lets robot-console
(unmodified -- `packages/host/src/discovery/mdnsDiscovery.ts`,
`watchers/mdnsWatcher.ts`, `connect/relayBridger.ts`, `link/
RelayCommandPlane.ts`) keep working against ``mbregistry`` instead of the
retiring ``mbrelay`` daemon: it advertises ``_mbrelay._tcp`` over mDNS and,
on every new TCP connection, hands the client a relay board that has just
been reset and verified at factory defaults -- "a drop-in replacement for
opening the serial port directly" (the legacy daemon's own
``docs/relay-server.md``, "The guarantee"), reproduced here against
``mbregistry``'s own store/locks instead of a second, competing serial
scanner (brief §1's whole reason for existing).

**Verified against robot-console's own source, not this sprint's
paraphrase of the brief** (Part 1 research): ``mdnsDiscovery.ts``'s
``RELAY_SERVICE_TYPE = "mbrelay"`` (so ``_mbrelay._tcp``), its own
live-captured example (`torture.local.:8760`, TXT `registry=8761`) is
"SRV port = the pool port a client dials; TXT `registry=<port>` = a
*separate* HTTP port" -- confirmed by ``mbrelayRegistry.ts`` GETting
`/names/<name>` on that second port, never the pool port. ``link/
RelayCommandPlane.ts`` never inspects a boot banner itself -- it drives
its own ``sync()`` (`?` retried until a `#`-prefixed status line answers)
then ``!ECHO OFF``/``!MODE RAW250``/``!CG``/``!P 7``/``!GO`` -- so this
module's job is only to have the board already reset and idle in the
command plane, banner already sent, *before* that preamble starts; it
never needs to parse or react to robot-console's own commands beyond
relaying bytes.

**One TCP connection, one relay, for the connection's whole life**
(architecture Decision 5: this pool serves only this host's own local
relays -- ``store.snapshot_local_devices()``, never a peer's, so there is
no remote-relay-proxying layer to build). On accept:

1. Under the shared assembly lock (``lock``, the same
   ``threading.RLock`` ``registry.cli.assemble_registry`` already shares
   across ``daemon``/``api``/``remote_api``/``peering`` -- see that
   module's own docstring): pick a free local relay (``role`` contains
   ``RELAY``/``BRIDGE``, ``state == "connected"``, unlocked) and, if one
   exists, acquire a ``relay``-kind lock on it for an in-daemon holder
   identity (:func:`_make_holder` -- this pool session is not a separate
   process or a registry client connection, so neither ``api.py``'s
   ``SO_PEERCRED`` pid extraction nor ``remote_api.py``'s per-connection
   session id applies; see that function's own docstring).
2. If none is free: the legacy pool's own answer, in the relay's own
   comment syntax (``docs/relay-server.md`` §3: "a byte pipe has no error
   channel, so the daemon says why ... and then closes") -- write
   ``# ERROR: no relay available (...)`` and close. Decided over silently
   refusing the connection because the message costs nothing (any client
   already written against the relay protocol ignores ``#`` lines) and a
   human on ``nc``/``mbrelay connect`` gets a readable answer, matching
   this ticket's own acceptance criterion ("decide and document one
   behavior; do not hang").
3. Outside the lock (this module's own "no serial I/O while holding the
   assembly lock" rule, matching ``remote_api.py``'s identical one for
   its own ``stream`` sub-protocol): ``RelayControl.reset_and_normalize()``
   (ticket 003) over a fresh :class:`~mbtools.relay.channel.
   LocalRelayChannel` (ticket 004) -- BREAK/reset if needed, ``HELLO``,
   ``!VER?``, the ``NORMALIZE_STEPS`` batch, verified -- then the
   resulting :class:`~mbtools.relay.protocol.BannerInfo`'s ``raw`` bytes
   (the board's own announcement line, e.g. ``DEVICE:RADIOBRIDGE:relay:
   togov:1234``) are written to the client socket first, plus a trailing
   ``\\r\\n`` -- exactly the legacy pool's own ``info.raw + b"\\r\\n"``
   preamble (``microbit-radio-relay/server/src/mbrelay/session.py``).
4. The channel is reopened (``open()`` never resets -- see
   ``relay.channel``'s own module docstring; the board stays exactly as
   normalize left it) and pumped as a raw, transparent byte pipe against
   the socket until the client disconnects, the relay itself errors, or
   this pool is stopped.
5. On disconnect (clean or abrupt): ``reset_and_normalize()`` again, this
   time with ``clear_stored=True`` (``!DEFAULTS``, restoring the *stored*
   flash record too -- reset-by-reconnect, ``docs/relay-server.md``'s
   documented model exactly, and this ticket's own acceptance criterion),
   then the lock is released. This runs in the connection handler's own
   ``finally``, so it happens on every exit path -- a clean client close,
   a relay-side error, or an exception raised while sending the banner --
   never leaving a board locked with no path back to the pool.

**Exempt from ``--auth-token``** (sprint.md's own Migration Concerns:
"robot-console has no mechanism to send one, matching legacy ``mbrelay``'s
own documented no-auth, internal-LAN-service posture for this exact
surface"): this module never checks one, and never will -- there is
nowhere in this wire contract for one to go without diverging from
robot-console's own hardcoded expectations.

**New default port, distinct from legacy ``mbrelay``'s 8760**
(architecture Decision 6, "in this project's existing 7440/7442/7443
port-numbering sequence"): :data:`DEFAULT_POOL_PORT` is ``7444``,
:data:`DEFAULT_NAMES_API_PORT` is ``7445`` (ticket 007's own default --
not yet served by anything; this module only ever *advertises* it in the
``registry`` TXT key, per the module table's "TXT ``registry=<names_api
port>``"). Both are discovered live via mDNS SRV/TXT by any real client,
per Decision 6's own "no client hardcodes them" consequence, so the exact
numbers only need to avoid colliding with 8760/8761 during sprint 005's
migration window, not match any existing convention.
"""

from __future__ import annotations

import logging
import os
import socket
import threading
from typing import Any, Callable
from uuid import uuid4

import zeroconf as _real_zeroconf

from mbtools.registry.locks import KIND_RELAY, HolderRef, LockManager
from mbtools.registry.store import STATE_CONNECTED, DeviceRecord, Store
from mbtools.relay.channel import LocalRelayChannel
from mbtools.relay.protocol import RelayControl, RelayError

__all__ = [
    "RelayPool",
    "SERVICE_TYPE",
    "DEFAULT_POOL_PORT",
    "DEFAULT_NAMES_API_PORT",
    "TXT_REGISTRY_PORT",
    "DEFAULT_REJECT_MESSAGE",
]

logger = logging.getLogger(__name__)

#: mDNS service type robot-console's own ``mdnsDiscovery.ts``/
#: ``mdnsWatcher.ts`` browse for (``RELAY_SERVICE_TYPE = "mbrelay"`` ->
#: ``_mbrelay._tcp``) -- verified directly against that source, per the
#: module docstring. The trailing-dot ``.local.`` form matches
#: ``registry.peering.SERVICE_TYPE``'s own convention.
SERVICE_TYPE = "_mbrelay._tcp.local."

#: See the module docstring's "New default port" section.
DEFAULT_POOL_PORT = 7444
DEFAULT_NAMES_API_PORT = 7445

#: TXT record key carrying the ``names_api`` HTTP port -- what
#: ``mdnsDiscovery.ts``'s ``parseRegistryPort(service.txt?.registry)``
#: parses, and ``mbrelayRegistry.ts`` then GETs ``/names/<name>`` on.
TXT_REGISTRY_PORT = "registry"

_ACCEPT_BACKLOG = 8

#: The legacy pool's own refusal message shape (``docs/relay-server.md``
#: §3), minus the "being handed back" count this module has no equivalent
#: state for -- a board mid release-reset is simply still locked (this
#: module never releases before that reset completes), so it is already
#: counted in ``busy``.
DEFAULT_REJECT_MESSAGE = "# ERROR: no relay available ({total} devices, {busy} in use)"

_RAW_READ_CHUNK = 4096


def _is_relay_role(role: "str | None") -> bool:
    """``role`` contains ``RELAY`` or ``BRIDGE``, case-insensitively --
    local copy of ``relay.cli``'s own ``_is_relay_role`` (that module's
    own filter for "any relay/bridge device"), duplicated rather than
    imported for the same small-self-contained-check reason
    ``relay.protocol`` keeps its own local ``BANNER_RE``/``IDENTITY_RE``
    copies (that module's own docstring)."""
    if not role:
        return False
    upper = role.upper()
    return "RELAY" in upper or "BRIDGE" in upper


def _local_ip() -> str:
    """Best-effort LAN IPv4 address (not ``127.0.0.1``) for this host.

    Local copy of ``registry.peering``'s own ``_local_ip`` (itself ported
    from ``mbdeploy/mdns.py``) -- the standard "connect a UDP socket, read
    back the local endpoint" trick (no packets actually sent). Falls back
    to resolving this host's own name if that fails.
    """
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("8.8.8.8", 80))
        return s.getsockname()[0]
    except OSError:
        return socket.gethostbyname(socket.gethostname())
    finally:
        s.close()


def _short_hostname() -> str:
    """This host's bare hostname, with any domain suffix stripped -- local
    copy of ``registry.peering``'s own ``_short_hostname``. This is the
    instance name advertised (the module docstring's "instance name =
    short hostname"), matching robot-console's own live-captured example
    (``torture``, a bare hostname, not a per-relay-board name)."""
    return socket.gethostname().split(".")[0]


def _encode_txt(txt: "dict[str, str]") -> "dict[bytes, bytes]":
    """UTF-8-encode a plain ``str`` TXT dict into zeroconf's byte form --
    local copy of ``registry.peering``'s own ``_encode_txt``."""
    return {
        str(key).encode("utf-8"): str(value if value is not None else "").encode("utf-8")
        for key, value in txt.items()
    }


class RelayPool:
    """The ``_mbrelay._tcp`` pool-port listener -- see the module
    docstring for the full per-connection contract.

    ``store``/``locks`` are injected references, the same instances a
    caller (``registry.cli``'s ``mbregistry run`` assembly, or a test)
    already constructed for the rest of the daemon -- this class never
    constructs its own, mirroring every other network-facing module in
    this package (``registry.api``/``registry.remote_api``/``registry.
    peering``)'s own "don't construct a second one of these" contract.

    ``lock`` is the shared ``threading.RLock`` ``registry.cli.
    assemble_registry`` already threads through ``daemon``/``api``/
    ``remote_api``/``peering`` -- every ``store``/``locks`` access this
    class makes (picking a free relay, acquiring/releasing its lock) goes
    through it, same reason as every other module here; defaults to a
    private ``RLock`` of this instance's own when omitted (every test in
    this module that constructs a ``RelayPool`` standalone).

    ``host``/``port`` default to every interface and
    :data:`DEFAULT_POOL_PORT`; a test passes ``port=0`` to let the OS
    choose an ephemeral port (see :attr:`bound_port`). ``names_api_port``
    is what gets advertised in the ``registry`` TXT key (see the module
    docstring) -- this class never opens or checks that port itself.

    ``control``/``channel_factory``/``zeroconf`` are test seams:
    ``control`` defaults to a real :class:`~mbtools.relay.protocol.
    RelayControl`; ``channel_factory`` defaults to
    :class:`~mbtools.relay.channel.LocalRelayChannel` itself (called as
    ``channel_factory(port)``) -- a test substitutes one backed by a fake
    serial port, the same ``serial_factory`` seam ``tests/relay/
    test_channel.py`` already exercises; ``zeroconf`` defaults to the
    real ``zeroconf`` package, mirroring ``registry.peering``'s own
    injectable-``zeroconf`` convention.
    """

    def __init__(
        self,
        *,
        store: Store,
        locks: LockManager,
        host: str = "0.0.0.0",
        port: int = DEFAULT_POOL_PORT,
        names_api_port: int = DEFAULT_NAMES_API_PORT,
        instance_host: "str | None" = None,
        advertise_address: "str | None" = None,
        lock: "threading.RLock | None" = None,
        control: "RelayControl | None" = None,
        channel_factory: "Callable[[str], Any] | None" = None,
        zeroconf: Any = None,
        reject_message: str = DEFAULT_REJECT_MESSAGE,
    ) -> None:
        self._store = store
        self._locks = locks
        self._host = host
        self._port = port
        self._names_api_port = names_api_port
        self._instance_host = instance_host if instance_host is not None else _short_hostname()
        self._advertise_address = (
            advertise_address if advertise_address is not None else _local_ip()
        )
        self._lock = lock if lock is not None else threading.RLock()
        self._control = control if control is not None else RelayControl()
        self._channel_factory: Callable[[str], Any] = (
            channel_factory if channel_factory is not None else LocalRelayChannel
        )
        self._zc_module = zeroconf if zeroconf is not None else _real_zeroconf
        self._reject_message = reject_message

        self._sock: "socket.socket | None" = None
        self._accept_thread: "threading.Thread | None" = None
        self._conn_threads: "list[threading.Thread]" = []
        self._stop_event = threading.Event()
        self._zc: Any = None
        self._own_info: Any = None
        self._started = False

    @property
    def bound_port(self) -> int:
        """The actual bound TCP port -- useful when constructed with
        ``port=0`` for a test to discover the real ephemeral port the OS
        chose (also what :meth:`start` advertises as the SRV port -- read
        back live, per the module docstring's own "read back from the
        live socket, not blindly from config" note)."""
        assert self._sock is not None
        return self._sock.getsockname()[1]

    # -- lifecycle -----------------------------------------------------

    def start(self) -> None:
        """Bind the pool-port socket, start accepting connections on a
        background thread, then register the ``_mbrelay._tcp`` mDNS
        advertisement (SRV -> the just-bound port). Returns immediately.
        Idempotent."""
        if self._started:
            return

        self._sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._sock.bind((self._host, self._port))
        self._sock.listen(_ACCEPT_BACKLOG)
        self._port = self._sock.getsockname()[1]
        self._stop_event.clear()

        self._accept_thread = threading.Thread(
            target=self._accept_loop, name="mbtools-relay-pool-accept", daemon=True
        )
        self._accept_thread.start()

        self._zc = self._zc_module.Zeroconf()
        txt = {TXT_REGISTRY_PORT: str(self._names_api_port)}
        self._own_info = self._zc_module.ServiceInfo(
            SERVICE_TYPE,
            f"{self._instance_host}.{SERVICE_TYPE}",
            addresses=[socket.inet_aton(self._advertise_address)],
            port=self._port,
            properties=_encode_txt(txt),
            server=f"{self._instance_host}.local.",
        )
        self._zc.register_service(self._own_info, allow_name_change=True)

        self._started = True

    def stop(self) -> None:
        """Withdraw the mDNS advertisement, stop accepting new
        connections, and join every connection-handler thread that is
        still alive before returning -- a caller that tears down
        ``store``/``locks`` immediately after ``stop()`` returns must not
        race a not-yet-finished handler thread (same reasoning as every
        other network-facing module in this package). An in-flight
        client session is not forcibly severed -- it keeps running until
        the client disconnects, same as ``registry.remote_api.
        RemoteAPIServer.stop``'s own convention. Idempotent."""
        if not self._started:
            return

        if self._zc is not None:
            if self._own_info is not None:
                try:
                    self._zc.unregister_service(self._own_info)
                except Exception:
                    logger.exception("relay_pool: error unregistering own mDNS advertisement")
            self._zc.close()
        self._zc = None
        self._own_info = None

        self._stop_event.set()
        if self._sock is not None:
            try:
                self._sock.close()
            except OSError:
                pass
        if self._accept_thread is not None:
            self._accept_thread.join(timeout=2.0)
            self._accept_thread = None
        for thread in self._conn_threads:
            thread.join(timeout=2.0)
        self._conn_threads.clear()

        self._started = False

    def __enter__(self) -> "RelayPool":
        self.start()
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.stop()

    # -- accept loop -----------------------------------------------------

    def _accept_loop(self) -> None:
        assert self._sock is not None
        while not self._stop_event.is_set():
            try:
                conn, _addr = self._sock.accept()
            except OSError:
                break  # socket closed by stop()
            try:
                conn.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            except OSError:
                pass
            thread = threading.Thread(
                target=self._handle_connection, args=(conn,),
                name="mbtools-relay-pool-conn", daemon=True,
            )
            thread.start()
            self._conn_threads.append(thread)

    # -- per-connection handling -------------------------------------------

    def _make_holder(self) -> HolderRef:
        """A ``relay``-kind lock holder identity for one in-daemon pool
        session.

        Neither existing :class:`HolderRef` origin fits verbatim: this
        session is not a separate process ``api.py`` reads a pid off of
        (there is no peer socket to call ``SO_PEERCRED`` on -- the pool
        *is* the process), and it is not a peer registry's own connection
        the way ``remote_api.py``'s ``"remote"`` origin models (this
        session never crosses a registry-to-registry trust boundary; it's
        an unauthenticated raw client of this same daemon). ``"local"``
        is the closer fit -- the mbregistry process itself is the true
        holder -- so ``pid`` is this process's own pid
        (:func:`os.getpid`), and ``ref`` is a fresh, per-connection UUID
        (not the shared pid, which every pool session would otherwise
        collide on) so two concurrent pool sessions never compare equal
        under :meth:`~mbtools.registry.locks.LockManager.release`'s own
        full-equality check.
        """
        return HolderRef(origin="local", ref=f"console-compat-pool:{uuid4()}", pid=os.getpid())

    def _pick_free_local_relay(self) -> "DeviceRecord | None":
        """A free, local, relay/bridge-role device with a known port, or
        ``None``. Must be called with :attr:`_lock` already held (every
        call site holds it) -- reads ``store``/``locks`` together as one
        atomic "is anything free" check, the same reason
        ``relay.cli._find_free_relay`` (via a registry ``list`` op) does
        its own filtering client-side rather than racing two separate
        calls.

        ``state == STATE_CONNECTED`` is this module's own addition beyond
        ``relay.cli``'s filter (which only checks ``role``/lock, since it
        goes through a registry ``list`` op that already reflects live
        state at read time): ``store.snapshot_local_devices()`` excludes
        fully ``disconnected`` local rows (sprint 005 ticket 011), but
        still includes ``attached_unprobed``/``connected_no_firmware``
        ones -- a board mid-probe or that failed to announce is not yet
        (or never) actually usable, so without this check a not-yet-ready
        board's row would be offered and immediately fail to open.
        """
        for record in self._store.snapshot_local_devices():
            if record.state != STATE_CONNECTED:
                continue
            if not record.port:
                continue
            if not _is_relay_role(record.role):
                continue
            if self._locks.status(record.uid) is not None:
                continue
            return record
        return None

    def _local_relay_counts(self) -> "tuple[int, int]":
        """``(total, busy)`` among this host's local relay/bridge-role
        devices, for :meth:`_reject`'s message -- must be called with
        :attr:`_lock` already held."""
        relays = [
            record
            for record in self._store.snapshot_local_devices()
            if _is_relay_role(record.role)
        ]
        total = len(relays)
        busy = sum(1 for record in relays if self._locks.status(record.uid) is not None)
        return total, busy

    @staticmethod
    def _safe_close(conn: socket.socket) -> None:
        try:
            conn.close()
        except OSError:
            pass

    @staticmethod
    def _safe_shutdown(conn: socket.socket) -> None:
        """Best-effort ``shutdown(SHUT_RDWR)`` -- used to unblock this
        connection's own blocking ``recv()`` loop from another thread
        (the channel's background reader, on a device-side error) without
        waiting for the client to notice anything is wrong first."""
        try:
            conn.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass

    def _reject(self, conn: socket.socket, total: int, busy: int) -> None:
        """No local relay free -- the legacy pool's own answer (module
        docstring): a ``#``-prefixed comment line, then close. Never
        hangs and never blocks a client waiting for a board that isn't
        coming (this ticket's own acceptance criterion)."""
        message = self._reject_message.format(total=total, busy=busy)
        try:
            conn.sendall(message.encode("utf-8", "replace") + b"\r\n")
        except OSError:
            pass
        self._safe_close(conn)

    def _handle_connection(self, conn: socket.socket) -> None:
        """One TCP client's whole session -- see the module docstring's
        numbered per-connection contract. Runs on its own thread (one per
        accepted connection, spawned by :meth:`_accept_loop`)."""
        holder = self._make_holder()
        with self._lock:
            record = self._pick_free_local_relay()
            if record is not None:
                self._locks.acquire(record.uid, KIND_RELAY, holder)
            total, busy = self._local_relay_counts()

        if record is None:
            self._reject(conn, total, busy)
            return

        assert record.port  # _pick_free_local_relay only ever returns a ported record
        channel = self._channel_factory(record.port)
        try:
            try:
                banner = self._control.reset_and_normalize(channel, clear_stored=False)
            except RelayError:
                logger.exception(
                    "relay_pool: reset/normalize failed for %s on acquire; closing",
                    record.uid,
                )
                return
            try:
                conn.sendall(banner.raw + b"\r\n")
            except OSError:
                return

            self._pump(conn, channel, record.uid)
        finally:
            try:
                self._control.reset_and_normalize(channel, clear_stored=True)
            except RelayError:
                logger.warning(
                    "relay_pool: reset/normalize failed for %s on release", record.uid
                )
            with self._lock:
                self._locks.release(record.uid, holder)
            self._safe_close(conn)

    def _pump(self, conn: socket.socket, channel: Any, uid: str) -> None:
        """The raw, transparent byte pipe -- socket bytes go straight to
        the channel's :meth:`~mbtools.relay.protocol.ByteChannel.
        write_nowait`, and whatever the channel reads is pushed straight
        back to the socket, until the client disconnects, the channel
        errors, or a malformed/closed socket ends the loop. No framing,
        no interpretation of what passes through either direction --
        matching ``docs/relay-server.md``'s own design target, "a drop-in
        replacement for opening the serial port directly."

        Reopens ``channel`` itself first (the acquire-time ``reset_and_
        normalize`` call already closed it in its own ``finally`` --
        ticket 003's convenience-wrapper contract) -- safe and reset-free,
        since ``LocalRelayChannel.open()`` never resets the board (``relay
        .channel``'s own module docstring) and only ever re-establishes
        the already-normalized transport.
        """
        channel.open()
        try:
            def on_data(data: bytes) -> None:
                try:
                    conn.sendall(data)
                except OSError:
                    pass

            def on_error(_exc: "BaseException | None") -> None:
                # The relay's own read failed (board vanished, port
                # error) -- unblock this method's own conn.recv() loop
                # below from here, on the channel's background thread,
                # rather than waiting for the client to notice anything.
                self._safe_shutdown(conn)

            channel.start_reading(on_data, on_error)
            try:
                while True:
                    try:
                        data = conn.recv(_RAW_READ_CHUNK)
                    except OSError:
                        break
                    if not data:
                        break  # clean EOF -- the client disconnected
                    channel.write_nowait(data)
            finally:
                channel.stop_reading()
        finally:
            channel.close()
