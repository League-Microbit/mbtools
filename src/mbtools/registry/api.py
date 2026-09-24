"""mbtools.registry.api — the local Unix-socket query/control protocol
server, and the ``SO_PEERCRED`` wiring that turns a connection into a PID.

Per sprint.md's Architecture (module "api"), this is the one place local
clients (ticket 009's ``mbregistry list``, sprint 002's ``mbdeploy``/
``mbserial``) reach ``store``, ``locks``, and ``flash`` — per spec §3.5,
"other programs consult it through a service," so no client tool reads
``store``'s SQLite file directly or re-implements lock bookkeeping. This
module has no business logic of its own beyond serialization and
dispatch: every real decision (what "already locked" means, what a
flash requires) is made by the module it delegates to.

**Wire protocol** (Open Questions #1 — an implementation choice, written
down here since it becomes sprint 002's de facto contract; see also
``docs/design/registry-api.md``): newline-delimited JSON over the Unix
socket, one connection per client session. Each request line is a JSON
object with an ``"op"`` field (``list``/``get``/``find``/``lock``/
``unlock``/``flash``/``mark_flashed``/``names_get``/``names_set``/
``names_clear``/``names_list`` -- the last four, sprint 004 ticket 005,
are ``BaseAPIServer``'s name-registry ops, not scoped to a device at all);
each non-streaming op writes exactly one JSON
response line. ``flash`` is the one streaming op: zero or more
``{"type": "log", "line": ...}`` lines (relayed from
:meth:`~mbtools.registry.flash.FlashOp.flash_hex`'s log callback as they
arrive, not buffered) followed by exactly one
``{"type": "result", "ok": ..., "success": ..., "exit_code": ...,
"error": ...}`` line. Every other response is either
``{"ok": true, ...op-specific fields...}`` or
``{"ok": false, "code": <one of mbtools.common's CODE_* constants>,
"error": <human message>}``.

**SO_PEERCRED wiring** (sprint.md's Architecture ASSUMPTION #3): on each
accepted connection, :func:`default_peer_pid` reads the connecting
process's real PID from the kernel — ``SO_PEERCRED`` on Linux,
``LOCAL_PEERPID`` on macOS (a working daemon on macOS is required per
this sprint's Open Question #3 treating "runs in the foreground for
local dev" as sufficient — braeburn, a macOS test host, is one of this
project's own hardware acceptance targets) — and that PID, never a
client-supplied one, is used for every ``lock``/``unlock``/``flash`` on
that connection. ``peer_pid_fn`` is injectable (mirrors every other
module's escape-hatch convention) so tests can exercise the rest of the
protocol without depending on real OS behavior; only the one test that
asserts the real ``SO_PEERCRED`` struct layout is Linux-only and skipped
(not xfailed) on macOS, per sprint.md's Test Strategy.

**Lock release on connection close** (the other half of ASSUMPTION #3,
"the lock releases when the PID dies *or* the connection closes"): each
connection tracks the uids it personally acquired and, when the
connection ends (cleanly or abruptly), calls
:meth:`~mbtools.registry.locks.LockManager.release` for exactly those
uids — never a blanket sweep, so another connection's locks are
untouched. The separate, PID-liveness half of that same ASSUMPTION
(a holder process dying without its connection closing) is not wired
anywhere else in this sprint (not ``daemon``, which only reads
``locks.status``) — this module wires
:meth:`~mbtools.registry.locks.LockManager.sweep` on a periodic
background timer as well, so both triggers are live.

**Threading model**: one OS thread per accepted connection (this sprint's
scale — "not a lot of devices," per the brief — does not need an event
loop), plus one periodic sweep thread. A single :class:`threading.RLock`
serializes every call into ``store``/``locks``/``flash`` so two
connections' check-then-act sequences (e.g. "does this uid exist, then
acquire its lock") can't interleave. ``mbtools.registry.store.Store``'s
sqlite3 connection is opened with ``check_same_thread=False`` (ticket
008's change, in ``store.py``) so it can be called from these
connection-handler threads at all; see that module's own comment for why
no further change was needed there.

**Shared lock across daemon and api (ticket 009's assembly)**: this
class's own ``RLock`` (above) and :class:`~mbtools.registry.daemon.Daemon`'s
each guarded only their own module's store/locks access until ticket 009
wired the two together — a documented gap (this module's own "Known
limitations" note in ``docs/design/registry-api.md``), since
``Daemon.run_once()``'s thread and this class's per-connection threads
touch the same ``Store``/``LockManager`` instances with nothing
serializing *across* the two. ``mbregistry run`` (ticket 009) closes that
gap by constructing one ``threading.RLock`` at assembly time and passing
it to both this class's ``lock`` parameter and ``Daemon``'s — see that
class's own "Concurrency" docstring note. A caller that constructs a bare
:class:`RegistryAPIServer` without ``lock=`` (every test in this module
that doesn't also construct a ``Daemon``) gets a private ``RLock`` of its
own, exactly as before.

**Flash does not hold the shared lock for the pyocd run**
(:meth:`RegistryAPIServer._op_flash`'s own note has the mechanics): the
shared lock only guards the short bookkeeping before and after —
resolving the record, checking this connection's own flash-kind lock is
held, and, afterwards, releasing that lock. The per-device flash-kind
lock (already held as a precondition, never released until the flash
attempt concludes) is what protects the device itself for the whole
streamed pyocd run; the shared lock is not needed for that, and holding
it there — this ticket's fix — is exactly what would have blocked
``list``/``get``/``lock`` for every other device once the lock became
shared with the daemon's own scan loop, which is worse than the
sprint-1 trade-off it replaces.
"""

from __future__ import annotations

import json
import logging
import os
import socket
import struct
import sys
import threading
from pathlib import Path
from typing import Any, Callable

from mbtools.common import CODE_INVALID_REQUEST, CODE_NOT_FOUND, CODE_NOT_LOCKED
from mbtools.registry._api_base import BaseAPIServer, _error
from mbtools.registry.flash import FlashOp, HexValidationError
from mbtools.registry.locks import KIND_FLASH, HolderRef, LockManager
from mbtools.registry.store import Entry, Store

__all__ = [
    "RegistryAPIServer",
    "default_peer_pid",
    "default_is_pid_alive",
    "DEFAULT_SOCKET_PATH",
    "DEFAULT_SWEEP_INTERVAL_S",
]

logger = logging.getLogger(__name__)

#: Production default socket path, under ``/run/mbregistry/`` per
#: sprint.md's Design Rationale "file layout (ASSUMPTION)" -- purely-live
#: state, cleared at boot. Every test overrides this to a ``tmp_path``.
DEFAULT_SOCKET_PATH = Path("/run/mbregistry/api.sock")

#: How often the background liveness sweep runs. Generous relative to a
#: human noticing a stuck lock, cheap enough to not matter at this
#: sprint's device-count scale.
DEFAULT_SWEEP_INTERVAL_S = 5.0

_ACCEPT_BACKLOG = 8


# ---------------------------------------------------------------------------
# SO_PEERCRED / LOCAL_PEERPID wiring
# ---------------------------------------------------------------------------


def _peer_pid_linux(sock: socket.socket) -> int:
    """Read the connecting process's PID via Linux's ``SO_PEERCRED``.

    ``SO_PEERCRED`` returns a ``struct ucred { pid_t pid; uid_t uid;
    gid_t gid; }`` -- three ``int``s on every Linux ABI this project
    targets. Kernel-verified, not client-suppliable (see the module
    docstring's "never a client-supplied PID").
    """
    creds = sock.getsockopt(
        socket.SOL_SOCKET, socket.SO_PEERCRED, struct.calcsize("3i")
    )
    pid, _uid, _gid = struct.unpack("3i", creds)
    return pid


# macOS's peer-credential getsockopt lives outside Python's socket module
# (no socket.SOL_LOCAL/LOCAL_PEERPID constants) -- these two values are
# stable, public constants from <sys/un.h> (confirmed against the macOS
# SDK: SOL_LOCAL=0, LOCAL_PEERPID=0x002), not a private/undocumented API.
_DARWIN_SOL_LOCAL = 0
_DARWIN_LOCAL_PEERPID = 0x002


def _peer_pid_darwin(sock: socket.socket) -> int:
    """Read the connecting process's PID via macOS's ``LOCAL_PEERPID``.

    braeburn (this project's macOS hardware-acceptance host, per
    ``CLAUDE.md``) needs a real daemon, not just "skip on macOS" --
    ``SO_PEERCRED`` is Linux-only, so macOS gets its own kernel-verified
    equivalent rather than falling back to a client-supplied (spoofable)
    PID.
    """
    raw = sock.getsockopt(
        _DARWIN_SOL_LOCAL, _DARWIN_LOCAL_PEERPID, struct.calcsize("i")
    )
    return struct.unpack("i", raw)[0]


def default_peer_pid(sock: socket.socket) -> int:
    """The production ``peer_pid_fn``: real kernel-verified PID lookup,
    dispatched by platform.

    Linux uses ``SO_PEERCRED``, macOS uses ``LOCAL_PEERPID`` (both
    kernel-verified, neither trusts a client-supplied value). Any other
    platform raises :class:`OSError` -- "fall back sensibly" per this
    ticket's own instructions means failing loudly here rather than
    silently trusting an unverifiable PID; this sprint targets Linux
    (production) and macOS (dev/test) only.
    """
    if sys.platform.startswith("linux"):
        return _peer_pid_linux(sock)
    if sys.platform == "darwin":
        return _peer_pid_darwin(sock)
    raise OSError(
        f"mbtools.registry.api: no peer-PID mechanism for platform {sys.platform!r}"
    )


def default_is_pid_alive(pid: int) -> bool:
    """The production ``is_pid_alive_fn``: ``os.kill(pid, 0)``-based
    liveness check, for :meth:`~mbtools.registry.locks.LockManager.sweep`.

    Mirrors ``tests/registry/locks/test_locks.py``'s own
    ``_real_is_pid_alive`` precedent exactly (a process that exists but
    isn't ours to signal is still alive; a vanished pid is not).
    """
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True  # exists, just isn't ours to signal
    except OSError:
        return False
    return True


def _local_holder(pid: int) -> HolderRef:
    """Build the :class:`~mbtools.registry.locks.HolderRef` for a local,
    Unix-socket connection's own pid (ticket 002, sprint.md Decision 2) --
    every ``lock``/``unlock``/``flash``/``mark_flashed`` op and the
    connection-close release path construct one of these from the
    ``SO_PEERCRED``/``LOCAL_PEERPID``-derived pid, never a client-supplied
    value. ``ref`` mirrors ``pid`` as a string so :meth:`LockManager.release`
    has a stable identity to match on for every origin, not just the ones
    with a real pid.
    """
    return HolderRef(origin="local", ref=str(pid), pid=pid)


class RegistryAPIServer(BaseAPIServer):
    """The Unix-socket query/control API server.

    ``list``/``find``/``lock``/``unlock``/``mark_flashed`` dispatch
    through :class:`~mbtools.registry._api_base.BaseAPIServer` (ticket
    006) -- this class supplies :meth:`_holder_for_connection` (wraps
    this connection's kernel-verified peer pid in a local
    :class:`~mbtools.registry.locks.HolderRef`) and leaves
    ``_list_visible_devices``/``_device_visible`` at their base
    defaults (every device, local or remote-owned, visible -- see that
    module's own docstring for why the local socket is not scoped the
    way ``remote_api`` is). ``flash`` (this class's own
    :meth:`_op_flash`, ticket 008's territory for the remote
    equivalent) and the sweep/threading/connection-close mechanics below
    are not shared -- they differ by transport in ways the shared base
    does not need to know about.

    ``store``/``locks``/``flash_op`` are injected references -- the same
    instances a caller (ticket 009's ``mbregistry run`` assembly, or a
    test) already constructed for the daemon pipeline; this class never
    constructs its own, mirroring the "don't construct a second
    LockManager" contract ``daemon``/``flash`` already established.

    ``peer_pid_fn``/``is_pid_alive_fn`` are the injectable escape hatches
    described in the module docstring; both default to the real,
    platform-appropriate production implementations.

    ``lock`` is the shared ``threading.RLock`` ticket 009's ``mbregistry
    run`` constructs once and passes to both this class and
    :class:`~mbtools.registry.daemon.Daemon` — see the module docstring's
    "Shared lock across daemon and api" note. Defaults to a private
    ``RLock`` of this instance's own when omitted, matching this class's
    pre-ticket-009 behavior for every test that constructs a server on
    its own.
    """

    def __init__(
        self,
        *,
        socket_path: str | Path,
        store: Store,
        locks: LockManager,
        flash_op: FlashOp,
        peer_pid_fn: Callable[[socket.socket], int] | None = None,
        is_pid_alive_fn: Callable[[int], bool] | None = None,
        sweep_interval_s: float = DEFAULT_SWEEP_INTERVAL_S,
        lock: threading.RLock | None = None,
        name_set_callback: Callable[[Entry], None] | None = None,
        name_clear_callback: Callable[[str], None] | None = None,
    ) -> None:
        self.socket_path = Path(socket_path)
        self._store = store
        self._locks = locks
        self._flash_op = flash_op
        self._peer_pid_fn = peer_pid_fn if peer_pid_fn is not None else default_peer_pid
        self._is_pid_alive_fn = (
            is_pid_alive_fn if is_pid_alive_fn is not None else default_is_pid_alive
        )
        self._sweep_interval_s = sweep_interval_s
        # sprint 004, ticket 005: fired by BaseAPIServer's
        # _op_names_set/_op_names_clear (see that module's own docstring)
        # -- registry.cli's assembly wires these to
        # PeerDiscovery.publish_name_set/publish_name_clear; None (the
        # default) leaves a bare RegistryAPIServer, and every pre-ticket-005
        # test that constructs one directly, unaffected.
        self._name_set_callback = name_set_callback
        self._name_clear_callback = name_clear_callback

        self._lock = lock if lock is not None else threading.RLock()
        self._stop_event = threading.Event()
        self._sock: socket.socket | None = None
        self._accept_thread: threading.Thread | None = None
        self._sweep_thread: threading.Thread | None = None
        self._conn_threads: list[threading.Thread] = []

    # -- lifecycle -----------------------------------------------------

    def start(self) -> None:
        """Bind the socket and start accepting connections plus the
        periodic sweep, both on background threads. Returns immediately.

        Explicitly ``chmod``s the socket to ``0o666`` after binding.
        Found on ticket 010's real-hardware pass: the production daemon
        (systemd's ``mbregistry.service``, no ``User=``, so it runs as
        root -- see ``cli.render_systemd_unit``) binds this socket under
        root's default umask, which produces mode ``0o755`` --
        readable/executable but not *writable* by anyone but the owner.
        Unix-domain ``connect()`` requires write permission on the
        socket inode, so every non-root invocation of ``mbregistry
        list`` (this module's own docstring: "other programs consult it
        through a service" -- with no mention that they must be root)
        failed with ``PermissionError: [Errno 13] Permission denied``
        before this fix, on every one of ticket 010's four Nolanet
        nodes. The API has no authentication beyond per-connection
        ``SO_PEERCRED`` pid tracking for lock ownership (ticket 008) --
        it was never designed to restrict which local users can
        connect, only which pid holds which lock -- so world-writable
        matches its actual security model rather than narrowing it.
        """
        self.socket_path.parent.mkdir(parents=True, exist_ok=True)
        if self.socket_path.exists():
            self.socket_path.unlink()
        self._sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self._sock.bind(str(self.socket_path))
        os.chmod(self.socket_path, 0o666)
        self._sock.listen(_ACCEPT_BACKLOG)
        self._stop_event.clear()

        self._accept_thread = threading.Thread(target=self._accept_loop, daemon=True)
        self._accept_thread.start()
        self._sweep_thread = threading.Thread(target=self._sweep_loop, daemon=True)
        self._sweep_thread.start()

    def serve_forever(self) -> None:
        """Blocking convenience for a caller (ticket 009's ``mbregistry
        run``) that wants this call itself to be the daemon's main loop:
        :meth:`start`, then block until :meth:`stop` is called from
        another thread.
        """
        self.start()
        self._stop_event.wait()

    def stop(self) -> None:
        """Stop accepting new connections and the sweep timer, and join
        every connection-handler thread that is still alive, before
        returning; remove the socket file.

        Already-open connections are not forcibly closed -- they wind
        down on their own next read/EOF, same as a client disconnecting
        (unchanged from before this join was added: a connection a
        client is deliberately keeping open across a ``stop()`` call is
        still left to wind down on its own, and its
        ``join(timeout=2.0)`` below simply times out without blocking
        shutdown). What changed is that a connection which *has* already
        seen EOF, or sees it within the timeout, is now waited for
        instead of left to finish on its own after this method returns.

        This join matters because every connection-handler thread
        (``_handle_connection``) calls back into ``store``/``locks``
        (``_op_list``, ``_op_find``, ...); a caller that tears down
        ``store`` (``store.close()``) immediately after ``stop()``
        returns -- every production and test caller does exactly this --
        would otherwise race a not-yet-finished handler thread against
        the now-closed sqlite connection. Found via a flaky
        ``IndexError`` in ``store._row_to_record`` traced to exactly this
        race: a connection-handler thread from a just-finished client
        request was still mid-``_op_list`` when the test's ``finally``
        block called ``store.close()`` right after ``stop()`` returned,
        because ``stop()`` joined the accept and sweep threads but never
        the per-connection ones ``_accept_loop`` spawns.
        """
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
        try:
            self.socket_path.unlink()
        except FileNotFoundError:
            pass
        except OSError:
            pass

    def __enter__(self) -> "RegistryAPIServer":
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
            thread = threading.Thread(
                target=self._handle_connection, args=(conn,), daemon=True
            )
            thread.start()
            self._conn_threads.append(thread)

    def _sweep_loop(self) -> None:
        """The periodic half of ASSUMPTION #3's "PID dies *or* the
        connection closes": catches a holder whose process died without
        its socket connection observably closing (e.g. a duplicated fd
        surviving a fork -- see sprint.md's own Design Rationale for why
        the two triggers are kept separate), on a timer independent of
        any one connection's lifecycle.
        """
        while not self._stop_event.wait(self._sweep_interval_s):
            with self._lock:
                released = self._locks.sweep(self._is_holder_alive)
            if released:
                logger.info("api: liveness sweep released locks for %s", released)

    def _is_holder_alive(self, holder: HolderRef) -> bool:
        """The ``is_alive`` callable :meth:`LockManager.sweep` needs,
        adapted from this server's own pid-based ``is_pid_alive_fn``
        (ticket 002, sprint.md Architecture point 4's "``api.py``" bullet).

        Dispatches on ``holder.origin``: every lock this server's own
        ``lock`` op grants is local (see :func:`_local_holder`), so only
        the ``"local"`` branch is exercised by locks *this* server
        acquired. A ``"remote"`` holder is reported alive unconditionally
        -- this sweep is not the one responsible for remote-session
        liveness (a remote holder's connection lives on
        ``registry.remote_api``, a different socket this class knows
        nothing about); ``remote_api.RemoteAPIServer`` runs its own,
        symmetric sweep (``_is_holder_alive`` there reports a ``"local"``
        holder alive unconditionally, for the same reason in reverse) --
        see that module's docstring. Since ``lock``/``unlock`` share one
        ``LockManager`` table (ticket 002, Decision 2), this server's
        sweep and ``remote_api``'s can both run against the same table
        without either one second-guessing the other's origin.
        """
        if holder.origin == "local":
            return self._is_pid_alive_fn(holder.pid)
        return True

    # -- connection handling ---------------------------------------------

    def _holder_for_connection(self, conn: socket.socket) -> HolderRef:
        """The :class:`~mbtools.registry._api_base.BaseAPIServer` hook
        (ticket 006): wraps this connection's kernel-verified peer pid
        (``SO_PEERCRED``/``LOCAL_PEERPID``, via ``self._peer_pid_fn``) in
        a local :class:`~mbtools.registry.locks.HolderRef`. Replaces the
        direct ``self._peer_pid_fn(conn)`` call that used to sit inline
        in :meth:`_handle_connection`, with the same ``OSError`` ->
        "drop the connection" handling left to that method's own
        try/except (mirroring how it always worked, just relocated).
        """
        pid = self._peer_pid_fn(conn)
        return _local_holder(pid)

    def _handle_connection(self, conn: socket.socket) -> None:
        try:
            holder = self._holder_for_connection(conn)
        except OSError:
            logger.exception("api: could not read peer pid; dropping connection")
            conn.close()
            return
        pid = holder.pid
        assert pid is not None  # every holder this class builds carries one

        acquired_uids: set[str] = set()
        rfile = conn.makefile("r", encoding="utf-8", newline="\n")
        wfile = conn.makefile("w", encoding="utf-8", newline="\n")
        try:
            for raw_line in rfile:
                line = raw_line.strip()
                if not line:
                    continue
                self._dispatch_line(line, pid, holder, acquired_uids, wfile)
        except (ConnectionError, OSError):
            pass
        finally:
            # Connection closed (cleanly or abruptly): release exactly
            # the locks this connection acquired, never a blanket sweep
            # -- see the module docstring.
            with self._lock:
                for uid in list(acquired_uids):
                    self._locks.release(uid, holder)
            for f in (rfile, wfile):
                try:
                    f.close()
                except OSError:
                    pass
            try:
                conn.close()
            except OSError:
                pass

    def _dispatch_line(
        self,
        line: str,
        pid: int,
        holder: HolderRef,
        acquired_uids: set[str],
        wfile: Any,
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
        elif op == "flash":
            resp = self._op_flash(req, pid, acquired_uids, wfile)
        elif op == "mark_flashed":
            resp = self._op_mark_flashed(req, holder)
        elif op == "names_get":
            resp = self._op_names_get(req)
        elif op == "names_set":
            resp = self._op_names_set(req)
        elif op == "names_clear":
            resp = self._op_names_clear(req)
        elif op == "names_list":
            resp = self._op_names_list(req)
        else:
            resp = _error(CODE_INVALID_REQUEST, f"unknown op {op!r}")
        self._write(wfile, resp)

    # -- ops --------------------------------------------------------------
    #
    # list/find/lock/unlock/mark_flashed are inherited from BaseAPIServer
    # (ticket 006) -- see this class's own docstring and that module's.
    # Only `flash` (ticket 008's remote-flash territory) stays here.

    def _op_flash(
        self,
        req: dict[str, Any],
        pid: int,
        acquired_uids: set[str],
        wfile: Any,
    ) -> dict[str, Any]:
        """``flash(uid, hex_path)``: requires a ``flash``-kind lock
        already held by this connection's own pid (consistent with
        ``flash.FlashOp.flash_hex``'s own lock-check, checked again here
        because the API additionally requires *this connection's* pid,
        not merely *some* holder). Streams
        :meth:`~mbtools.registry.flash.FlashOp.flash_hex`'s log lines to
        the client as they arrive, then -- because ``flash_hex`` never
        releases the lock itself (see that module's own docstring) --
        releases it here, unconditionally, once the attempt concludes
        (success, pyocd failure, or a validation error). That release is
        what ``daemon``'s flash-triggered re-probe hook is watching for.

        ``self._lock`` is held only for the short bookkeeping before and
        after the streamed pyocd run — resolving the record and checking
        this connection's own flash-kind lock is held, then, afterwards,
        releasing that lock — never for the run itself (ticket 009's
        fix; see the module docstring's "Flash does not hold the shared
        lock for the pyocd run" note). The per-device flash-kind lock,
        already held as a verified precondition and never released until
        the attempt concludes, is what protects ``uid`` for the whole
        run; the shared lock's job here is only the two short
        store/locks bookkeeping steps, not guarding the device itself.
        """
        def _flash_error(code: str, message: str) -> dict[str, Any]:
            # Every response to a "flash" request -- whether a
            # precondition failure before any log line was ever sent, or
            # the terminal message after streaming -- carries
            # "type": "result", so a client's dispatch loop for this one
            # op is always "type == 'log' -> print it; else -> done",
            # with no separate "did we ever start streaming" branch.
            payload = _error(code, message)
            payload["type"] = "result"
            payload["success"] = False
            payload["exit_code"] = None
            return payload

        token = req.get("uid")
        hex_path = req.get("hex_path")
        if not token or not hex_path:
            return _flash_error(CODE_INVALID_REQUEST, "'flash' requires 'uid' and 'hex_path'")

        def log(line: str) -> None:
            self._write(wfile, {"type": "log", "line": line})

        # Bookkeeping step 1 (short, held under the shared lock): resolve
        # the record and confirm this connection's own flash-kind lock is
        # held. Nothing here touches pyocd or the hex file.
        with self._lock:
            record = self._store.find(str(token))
            if record is None:
                return _flash_error(CODE_NOT_FOUND, f"no such device: {token!r}")
            uid = record.uid
            holder = self._locks.status(uid)
            if holder is None or holder.kind != KIND_FLASH or holder.pid != pid:
                return _flash_error(
                    CODE_NOT_LOCKED,
                    f"{uid}: flash requires a flash-kind lock held by this "
                    "connection (call 'lock' first)",
                )

        # The run itself: deliberately outside the shared lock (see the
        # module docstring and this method's own docstring). ``uid``'s
        # own flash-kind lock, verified above and not released until one
        # of the branches below runs, is what protects the device for
        # this whole streamed pyocd invocation.
        try:
            result = self._flash_op.flash_hex(uid, str(hex_path), log)
        except HexValidationError as exc:
            # Bookkeeping step 2 (short, held under the shared lock): the
            # flash never ran -- release the lock this connection is
            # still holding.
            with self._lock:
                self._locks.release(uid, _local_holder(pid))
                acquired_uids.discard(uid)
            return {
                "type": "result",
                "ok": False,
                "code": CODE_INVALID_REQUEST,
                "success": False,
                "exit_code": None,
                "error": str(exc),
            }

        # Bookkeeping step 2 (short, held under the shared lock): the
        # attempt concluded (success or pyocd failure) -- release the
        # lock. This release is what daemon's flash-triggered re-probe
        # hook is watching for.
        with self._lock:
            self._locks.release(uid, _local_holder(pid))
            acquired_uids.discard(uid)
        return {
            "type": "result",
            "ok": result.success,
            "success": result.success,
            "exit_code": result.exit_code,
            "error": result.error,
        }

    # `mark_flashed` is inherited from BaseAPIServer (ticket 006) -- see
    # this class's own docstring. Its precondition/semantics (a
    # flash-kind lock already held by *this connection's own* holder,
    # checked via `LockManager.status`, exactly as `_op_flash` above
    # checks) are unchanged from this method's pre-ticket-006 shape.
