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
extraction, and which extra ops they support. This module covers the
JSON-op sub-protocol, the framed binary stream sub-protocol the
``stream`` op switches a connection into (ticket 007 — see "The
``stream`` op and the binary sub-protocol" below), and, as of ticket 008,
a robustness-preserving remote ``flash`` op plus the ``send_hex`` staging
op it depends on (see "Remote flash and hex staging" below).

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

**The ``stream`` op and the binary sub-protocol (sprint 003, ticket
007)**: a connection that already holds a ``serial``-kind lock on ``uid``
(via this same connection's own ``lock`` call -- checked exactly the way
``api.RegistryAPIServer._op_flash``'s own "this connection's own holder"
precondition works) may send ``{"op": "stream", "uid": "..."}``. On
success, the connection permanently leaves newline-JSON framing and
switches into the length-prefixed binary frame format
:mod:`mbtools.registry.stream_frame` defines (sprint.md Decision 1) --
see :meth:`RemoteAPIServer._handle_stream`. The local port is opened with
DTR/RTS held low (no reboot), reusing
:func:`mbtools.serial.connect.open_no_reboot` rather than a second copy
of the same pyserial calls, and is never opened or read/written while
``self._lock`` (the shared store/locks lock) is held -- only the
``stream`` request's own precondition check (resolve the record, confirm
the lock) runs under that lock; the port I/O itself, and the frame
read/write loop, run after it has been released. ``CLOSE`` (or a plain
connection drop, or a malformed frame) ends the stream and this
connection together -- there is no "return to JSON mode."

**Protocol synchronization note**: the client must not send any stream
frame bytes before it has received the ``stream`` request's own
``{"ok": true}`` acknowledgement line. This server stops reading from the
connection's line-buffered JSON reader the moment it dispatches the
``stream`` op and switches to reading raw bytes directly off the socket;
any bytes the client sent *before* the ack but after the request line
would already be sitting in the JSON reader's own internal decode buffer
(a `io.TextIOWrapper`, which reads ahead of the line it returns) and
would not be seen by the socket-level reader that follows. A client that
already waits for each op's response before sending the next thing --
true of every op this protocol has -- satisfies this for free.

**The ``watch`` op (sprint 008, ticket 001)**: any connection may send
``{"op": "watch"}`` -- no lock or other precondition required. On
success (``{"ok": true}``), the connection leaves ordinary
request/response dispatch for the rest of its life, exactly like
``stream`` above, and instead receives one JSON line per event published
on :attr:`RemoteAPIServer._eventbus` (subscribed at that moment; no
snapshot of already-connected devices is sent -- see
``_api_base.BaseAPIServer._handle_watch``'s own docstring for the
change-only rationale) until the connection closes. This is the same
shared mechanism ``api.RegistryAPIServer``'s local socket uses -- see
that module's own docstring -- so a Node client (robot-console) gets
change notifications without linking ``pyzmq`` or knowing this
registry's PUB port, on either transport.

**Remote flash and hex staging (sprint 003, ticket 008)**: a connection
that already holds a ``flash``-kind lock on ``uid`` (via this same
connection's own ``lock`` call -- checked via :class:`HolderRef` equality,
the same "this connection's own holder" precondition
``api.RegistryAPIServer._op_flash`` checks for the local socket, just not
tied to a pid) may send ``{"op": "flash", "uid": "...", "hex_path":
"..."}``. Unlike the local Unix socket's ``flash`` op, a remote client has
no filesystem this process can read directly, so ``hex_path`` here is not
an arbitrary path -- it must be a path returned by this same connection's
own prior ``{"op": "send_hex", "data": "<base64>"}`` call
(:meth:`RemoteAPIServer._op_send_hex`), which decodes the given base64
payload (capped at :data:`MAX_HEX_PAYLOAD_BYTES`, checked cheaply against
the base64 text length before ever decoding, then again against the
decoded byte count) and writes it to a server-side temp file. A
``hex_path`` this connection did not itself stage is refused
(``invalid_request``) -- this is what stops a remote client from asking
this registry to flash (and report pyocd's interpretation of) an
arbitrary file already on this host.

``flash`` here calls :func:`mbtools.registry.flashlogic.flash_hex`
directly -- not the sprint-1 minimal :class:`~mbtools.registry.flash.
FlashOp` the local socket's own ``flash`` op uses -- so a remote flash
gets the exact same transient-retry/mass-erase/blank-board-reporting
behavior a local ``mbdeploy deploy`` gets (the same function, not a
parallel reimplementation that could drift). Its log lines stream to the
client exactly like the local socket's own ``flash`` op
(``{"type": "log", "line": ...}`` followed by one terminal
``{"type": "result", ...}``), and, like that op, the pyocd invocation
itself runs with the shared ``store``/``locks`` lock released -- only the
per-device ``flash``-kind lock, already held and verified before the run
starts, protects the device for its duration. On completion (success,
pyocd failure, or an unexpected exception) the lock is released
unconditionally -- the trigger `daemon`'s flash-triggered re-probe hook
is watching for -- and, on success only, ``store.increment_flash_count``
is called directly: this op both flashes and knows it happened in one
step, so unlike the local socket's two-call ``flash`` + ``mark_flashed``
pattern, no separate client-facing bookkeeping call is needed here. The
staged temp file is removed afterwards regardless of outcome, and any
file a connection staged but never flashed (e.g. it disconnected first)
is cleaned up when that connection closes.
"""

from __future__ import annotations

import base64
import binascii
import contextlib
import json
import logging
import os
import socket
import tempfile
import threading
from typing import Any, Callable
from uuid import uuid4

from mbtools.common import CODE_INVALID_REQUEST, CODE_NOT_LOCKED, CODE_UNAUTHORIZED
from mbtools.registry._api_base import _WATCH, BaseAPIServer, _error
from mbtools.registry.eventbus import EventBus
from mbtools.registry.flashlogic import flash_hex
from mbtools.registry.locks import KIND_FLASH, HolderRef, LockManager
from mbtools.registry.store import DeviceRecord, Store
from mbtools.serial.connect import BAUD_RATE, BREAK_DURATION, OPEN_SETTLE

__all__ = [
    "RemoteAPIServer",
    "DEFAULT_REMOTE_PORT",
    "DEFAULT_SWEEP_INTERVAL_S",
    "MAX_HEX_PAYLOAD_BYTES",
]

logger = logging.getLogger(__name__)

#: Production default TCP port (sprint.md Decision 7), all configurable.
DEFAULT_REMOTE_PORT = 7440

#: Same default as api.py's own sweep -- see that module's constant.
DEFAULT_SWEEP_INTERVAL_S = 5.0

_ACCEPT_BACKLOG = 8

#: A generous ceiling on one `send_hex` request's *decoded* byte count
#: (ticket 008 -- "cap payload size sensibly"): this project's boards
#: (nRF52833, 512 KiB flash) never produce an Intel HEX file within two
#: orders of magnitude of this, so a real client never comes close; a
#: malicious or buggy one can't make this process buffer or write an
#: unbounded amount of data to a temp file on the strength of one JSON
#: line. Checked against the base64 *text* length first (cheap, no
#: decode needed) and again against the decoded byte count (authoritative
#: -- base64 padding/alphabet tricks must not be able to slip a larger
#: payload past the first check).
MAX_HEX_PAYLOAD_BYTES = 8 * 1024 * 1024  # 8 MiB

#: The base64-text-length equivalent of MAX_HEX_PAYLOAD_BYTES (base64
#: expands 3 bytes into 4 characters), used for the cheap pre-decode
#: rejection above.
_MAX_HEX_B64_CHARS = 4 * ((MAX_HEX_PAYLOAD_BYTES + 2) // 3)

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

    ``serial_factory``/``stream_baud``/``stream_settle_s``/
    ``break_duration_s`` (ticket 007) parametrize how :meth:`_handle_stream`
    opens and resets the local port for a ``stream`` session -- test-only
    escape hatches mirroring ``serial.connect.connect``'s own
    ``serial_factory``/``settle_s`` seam (a test passes a callable
    returning ``mbtools.testing.fakes.FakeSerial`` and ``stream_settle_s=0``
    to skip the real-hardware settle delay). Production code leaves all
    four at their defaults: real pyserial, ``serial.connect.BAUD_RATE``,
    ``serial.connect.OPEN_SETTLE``, and ``serial.connect.BREAK_DURATION``.
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
        serial_factory: Callable[..., Any] | None = None,
        stream_baud: int = BAUD_RATE,
        stream_settle_s: float | None = None,
        break_duration_s: float = BREAK_DURATION,
        eventbus: EventBus | None = None,
    ) -> None:
        self.host = host
        self.port = port
        self._store = store
        self._locks = locks
        self._auth_token = auth_token
        self._sweep_interval_s = sweep_interval_s
        # Sprint 008 ticket 001: same "watch" event source as
        # api.RegistryAPIServer's own eventbus parameter -- see that
        # class's own docstring note for the default-when-omitted
        # convention this mirrors.
        self._eventbus = eventbus if eventbus is not None else EventBus()
        self._serial_factory = serial_factory
        self._stream_baud = stream_baud
        self._stream_settle = OPEN_SETTLE if stream_settle_s is None else stream_settle_s
        self._break_duration = break_duration_s

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
        # ticket 008: hex files this connection staged via its own
        # `send_hex` calls, keyed by the temp-file path handed back to
        # the client -- see `_op_send_hex`/`_op_flash`. A path is removed
        # from this set (and its file deleted) the moment a `flash`
        # request consumes it; anything left over when the connection
        # ends (staged but never flashed) is cleaned up below.
        uploaded_hex_paths: set[str] = set()
        rfile = conn.makefile("r", encoding="utf-8", newline="\n")
        wfile = conn.makefile("w", encoding="utf-8", newline="\n")
        try:
            authenticated = self._auth_token is None
            stream_record: DeviceRecord | None = None
            watch_requested = False
            for raw_line in rfile:
                line = raw_line.strip()
                if not line:
                    continue
                if not authenticated:
                    authenticated = self._check_auth(line, wfile)
                    if not authenticated:
                        break
                    continue
                result = self._dispatch_line(
                    line, holder, acquired_uids, uploaded_hex_paths, wfile
                )
                if result is _WATCH:
                    # sprint 008 ticket 001: "watch" was accepted -- same
                    # "leaves ordinary request/response dispatch for the
                    # rest of this connection's life" handoff "stream"
                    # already uses, just to a JSON-lines event feed
                    # instead of the binary frame sub-protocol.
                    watch_requested = True
                    break
                if result is not None:
                    stream_record = result
                    # ticket 007: "stream" was accepted -- the connection
                    # leaves newline-JSON framing for the rest of its
                    # life (module docstring's own note); nothing here
                    # goes back to reading JSON lines from `rfile` after
                    # this, ever, on any exit path.
                    break
            if watch_requested:
                self._handle_watch(wfile)
            elif stream_record is not None:
                self._handle_stream(conn, holder, acquired_uids, stream_record)
        except (ConnectionError, OSError):
            pass
        finally:
            # Connection closed (cleanly or abruptly), or a stream
            # session just concluded (_handle_stream already released
            # its own uid's lock on every one of its own exit paths, so
            # this is a no-op for that uid): release exactly the locks
            # this session acquired, never a blanket sweep --
            # mirrors api.py's own connection-close release.
            with self._lock:
                for uid in list(acquired_uids):
                    self._locks.release(uid, holder)
            # ticket 008: any hex file this connection staged but never
            # consumed via `flash` (e.g. it disconnected in between) --
            # never leave a temp file behind past this connection's life.
            for path in uploaded_hex_paths:
                with contextlib.suppress(OSError):
                    os.unlink(path)
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
        self,
        line: str,
        holder: HolderRef,
        acquired_uids: set[str],
        uploaded_hex_paths: set[str],
        wfile: Any,
    ) -> DeviceRecord | object | None:
        """Dispatch one JSON request line and write its response.

        Returns ``None`` for every ordinary op; a successfully-accepted
        ``stream`` (ticket 007) returns the resolved
        :class:`~mbtools.registry.store.DeviceRecord` to stream; a
        successfully-accepted ``watch`` (sprint 008 ticket 001) returns
        the module-level :data:`_WATCH` sentinel. Either non-``None``
        return tells the caller (:meth:`_handle_connection`) to stop
        reading further JSON lines and hand the connection to
        :meth:`_handle_stream`/:meth:`_handle_watch` respectively.
        """
        try:
            req = json.loads(line)
        except json.JSONDecodeError:
            self._write(wfile, _error(CODE_INVALID_REQUEST, "malformed JSON request"))
            return None
        if not isinstance(req, dict):
            self._write(wfile, _error(CODE_INVALID_REQUEST, "request must be a JSON object"))
            return None

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
        elif op == "send_hex":
            resp = self._op_send_hex(req, uploaded_hex_paths)
        elif op == "flash":
            resp = self._op_flash(req, holder, acquired_uids, uploaded_hex_paths, wfile)
        elif op == "stream":
            resp, record = self._op_stream_precheck(req, holder)
            self._write(wfile, resp)
            return record
        elif op == "watch":
            resp = self._op_watch()
            self._write(wfile, resp)
            return _WATCH
        else:
            resp = _error(CODE_INVALID_REQUEST, f"unknown op {op!r}")
        self._write(wfile, resp)
        return None

    # ``_op_stream_precheck`` is inherited from ``_api_base.BaseAPIServer``
    # (relocated there sprint 008 ticket 004 -- see this module's own
    # docstring, "The `stream` op and the binary sub-protocol" note, and
    # that module's docstring for the full relocation story). No behavior
    # change for an existing remote `stream` caller.

    # -- remote flash and hex staging (ticket 008) ------------------------

    def _op_send_hex(
        self, req: dict[str, Any], uploaded_hex_paths: set[str]
    ) -> dict[str, Any]:
        """``send_hex(data)``: stage a base64-encoded hex file's bytes
        into a server-side temp file, for a subsequent ``flash`` request
        *on this same connection* to reference via its own ``hex_path``
        (see :meth:`_op_flash` and the module docstring's "Remote flash
        and hex staging" note for why ``flash`` doesn't just take an
        arbitrary filesystem path here).

        ``uploaded_hex_paths`` is this connection's own bookkeeping set,
        owned by :meth:`_handle_connection` and mirroring
        ``acquired_uids`` -- a path is added here on success, removed by
        :meth:`_op_flash` once consumed, and cleaned up on connection
        close for anything staged but never flashed.

        The payload is capped at :data:`MAX_HEX_PAYLOAD_BYTES`: first
        cheaply, against the base64 *text* length (no decode needed), and
        again against the actual decoded byte count (authoritative --
        never trust the cheap check alone).
        """
        data = req.get("data")
        if not isinstance(data, str) or not data:
            return _error(CODE_INVALID_REQUEST, "'send_hex' requires 'data' (base64)")
        if len(data) > _MAX_HEX_B64_CHARS:
            return _error(
                CODE_INVALID_REQUEST,
                f"hex payload exceeds the {MAX_HEX_PAYLOAD_BYTES}-byte limit",
            )
        try:
            raw = base64.b64decode(data, validate=True)
        except (binascii.Error, ValueError) as exc:
            return _error(CODE_INVALID_REQUEST, f"'data' is not valid base64: {exc}")
        if len(raw) > MAX_HEX_PAYLOAD_BYTES:
            return _error(
                CODE_INVALID_REQUEST,
                f"hex payload exceeds the {MAX_HEX_PAYLOAD_BYTES}-byte limit",
            )

        fd, path = tempfile.mkstemp(prefix="mbregistry-remote-hex-", suffix=".hex")
        try:
            with os.fdopen(fd, "wb") as fh:
                fh.write(raw)
        except OSError as exc:
            with contextlib.suppress(OSError):
                os.unlink(path)
            return _error(CODE_INVALID_REQUEST, f"could not stage hex file: {exc}")

        uploaded_hex_paths.add(path)
        return {"ok": True, "hex_path": path}

    def _op_flash(
        self,
        req: dict[str, Any],
        holder: HolderRef,
        acquired_uids: set[str],
        uploaded_hex_paths: set[str],
        wfile: Any,
    ) -> dict[str, Any]:
        """``flash(uid, hex_path)``: requires a ``flash``-kind lock
        already held by *this connection's own* :class:`HolderRef` --
        the same "this connection's own holder, not merely some holder"
        precondition ``api.RegistryAPIServer._op_flash`` checks for the
        local socket, generalized here via ``HolderRef`` equality (ticket
        002) instead of a bare pid comparison. ``hex_path`` must be a
        path this same connection staged via its own prior ``send_hex``
        call (tracked in ``uploaded_hex_paths``) -- see the module
        docstring's "Remote flash and hex staging" note.

        Calls :func:`mbtools.registry.flashlogic.flash_hex` directly --
        not the sprint-1 minimal :class:`~mbtools.registry.flash.FlashOp`
        -- so this gets the exact same transient-retry/mass-erase/
        blank-board-reporting behavior a local ``mbdeploy deploy`` gets,
        streaming its log lines to ``wfile`` exactly like the local
        socket's own ``flash`` op. Deliberately runs the pyocd invocation
        itself outside ``self._lock`` (the shared store/locks lock) --
        only the short bookkeeping before and after (resolving the
        record and confirming the lock, then releasing it and, on
        success, incrementing ``flash_count``) happens under it; the
        already-verified per-device ``flash``-kind lock is what protects
        the device for the run itself.

        The lock is released unconditionally once the attempt concludes
        (success, pyocd failure, or an unexpected exception) -- this is
        what ``daemon``'s flash-triggered re-probe hook is watching for --
        and ``store.increment_flash_count(uid)`` is called directly on
        success only: this op both flashes and knows it happened in one
        step, so no separate client-facing ``mark_flashed`` call is
        needed on this path. The staged hex temp file is always removed
        afterwards, regardless of outcome.

        ``record.port`` (already resolved above, under the lock) is
        passed through as ``flash_hex``'s own ``port`` -- ticket 009's
        permission pre-check runs here, server-side, against the actual
        device this daemon owns, exactly like the local socket's flash
        path runs it against the client's own already-resolved
        ``device["port"]`` (``deploy.cli._flash``).
        """

        def _flash_error(code: str, message: str, **extra: Any) -> dict[str, Any]:
            # Every response to a "flash" request -- whether a
            # precondition failure before any log line was ever sent, or
            # the terminal message after streaming -- carries
            # "type": "result", mirroring api.RegistryAPIServer's own
            # `_op_flash` so a client's dispatch loop for this one op is
            # always "type == 'log' -> print it; else -> done".
            payload = _error(code, message, **extra)
            payload["type"] = "result"
            payload["success"] = False
            payload["exit_code"] = None
            return payload

        token = req.get("uid")
        hex_path = req.get("hex_path")
        if not token or not hex_path:
            return _flash_error(CODE_INVALID_REQUEST, "'flash' requires 'uid' and 'hex_path'")
        hex_path = str(hex_path)
        if hex_path not in uploaded_hex_paths:
            return _flash_error(
                CODE_INVALID_REQUEST,
                "'hex_path' must be a path staged by this connection's own "
                "'send_hex' call",
            )

        # Bookkeeping step 1 (short, held under the shared lock): resolve
        # the record (scoped to this registry's own devices, same as
        # every other op) and confirm this connection's own flash-kind
        # lock is held. Nothing here touches pyocd or the hex file.
        with self._lock:
            record, err = self._resolve_visible(str(token))
            if err is not None:
                return _flash_error(err["code"], err["error"], **{
                    k: v for k, v in err.items() if k not in ("ok", "code", "error")
                })
            assert record is not None
            uid = record.uid
            status = self._locks.status(uid)
            if status is None or status.kind != KIND_FLASH or status.holder != holder:
                return _flash_error(
                    CODE_NOT_LOCKED,
                    f"{uid}: flash requires a flash-kind lock held by this "
                    "connection (call 'lock' first)",
                )
            detached = self._detached_error(record)
            if detached is not None:
                return _flash_error(detached["code"], detached["error"])

        def log(line: str) -> None:
            self._write(wfile, {"type": "log", "line": line})

        # The run itself: deliberately outside the shared lock (see this
        # method's own docstring). `uid`'s own flash-kind lock, verified
        # above and not released until the `finally`/bookkeeping-step-2
        # below, is what protects the device for this whole streamed
        # pyocd invocation.
        board_name = record.device_name or uid
        try:
            rc = flash_hex(
                uid, hex_path, log=log, board_name=board_name, port=record.port
            )
            success = rc == 0
            exit_code: int | None = rc
            error: str | None = None if success else f"pyocd flash failed (exit {rc})"
        except Exception as exc:  # pragma: no cover - flash_hex doesn't raise in practice
            logger.exception("remote_api: flash_hex raised for %s", uid)
            success = False
            exit_code = None
            error = str(exc)
        finally:
            with contextlib.suppress(OSError):
                os.unlink(hex_path)
            uploaded_hex_paths.discard(hex_path)

        # Bookkeeping step 2 (short, held under the shared lock): the
        # attempt concluded -- release the lock (this is what daemon's
        # flash-triggered re-probe hook is watching for) and, on success
        # only, record that this uid was flashed.
        with self._lock:
            self._locks.release(uid, holder)
            acquired_uids.discard(uid)
            if success:
                self._store.increment_flash_count(uid)

        return {
            "type": "result",
            "ok": success,
            "success": success,
            "exit_code": exit_code,
            "error": error,
        }

    # ``_handle_stream`` is inherited from ``_api_base.BaseAPIServer``
    # (relocated there sprint 008 ticket 004, mechanics unchanged) -- see
    # that module's own docstring for the full relocation story.

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
