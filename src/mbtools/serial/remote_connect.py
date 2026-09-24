"""mbtools.serial.remote_connect -- session logic for ``mbserial`` against
a peer-owned device (sprint 003, ticket 013, SUC-004).

Per sprint.md's Architecture (module ``serial.cli``/``serial.connect``,
Step 5/UC-011/SUC-004) and ticket 012's own local-vs-remote precedent
(``deploy.cli``), this module is ``serial.connect``'s remote-transport
counterpart: resolve ``target`` via an already-connected
``RemoteRegistryClient`` (owning-host-scoped, ticket 011), lock it
(``kind=serial``, fail fast per SUC-005 -- no retry), and switch that same
connection into the owning host's binary stream sub-protocol
(``RemoteRegistryClient.open_stream``, ticket 007) instead of opening a
local port. Returns a :class:`RemoteSession` -- a ``Session``-shaped
adapter over the resulting ``RemoteStream`` -- so ``serial.connect``'s own
``interact``/``send_command`` (imported and reused here, unmodified) run
identically against either transport: both are already duck-typed against
"anything with ``.read``/``.write``" per that module's own docstring, so
zero new logic is needed there (this ticket's own acceptance criterion).

**Same connection for lock and stream** (``registry.remote_client``'s own
"Session model" docstring): ``open_stream`` requires a ``serial``-kind
lock already held *by this same connection* -- so :func:`connect` below
never reconnects between ``lock()`` and ``open_stream()``. Once
``open_stream()`` succeeds, the ``RemoteRegistryClient`` instance that
produced it is inert (its socket has moved to the returned
``RemoteStream`` -- ticket 011's own "no way back to JSON framing" rule);
the lock this call took from then on is released only when the
stream/TCP session itself closes (a plain disconnect, or
``RemoteStream.close()``'s own ``CLOSE`` frame reaching
``RemoteAPIServer``'s connection-close release path) -- exactly
:meth:`RemoteSession.close`'s job. There is no explicit ``unlock`` call on
this path at all, unlike local ``serial.connect.Session.close()``.

**No reset by default** (SUC-004's "no reset unless asked" postcondition,
unchanged for the remote transport): ``RemoteAPIServer._handle_stream``
(ticket 007) already opens the owning host's port with DTR/RTS held low,
via the same ``serial.connect.open_no_reboot`` the local transport uses
-- this module sends nothing extra unless ``reset=True``.

**``--reset`` composes a BREAK, client-side** (this ticket's own
Description): sprint.md's "the owning host's platform decides which" is
not, in practice, something the wire protocol
(``stream_frame``'s five frame types --
``DATA``/``BREAK``/``SET_DTR``/``SET_RTS``/``CLOSE``) or
``RemoteAPIServer``'s own ``_handle_stream`` dispatch expose as a single
"reset, you pick how" primitive: ``FRAME_BREAK`` unconditionally triggers
``ser.send_break(...)`` server-side, with no platform branch of its own.
So ``connect(reset=True)`` sends a plain ``BREAK`` frame
(``RemoteStream.send_break()``) -- matching today's local Linux behavior
-- and leaves "does a plain BREAK actually reset the board regardless of
the owning host's OS" as a question for ticket 014's hardware pass, per
this ticket's own Description.
"""

from __future__ import annotations

import threading
import time

from mbtools.registry.client import RegistryClientError
from mbtools.registry.remote_client import RemoteRegistryClient, RemoteStream
from mbtools.serial.connect import (
    DEFAULT_TIMEOUT,
    IDLE_GAP,
    READ_TIMEOUT,
    RESET_SETTLE,
    interact,
    send_command,
)

__all__ = ["RemoteSession", "connect"]

#: Lock kind this module takes -- a plain wire-protocol string, not an
#: import from ``registry.locks``, mirroring ``serial.connect``'s own
#: ``_LOCK_KIND_SERIAL`` (see that module's docstring for why: it keeps
#: ``serial`` from ever importing ``locks``/``store`` directly).
_LOCK_KIND_SERIAL = "serial"


class RemoteSession:
    """A locked, open stream session on one peer-owned board.

    Wraps a :class:`~mbtools.registry.remote_client.RemoteStream` (ticket
    011) in the same six duck-typed members
    :class:`~mbtools.serial.connect.Session` exposes
    (``reset_input_buffer``/``write``/``flush``/``readline``/``read``/
    ``in_waiting``), so :func:`~mbtools.serial.connect.interact`/
    :func:`~mbtools.serial.connect.send_command` -- imported and used
    unchanged here -- work identically against either transport (module
    docstring).

    A background thread continuously pulls whole frames off the
    ``RemoteStream`` (``RemoteStream.read()`` blocks for exactly one
    frame, with no per-call timeout of its own) into an internal buffer;
    :meth:`read`/:meth:`readline` then wait on that buffer with a short,
    bounded timeout (:data:`~mbtools.serial.connect.READ_TIMEOUT`, the
    same budget a locally-opened pyserial port is itself configured with)
    rather than blocking on the socket directly -- this is what lets
    ``send_command``'s own overall-timeout/idle-gap polling loop keep
    terminating, exactly as it does against a real port whose
    ``readline()`` times out on the same cadence.

    :meth:`close` is idempotent and is the *only* place this session's
    ``serial``-kind lock is released -- not via an explicit ``unlock``
    call (the connection ``open_stream`` consumed has none left to make --
    module docstring), but simply by closing the stream:
    ``RemoteAPIServer`` releases a connection's locks the moment its TCP
    session ends, exactly like ``serial.connect.Session.close``'s own
    guarantee for the local lock.
    """

    def __init__(self, uid: str, name: str, stream: RemoteStream) -> None:
        self.uid = uid
        self.name = name
        self.stream = stream
        self._closed = False

        self._cond = threading.Condition()
        self._buf = bytearray()
        self._eof = False
        self._reader = threading.Thread(
            target=self._pump, name=f"mbserial-remote-read-{uid}", daemon=True
        )
        self._reader.start()

    # -- background frame pump ---------------------------------------------

    def _pump(self) -> None:
        """Continuously read whole frames off ``self.stream`` into
        ``self._buf``, decoupling the underlying blocking, per-frame
        ``RemoteStream.read()`` call from :meth:`read`/:meth:`readline`'s
        own bounded-wait polling (class docstring). Sets ``self._eof`` and
        returns on a ``CLOSE`` frame, a clean stream EOF (both are
        collapsed to ``None`` by ``RemoteStream.read()`` itself), or a
        transport failure (e.g. this session's own :meth:`close` closing
        the socket out from under a blocked read) -- any exception here
        means "the stream ended," never a reason to crash this thread.
        """
        while True:
            try:
                payload = self.stream.read()
            except Exception:
                payload = None
            with self._cond:
                if payload is None:
                    self._eof = True
                    self._cond.notify_all()
                    return
                if payload:
                    self._buf.extend(payload)
                    self._cond.notify_all()
                # An empty DATA frame is a valid wire no-op
                # (stream_frame's own format) -- nothing changed, keep
                # pumping without waking a waiter for no reason.

    # -- duck-typed pass-through (serial.connect.Session's own six) --------

    def reset_input_buffer(self) -> None:
        with self._cond:
            self._buf.clear()

    def write(self, data: bytes) -> int:
        self.stream.write(data)
        return len(data)

    def flush(self) -> None:
        pass  # RemoteStream.write() already sendall()s each frame in full

    def readline(self) -> bytes:
        deadline = time.monotonic() + READ_TIMEOUT
        with self._cond:
            while b"\n" not in self._buf and not self._eof:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                self._cond.wait(remaining)
            idx = self._buf.find(b"\n")
            if idx == -1:
                data = bytes(self._buf)
                del self._buf[:]
                return data
            data = bytes(self._buf[: idx + 1])
            del self._buf[: idx + 1]
            return data

    def read(self, size: int = 1) -> bytes:
        if size <= 0:
            return b""
        deadline = time.monotonic() + READ_TIMEOUT
        with self._cond:
            while len(self._buf) < size and not self._eof:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                self._cond.wait(remaining)
            n = min(size, len(self._buf))
            data = bytes(self._buf[:n])
            del self._buf[:n]
            return data

    @property
    def in_waiting(self) -> int:
        with self._cond:
            return len(self._buf)

    # -- convenience wrappers over serial.connect's own ported functions --

    def interact(self) -> int:
        return interact(self)

    def send_command(
        self, message: str, timeout: float = DEFAULT_TIMEOUT, idle_gap: float = IDLE_GAP
    ) -> list[str]:
        return send_command(self, message, timeout, idle_gap)

    # -- lifecycle -----------------------------------------------------

    def close(self) -> None:
        """Close the stream and release the ``serial``-kind lock, exactly
        once, however the session ends (class docstring). Safe to call
        more than once -- a caller doesn't need to track whether it
        already did.
        """
        if self._closed:
            return
        self._closed = True
        try:
            self.stream.close()
        except Exception:
            pass
        self._reader.join(timeout=1.0)

    def __enter__(self) -> "RemoteSession":
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()


def connect(
    client: RemoteRegistryClient,
    target: str,
    *,
    reset: bool = False,
    reset_settle_s: float | None = None,
) -> RemoteSession:
    """Resolve ``target`` against the owning host ``client`` is connected
    to, lock it (``kind=serial``), and switch that same connection into
    the binary stream sub-protocol -- module docstring.

    ``DeviceLockedError``/any other ``RegistryClientError``/
    ``RegistryUnavailable`` raised by ``find``/``lock`` propagate
    unchanged (SUC-005's fail-fast, no retry, no blocking wait -- the
    exact same contract ``serial.connect.connect`` gives the local
    transport). If ``open_stream`` itself fails once the lock is held (a
    ``DeviceNotFoundError``/``RegistryUnavailable`` mid-request), the lock
    is released on this same still-usable connection before the exception
    propagates -- never leaked, mirroring ``serial.connect.connect``'s own
    "unlock on any failure after locking" guarantee. Once ``open_stream``
    succeeds, this connection is spent (ticket 011) and
    :meth:`RemoteSession.close` is the only way to release the lock from
    here on.

    ``reset_settle_s`` is a test-only escape hatch (mirrors
    ``serial.connect.connect``'s own ``reset_settle_s``): production code
    leaves it at its default (:data:`~mbtools.serial.connect.RESET_SETTLE`).
    """
    reset_settle = RESET_SETTLE if reset_settle_s is None else reset_settle_s

    device = client.find(target)
    uid = device["uid"]
    name = device.get("device_name") or uid

    client.lock(uid, _LOCK_KIND_SERIAL)

    try:
        stream = client.open_stream(uid)
    except Exception:
        try:
            client.unlock(uid)
        except RegistryClientError:
            pass
        raise

    if reset:
        stream.send_break()
        if reset_settle:
            time.sleep(reset_settle)

    return RemoteSession(uid, name, stream)
