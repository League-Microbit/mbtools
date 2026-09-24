"""mbtools.registry.remote_api — the TCP control-plane server giving a
client on another host the same ``list``/``get``/``find``/``lock``/
``unlock``/``mark_flashed`` access ``registry.api.RegistryAPIServer``'s
local Unix socket gives, scoped to *this* registry's own (``host IS
NULL``) devices.

Per sprint.md's Architecture (module ``registry.remote_api``, Step 5) and
ticket 006's own Description, this is a TCP listener parallel to
``api.RegistryAPIServer``'s existing Unix socket, sharing that module's
per-op dispatch logic (:mod:`mbtools.registry._api_base`) rather than
duplicating it — the two servers differ only in transport, peer-identity
extraction, and (tickets 007/008) which extra ops they support. This
module covers the JSON-op sub-protocol only: the framed binary stream
sub-protocol (ticket 007) and remote ``flash`` (ticket 008) are separate
tickets, since they change for different reasons (sprint.md Step 1-2,
responsibilities 3/4/5) — this class has no ``flash``/``stream`` op yet.

**Session identity, not PID identity** (sprint.md Decision 2, ticket
002's own module docstring: "a PID means nothing across hosts"): each
accepted connection is minted a fresh, random session id
(:func:`uuid.uuid4`) and wrapped in a remote-origin
:class:`~mbtools.registry.locks.HolderRef` — never a client-supplied
value, mirroring ``api.py``'s own "never a client-supplied PID"
precedent for the local socket. ``host`` on that ``HolderRef`` is the
connecting client's own source IP, read from the kernel via
``conn.getpeername()`` — sprint.md's Open Question left "source IP vs. a
client-declared display host" for the implementer; the kernel-verified
source IP was chosen for the same reason a PID is: an identity used to
grant or refuse a lock should never be something the client gets to
assert for itself. A prettier display host, if wanted later for
"locked by ... on <host>" messages, can layer on top without changing
what actually arbitrates a lock conflict.

**Shared lock table, shared visibility scope, not shared with a third
host** (sprint.md Decision 2 / Decision 8): ``lock``/``unlock`` acquire
against the exact same :class:`~mbtools.registry.locks.LockManager`
instance ``daemon``/the local Unix-socket ``api`` already share — a
local and a remote request for the same uid now correctly conflict
through one table. ``list``/``find`` are scoped to
``store.snapshot_local_devices()`` (``host IS NULL``) — a remote client
resolving a name this registry doesn't own gets ``not_found``, exactly
as if the device didn't exist here; this API never forwards a request to
a third host, per Decision 8's "a remote client connects directly to the
owning host's registry."

**Lock release on connection close, and on a vanished session** (ticket
006 acceptance criteria #4/#5, mirroring — not duplicating —
``api.py``'s own "PID dies *or* the connection closes" pattern from
sprint.md's ASSUMPTION #3, generalized here to "the session's connection
closes *or* the session is found dead by the periodic sweep"): each
connection tracks the uids it personally acquired and releases exactly
those on close (clean or abrupt), same as ``api.py``. Independently, this
class tracks its own live-session-id set (``self._live_sessions``,
populated on accept, emptied on close) and runs its own periodic sweep
(mirroring ``api.py``'s ``_sweep_loop``) that releases any remote-origin
lock whose session id is no longer live — the mechanism a genuinely
network-partitioned client (no clean FIN/RST, so nothing here would
otherwise notice) still needs, one this module addresses two ways: the
sweep itself, and enabling ``SO_KEEPALIVE`` (see
:meth:`RemoteAPIServer._configure_keepalive`) on every accepted socket so
a half-dead connection's blocked read eventually errors out and joins the
normal close path, rather than blocking forever.

**Threading model**: identical shape to ``api.py`` — one OS thread per
accepted connection, plus one periodic sweep thread, both serialized
against ``store``/``locks`` by a single, optionally shared
``threading.RLock`` (the ``lock`` constructor parameter, for the same
``mbregistry run``-assembly reason ``api.RegistryAPIServer``'s own
docstring explains).

**Auth (sprint.md Decision 6)**: when ``auth_token`` is set (production:
``--auth-token``/``$MBREGISTRY_TOKEN``, ticket 009's CLI), a connection's
first non-empty line must be ``{"token": "<value>"}`` matching it, or the
connection is rejected (``CODE_UNAUTHORIZED``) before any op is
dispatched. When unset (the default), no auth check happens at all,
matching the local Unix socket's own trust model exactly.
"""

from __future__ import annotations

import json
import logging
import socket
import threading
from typing import Any
from uuid import uuid4

from mbtools.common import CODE_INVALID_REQUEST, CODE_UNAUTHORIZED
from mbtools.registry._api_base import BaseAPIServer, _error
from mbtools.registry.locks import HolderRef, LockManager
from mbtools.registry.store import DeviceRecord, Store

__all__ = [
    "RemoteAPIServer",
    "DEFAULT_REMOTE_PORT",
    "DEFAULT_SWEEP_INTERVAL_S",
]

logger = logging.getLogger(__name__)

#: Production default TCP port (sprint.md Decision 7), all configurable.
DEFAULT_REMOTE_PORT = 7440

#: Same default as api.py's own sweep -- see that module's constant.
DEFAULT_SWEEP_INTERVAL_S = 5.0

_ACCEPT_BACKLOG = 8

#: Best-effort TCP keepalive tuning (see _configure_keepalive) -- short
#: enough that a genuinely dead peer is noticed well within a human
#: session, not the multi-hour OS default.
_KEEPALIVE_IDLE_S = 30
_KEEPALIVE_INTERVAL_S = 10
_KEEPALIVE_PROBES = 3


class RemoteAPIServer(BaseAPIServer):
    """The TCP control-plane server -- see the module docstring.

    ``store``/``locks`` are injected references, the same instances a
    caller (ticket 009's ``mbregistry run`` assembly, or a test) already
    constructed for the daemon/local-api pipeline; this class never
    constructs its own, mirroring ``api.RegistryAPIServer``'s own
    "don't construct a second ``LockManager``" contract.

    ``host``/``port`` default to every interface (``0.0.0.0``) and
    sprint.md's fixed default port (:data:`DEFAULT_REMOTE_PORT`); a test
    passes ``port=0`` to let the OS choose an ephemeral port (see
    :attr:`bound_port`), avoiding collisions between parallel test runs.

    ``lock`` is the shared ``threading.RLock`` ticket 009's ``mbregistry
    run`` constructs once and passes to ``Daemon``, ``RegistryAPIServer``,
    and this class alike -- see ``api.RegistryAPIServer``'s own docstring
    for why. Defaults to a private ``RLock`` of this instance's own when
    omitted, matching every test in this module that constructs a server
    on its own.
    """

    def __init__(
        self,
        *,
        host: str = "0.0.0.0",
        port: int = DEFAULT_REMOTE_PORT,
        store: Store,
        locks: LockManager,
        auth_token: str | None = None,
        sweep_interval_s: float = DEFAULT_SWEEP_INTERVAL_S,
        lock: threading.RLock | None = None,
    ) -> None:
        self.host = host
        self.port = port
        self._store = store
        self._locks = locks
        self._auth_token = auth_token
        self._sweep_interval_s = sweep_interval_s

        self._lock = lock if lock is not None else threading.RLock()
        self._stop_event = threading.Event()
        self._sock: socket.socket | None = None
        self._accept_thread: threading.Thread | None = None
        self._sweep_thread: threading.Thread | None = None
        self._conn_threads: list[threading.Thread] = []

        # This server's own live-connection set (ticket 006 acceptance
        # criterion #5) -- guarded by its own lock, deliberately separate
        # from self._lock (which only ever guards store/locks access),
        # since membership bookkeeping here never touches either.
        self._sessions_lock = threading.Lock()
        self._live_sessions: set[str] = set()

    @property
    def bound_port(self) -> int:
        """The actual bound TCP port -- useful when constructed with
        ``port=0`` for a test to discover the real ephemeral port the OS
        chose."""
        assert self._sock is not None
        return self._sock.getsockname()[1]

    # -- lifecycle -----------------------------------------------------

    def start(self) -> None:
        """Bind the socket and start accepting connections plus the
        periodic sweep, both on background threads. Returns immediately.
        """
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._sock.bind((self.host, self.port))
        self._sock.listen(_ACCEPT_BACKLOG)
        self.port = self._sock.getsockname()[1]
        self._stop_event.clear()

        self._accept_thread = threading.Thread(target=self._accept_loop, daemon=True)
        self._accept_thread.start()
        self._sweep_thread = threading.Thread(target=self._sweep_loop, daemon=True)
        self._sweep_thread.start()

    def serve_forever(self) -> None:
        """Blocking convenience for a caller (ticket 009's ``mbregistry
        run``) that wants this call itself to be part of the daemon's
        main loop: :meth:`start`, then block until :meth:`stop` is
        called from another thread."""
        self.start()
        self._stop_event.wait()

    def stop(self) -> None:
        """Stop accepting new connections and the sweep timer, and join
        every connection-handler thread that is still alive, before
        returning -- mirrors ``api.RegistryAPIServer.stop``'s own reason
        (a caller that tears down ``store``/``locks`` immediately after
        ``stop()`` returns must not race a not-yet-finished handler
        thread)."""
        self._stop_event.set()
        if self._sock is not None:
            try:
                self._sock.close()
            except OSError:
                pass
        if self._accept_thread is not None:
            self._accept_thread.join(timeout=2.0)
        if self._sweep_thread is not None:
            self._sweep_thread.join(timeout=2.0)
        for thread in self._conn_threads:
            thread.join(timeout=2.0)

    def __enter__(self) -> "RemoteAPIServer":
        self.start()
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.stop()

    # -- accept / sweep loops -------------------------------------------

    def _accept_loop(self) -> None:
        assert self._sock is not None
        while not self._stop_event.is_set():
            try:
                conn, _addr = self._sock.accept()
            except OSError:
                break  # socket closed by stop()
            self._configure_keepalive(conn)
            thread = threading.Thread(
                target=self._handle_connection, args=(conn,), daemon=True
            )
            thread.start()
            self._conn_threads.append(thread)

    @staticmethod
    def _configure_keepalive(conn: socket.socket) -> None:
        """Best-effort ``SO_KEEPALIVE`` + a shortened probe interval, so
        a half-dead TCP session (peer vanished without a clean FIN/RST --
        a network partition, not a graceful disconnect) eventually errors
        out its blocked read instead of hanging forever, feeding the same
        connection-close release path a clean disconnect already uses
        (ticket 006 acceptance criterion #5's "consider TCP keepalive").

        Every knob beyond ``SO_KEEPALIVE`` itself is platform-specific
        and not exposed uniformly by Python's ``socket`` module (Linux:
        ``TCP_KEEPIDLE``/``TCP_KEEPINTVL``/``TCP_KEEPCNT``; macOS:
        ``TCP_KEEPALIVE`` for idle time only, no interval/count knobs) --
        each is looked up via ``getattr`` and applied only if present,
        wrapped in its own ``try/except OSError``, matching this
        project's existing "best-effort, platform-gated" precedent
        (``api.py``'s own ``SO_PEERCRED``/``LOCAL_PEERPID`` dispatch).
        A platform with none of these (or that refuses ``SO_KEEPALIVE``
        itself) simply relies on the periodic liveness sweep alone, same
        as before this method existed.
        """
        try:
            conn.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
        except OSError:
            return
        for opt_name, value in (
            ("TCP_KEEPIDLE", _KEEPALIVE_IDLE_S),
            ("TCP_KEEPINTVL", _KEEPALIVE_INTERVAL_S),
            ("TCP_KEEPCNT", _KEEPALIVE_PROBES),
            ("TCP_KEEPALIVE", _KEEPALIVE_IDLE_S),  # macOS's idle-time knob
        ):
            opt = getattr(socket, opt_name, None)
            if opt is None:
                continue
            try:
                conn.setsockopt(socket.IPPROTO_TCP, opt, value)
            except OSError:
                pass

    def _sweep_loop(self) -> None:
        """Mirrors ``api.RegistryAPIServer._sweep_loop`` -- an
        independent, periodic trigger for a session that vanished without
        an observable connection close (ticket 006 acceptance criterion
        #5)."""
        while not self._stop_event.wait(self._sweep_interval_s):
            with self._lock:
                released = self._locks.sweep(self._is_holder_alive)
            if released:
                logger.info("remote_api: liveness sweep released locks for %s", released)

    def _is_holder_alive(self, holder: HolderRef) -> bool:
        """The ``is_alive`` callable :meth:`LockManager.sweep` needs, from
        this server's own live-session-id set.

        Dispatches on ``holder.origin``: only ``"remote"`` is this
        server's concern -- a ``"local"`` holder is reported alive
        unconditionally, the exact symmetric mirror of
        ``api.RegistryAPIServer._is_holder_alive``'s own treatment of a
        ``"remote"`` holder (neither sweep thread second-guesses the
        other's origin; each is authoritative only over its own, even
        though both run against the one shared ``LockManager`` table --
        sprint.md Decision 2).
        """
        if holder.origin != "remote":
            return True
        with self._sessions_lock:
            return holder.ref in self._live_sessions

    # -- connection handling ---------------------------------------------

    def _holder_for_connection(self, conn: socket.socket) -> HolderRef:
        """The :class:`~mbtools.registry._api_base.BaseAPIServer` hook
        (ticket 006): mint a fresh session id and tag it with the
        connecting client's kernel-reported source IP -- see the module
        docstring's "Session identity, not PID identity" note."""
        host = conn.getpeername()[0]
        return HolderRef(origin="remote", ref=str(uuid4()), host=host)

    def _handle_connection(self, conn: socket.socket) -> None:
        try:
            holder = self._holder_for_connection(conn)
        except OSError:
            logger.exception(
                "remote_api: could not read peer address; dropping connection"
            )
            conn.close()
            return

        with self._sessions_lock:
            self._live_sessions.add(holder.ref)

        acquired_uids: set[str] = set()
        rfile = conn.makefile("r", encoding="utf-8", newline="\n")
        wfile = conn.makefile("w", encoding="utf-8", newline="\n")
        try:
            authenticated = self._auth_token is None
            for raw_line in rfile:
                line = raw_line.strip()
                if not line:
                    continue
                if not authenticated:
                    authenticated = self._check_auth(line, wfile)
                    if not authenticated:
                        break
                    continue
                self._dispatch_line(line, holder, acquired_uids, wfile)
        except (ConnectionError, OSError):
            pass
        finally:
            # Connection closed (cleanly or abruptly): release exactly
            # the locks this session acquired, never a blanket sweep --
            # mirrors api.py's own connection-close release.
            with self._lock:
                for uid in list(acquired_uids):
                    self._locks.release(uid, holder)
            with self._sessions_lock:
                self._live_sessions.discard(holder.ref)
            for f in (rfile, wfile):
                try:
                    f.close()
                except OSError:
                    pass
            try:
                conn.close()
            except OSError:
                pass

    def _check_auth(self, line: str, wfile: Any) -> bool:
        """The first non-empty line on a connection, when
        ``auth_token`` (sprint.md Decision 6) is configured: must be
        ``{"token": "<value>"}`` matching it, checked before any op is
        dispatched (ticket 006 acceptance criterion #7). A mismatch or
        malformed first message gets ``CODE_UNAUTHORIZED`` and the
        connection is torn down -- op dispatch is never reached.
        Success gets a plain ``{"ok": true}`` acknowledgement so the
        client knows to proceed with its first real op next.
        """
        try:
            req = json.loads(line)
        except json.JSONDecodeError:
            req = None
        token = req.get("token") if isinstance(req, dict) else None
        if token != self._auth_token:
            self._write(wfile, _error(CODE_UNAUTHORIZED, "invalid or missing auth token"))
            return False
        self._write(wfile, {"ok": True})
        return True

    def _dispatch_line(
        self, line: str, holder: HolderRef, acquired_uids: set[str], wfile: Any
    ) -> None:
        try:
            req = json.loads(line)
        except json.JSONDecodeError:
            self._write(wfile, _error(CODE_INVALID_REQUEST, "malformed JSON request"))
            return
        if not isinstance(req, dict):
            self._write(wfile, _error(CODE_INVALID_REQUEST, "request must be a JSON object"))
            return

        op = req.get("op")
        if op == "list":
            resp = self._op_list()
        elif op in ("get", "find"):
            resp = self._op_find(req)
        elif op == "lock":
            resp = self._op_lock(req, holder, acquired_uids)
        elif op == "unlock":
            resp = self._op_unlock(req, holder, acquired_uids)
        elif op == "mark_flashed":
            resp = self._op_mark_flashed(req, holder)
        else:
            # "flash"/"stream" (tickets 007/008) are not yet supported --
            # an unknown op here, same wire error a client gets for any
            # other typo, per the module docstring's own scope note.
            resp = _error(CODE_INVALID_REQUEST, f"unknown op {op!r}")
        self._write(wfile, resp)

    # -- visibility scope: this registry's own devices only ------------------

    def _list_visible_devices(self) -> list[DeviceRecord]:
        """Scope ``list`` to this registry's own devices (``host IS
        NULL``) -- ticket 006 acceptance criterion #3."""
        return self._store.snapshot_local_devices()

    def _device_visible(self, record: DeviceRecord) -> bool:
        """Scope ``find``/``lock``/``unlock``/``mark_flashed`` the same
        way :meth:`_list_visible_devices` scopes ``list`` -- a
        remote-owned row (this registry's cached view of some *other*
        peer's device) is ``not_found`` here, exactly as if it didn't
        exist (Decision 8: this API never forwards to a third host)."""
        return record.host is None
