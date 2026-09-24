"""mbtools.registry.remote_client — typed TCP client library for
``registry.remote_api``'s wire protocol.

Per sprint.md's Architecture (module ``registry.remote_client``, Step 3's
module table: "Speak the remote TCP protocol from any host ... mirrors
``registry.client``'s shape so callers don't need two mental models"),
this is the TCP-protocol counterpart to
:class:`mbtools.registry.client.RegistryClient` — same method names,
same session model, and the *exact same* exception classes (imported from
:mod:`mbtools.registry.client`, never redefined here) so a caller's
``except DeviceLockedError`` works unmodified against either client. It
is the shared foundation ``deploy.cli``/``serial.cli`` (sprint 003
tickets 012/013) build their local-vs-remote branch on.

Like :mod:`mbtools.registry.client`, this is a pure protocol client: it
knows TCP connect, newline-delimited-JSON framing for the JSON-op
sub-protocol, base64 hex staging + streamed flash, and the binary framed
stream sub-protocol's client side (:class:`RemoteStream`). It does not
depend on :mod:`mbtools.registry.render` or any rendering/business logic
— same boundary ``registry.client`` already has.

**Session model** (mirrors ``registry.client``'s own, per
``docs/design/registry-api.md``): a :class:`RemoteRegistryClient` holds
one persistent TCP connection, opened lazily on first use (or explicitly
via :meth:`connect`, or a ``with`` block) and kept open until
:meth:`close`. A caller that wants a lock held across several requests
(lock → flash/stream → the server's own connection-close release) must
keep the same instance connected across all of them, exactly like
``RegistryClient``.

**Auth** (sprint.md Decision 6): when constructed with ``auth_token``
set, :meth:`connect` sends ``{"token": "<value>"}`` as the connection's
first message and raises the same typed exception
(:class:`~mbtools.registry.client.RegistryClientError`, ``code ==
"unauthorized"``) as any other protocol-level failure if it's rejected —
see ``docs/design/registry-api.md``'s "Auth" section under "Remote TCP
control plane".

**The ``stream`` op** (:meth:`open_stream`) permanently switches this
connection out of JSON framing, per the wire protocol's own "no way back"
rule — see :class:`RemoteStream`'s own docstring. Once returned, the
:class:`RemoteRegistryClient` instance that produced it no longer owns a
usable connection (its socket/file objects are handed to the returned
:class:`RemoteStream`); a caller wanting another JSON op needs a fresh
client instance.

**The ``flash`` op** (:meth:`flash`) is the first client-side streaming-
flash consumer in the codebase: it reads a local hex file, stages it via
``send_hex`` (base64), then sends ``flash`` with the staged path and
streams each ``{"type": "log"}`` line to the caller's ``log_callback`` as
it arrives, exactly mirroring the wire protocol's own "read lines; a
'type': 'log' line is progress; any other line is the final outcome"
dispatch loop. The terminal line is returned as a :class:`FlashResult` —
never raised — since a real pyocd outcome (success or failure) is a
normal result, not a protocol error; only a *precondition* failure
(``not_locked``, an unstaged ``hex_path``, ...) raises the usual typed
exception, since those responses carry a wire ``code`` the way every
other op's failure does.
"""

from __future__ import annotations

import base64
import json
import socket
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from mbtools.registry.client import (
    DeviceLockedError,
    DeviceNotFoundError,
    InvalidRequestError,
    RegistryClientError,
    RegistryUnavailable,
    _raise_for_error,
)
from mbtools.registry.remote_api import DEFAULT_REMOTE_PORT
from mbtools.registry.stream_frame import (
    FRAME_BREAK,
    FRAME_CLOSE,
    FRAME_DATA,
    FRAME_SET_DTR,
    FRAME_SET_RTS,
    FrameError,
    encode_frame,
    read_frame,
)

__all__ = [
    "RemoteRegistryClient",
    "RemoteStream",
    "FlashResult",
    "DEFAULT_REMOTE_PORT",
    # re-exported for callers that only import this module -- the exact
    # same classes registry.client raises, never redefined here (see the
    # module docstring).
    "RegistryUnavailable",
    "RegistryClientError",
    "InvalidRequestError",
    "DeviceNotFoundError",
    "DeviceLockedError",
]


@dataclass(frozen=True)
class FlashResult:
    """The terminal outcome of a :meth:`RemoteRegistryClient.flash` call,
    once every ``{"type": "log"}`` line has been streamed to the caller's
    ``log_callback`` — mirrors the wire protocol's own terminal
    ``{"type": "result", ...}`` line (``docs/design/registry-api.md``'s
    "Remote flash and hex staging"). ``exit_code``/``error`` mirror
    ``registry.flashlogic.flash_hex``'s own return shape once translated
    through the wire: ``exit_code`` is pyocd's own process exit code (or
    ``None`` if it never ran), ``error`` is ``None`` on success.
    """

    success: bool
    exit_code: int | None
    error: str | None


class RemoteStream:
    """Client side of the binary data+control sub-protocol a connection
    switches into after :meth:`RemoteRegistryClient.open_stream`'s
    ``stream`` request is acknowledged (sprint.md Decision 1,
    ``docs/design/registry-api.md``'s "Stream sub-protocol").

    Per the wire protocol's own rule, there is no way back to JSON
    framing on this connection — this class owns the raw socket for the
    rest of the session and is the only way to interact with it.
    :meth:`write`/:meth:`send_break`/:meth:`set_dtr`/:meth:`set_rts` send
    the corresponding frame type; :meth:`read` blocks for exactly one
    frame and returns its payload (``DATA``) or ``None`` (``CLOSE``, or a
    clean EOF at a frame boundary — the same two "stream ended" signals
    :func:`mbtools.registry.stream_frame.read_frame` itself distinguishes
    from a truncated/malformed frame, which raises
    :class:`~mbtools.registry.client.RegistryUnavailable` instead, since
    from this class's caller's point of view a corrupt frame and a
    dropped connection are both "the transport is gone", not a value to
    branch on).

    :meth:`close` sends a ``CLOSE`` frame (best-effort — a transport
    failure while sending it is not raised, since the intent is already
    "tear this down") and closes the socket; idempotent, like
    ``RegistryClient.close``.
    """

    def __init__(self, sock: socket.socket) -> None:
        self._sock = sock
        self._closed = False

    # -- outbound frames ---------------------------------------------------

    def write(self, data: bytes) -> None:
        """Send ``data`` as one ``DATA`` frame."""
        self._send_frame(FRAME_DATA, data)

    def send_break(self) -> None:
        """Send a ``BREAK`` frame -- triggers ``ser.send_break(...)`` on
        the owning host's local port."""
        self._send_frame(FRAME_BREAK)

    def set_dtr(self, value: bool) -> None:
        """Send a ``SET_DTR`` frame with ``value`` as its one-byte
        payload."""
        self._send_frame(FRAME_SET_DTR, bytes([1 if value else 0]))

    def set_rts(self, value: bool) -> None:
        """Send a ``SET_RTS`` frame with ``value`` as its one-byte
        payload."""
        self._send_frame(FRAME_SET_RTS, bytes([1 if value else 0]))

    # -- inbound frames ------------------------------------------------------

    def read(self) -> bytes | None:
        """Block for exactly one frame off the wire.

        Returns the payload of a ``DATA`` frame (raw bytes just read from
        the owning host's local port -- possibly empty, a valid no-op
        per the wire format). Returns ``None`` on a ``CLOSE`` frame or a
        clean EOF at a frame boundary -- either is "the stream ended",
        the same way ``read_frame`` itself collapses those two cases.
        """
        frame = self._read_frame()
        if frame is None:
            return None
        frame_type, payload = frame
        if frame_type == FRAME_CLOSE:
            return None
        return payload

    # -- lifecycle -----------------------------------------------------

    def close(self) -> None:
        """Send a ``CLOSE`` frame (best-effort) and close the underlying
        socket. Safe to call more than once."""
        if self._closed:
            return
        self._closed = True
        try:
            self._sock.sendall(encode_frame(FRAME_CLOSE))
        except OSError:
            pass
        try:
            self._sock.close()
        except OSError:
            pass

    def __enter__(self) -> "RemoteStream":
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    # -- wire mechanics ----------------------------------------------------

    def _send_frame(self, frame_type: int, payload: bytes = b"") -> None:
        try:
            self._sock.sendall(encode_frame(frame_type, payload))
        except OSError as exc:
            raise RegistryUnavailable(f"remote stream unavailable: {exc}") from exc

    def _read_frame(self) -> tuple[int, bytes] | None:
        def _read_exact(n: int) -> bytes:
            chunks: list[bytes] = []
            remaining = n
            while remaining > 0:
                try:
                    chunk = self._sock.recv(remaining)
                except OSError as exc:
                    raise RegistryUnavailable(
                        f"remote stream unavailable: {exc}"
                    ) from exc
                if not chunk:
                    break
                chunks.append(chunk)
                remaining -= len(chunk)
            return b"".join(chunks)

        try:
            return read_frame(_read_exact)
        except FrameError as exc:
            raise RegistryUnavailable(f"remote stream framing error: {exc}") from exc


class RemoteRegistryClient:
    """A typed client for ``registry.remote_api``'s TCP wire protocol —
    see the module docstring.

    ``host``/``port`` name the owning registry's ``remote_api`` listener
    (``device["endpoint"]``, once ``registry.peering``/``render`` surface
    it, splits into these two). ``auth_token`` mirrors
    ``--auth-token``/``$MBREGISTRY_TOKEN`` (sprint.md Decision 6) — set it
    to match the owning registry's configured token, or leave it ``None``
    (the default) for an unauthenticated registry.
    """

    def __init__(
        self,
        host: str,
        port: int = DEFAULT_REMOTE_PORT,
        *,
        auth_token: str | None = None,
    ) -> None:
        self.host = host
        self.port = port
        self._auth_token = auth_token
        self._sock: socket.socket | None = None
        self._rfile: Any = None
        self._wfile: Any = None

    # -- connection lifecycle --------------------------------------------

    def connect(self) -> None:
        """Open the underlying TCP connection, if not already open, and
        complete the auth handshake if ``auth_token`` was given.

        Raises :class:`~mbtools.registry.client.RegistryUnavailable` if
        the connection cannot be established (or drops mid-handshake), or
        the usual typed :class:`~mbtools.registry.client.RegistryClientError`
        (``code == "unauthorized"``) if an auth token was sent and
        rejected.
        """
        if self._sock is not None:
            return
        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            sock.connect((self.host, self.port))
        except OSError as exc:
            raise RegistryUnavailable(
                f"registry unavailable at {self.host}:{self.port}: {exc}"
            ) from exc
        self._sock = sock
        self._rfile = sock.makefile("r", encoding="utf-8", newline="\n")
        self._wfile = sock.makefile("w", encoding="utf-8", newline="\n")

        if self._auth_token is not None:
            try:
                resp = self._raw_request({"token": self._auth_token})
            except RegistryUnavailable:
                self.close()
                raise
            if not resp.get("ok"):
                self.close()
                _raise_for_error(resp)

    def close(self) -> None:
        """Close the underlying connection, if open. Idempotent -- safe
        to call more than once, and safe to call on a never-connected
        instance, or after :meth:`open_stream` has handed the connection
        off to a :class:`RemoteStream`.
        """
        for f in (self._rfile, self._wfile):
            if f is not None:
                try:
                    f.close()
                except OSError:
                    pass
        if self._sock is not None:
            try:
                self._sock.close()
            except OSError:
                pass
        self._sock = None
        self._rfile = None
        self._wfile = None

    def __enter__(self) -> "RemoteRegistryClient":
        self.connect()
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    # -- wire protocol -----------------------------------------------------

    def _raw_request(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Send one newline-delimited-JSON request and read its one
        response line, without checking ``ok`` -- the caller decides
        whether/how to raise. Requires the connection to already be open
        (never calls :meth:`connect` itself, so it's also usable for the
        auth handshake mid-:meth:`connect`, before this instance would
        otherwise consider itself connected).
        """
        assert self._wfile is not None and self._rfile is not None
        try:
            self._wfile.write(json.dumps(payload))
            self._wfile.write("\n")
            self._wfile.flush()
            line = self._rfile.readline()
        except OSError as exc:
            raise RegistryUnavailable(f"registry unavailable: {exc}") from exc
        if not line:
            raise RegistryUnavailable("connection closed by mbregistry remote_api")
        try:
            resp: dict[str, Any] = json.loads(line)
        except json.JSONDecodeError as exc:
            raise RegistryUnavailable(f"registry unavailable: {exc}") from exc
        return resp

    def _request(self, payload: dict[str, Any]) -> dict[str, Any]:
        """:meth:`connect`, then :meth:`_raw_request`, raising the
        appropriate typed exception for an ``{"ok": false, ...}``
        response -- the non-streaming counterpart of
        ``RegistryClient._request``."""
        self.connect()
        resp = self._raw_request(payload)
        if not resp.get("ok"):
            _raise_for_error(resp)
        return resp

    def _stream_request(
        self, payload: dict[str, Any], log_callback: Callable[[str], None] | None
    ) -> dict[str, Any]:
        """Like :meth:`_request`, but for an op (only ``flash``, so far)
        whose response is zero or more ``{"type": "log", ...}`` lines
        followed by exactly one non-``"log"`` terminal line -- the
        dispatch loop ``docs/design/registry-api.md``'s "flash" section
        describes. Each log line is handed to ``log_callback`` (if
        given); the terminal line is returned as-is, unchecked -- see
        :meth:`flash` for why a real flash outcome is never raised here.
        """
        self.connect()
        assert self._wfile is not None and self._rfile is not None
        try:
            self._wfile.write(json.dumps(payload))
            self._wfile.write("\n")
            self._wfile.flush()
            while True:
                line = self._rfile.readline()
                if not line:
                    raise RegistryUnavailable(
                        "connection closed by mbregistry remote_api"
                    )
                resp: dict[str, Any] = json.loads(line)
                if resp.get("type") == "log":
                    if log_callback is not None:
                        log_callback(str(resp.get("line", "")))
                    continue
                return resp
        except OSError as exc:
            raise RegistryUnavailable(f"registry unavailable: {exc}") from exc
        except json.JSONDecodeError as exc:
            raise RegistryUnavailable(f"registry unavailable: {exc}") from exc

    # -- typed ops -----------------------------------------------------

    def find(self, uid: str) -> dict[str, Any]:
        """One device by uid, short-uid, or device_name token, scoped to
        the owning registry's own devices (``docs/design/registry-
        api.md``'s "Scope: this registry's own devices only"). Raises
        :class:`~mbtools.registry.client.DeviceNotFoundError` if nothing
        matches.
        """
        resp = self._request({"op": "find", "uid": uid})
        return dict(resp["device"])

    def lock(self, uid: str, kind: str) -> None:
        """Acquire a ``kind``-kind lock on ``uid`` for this connection's
        session id. Raises
        :class:`~mbtools.registry.client.DeviceNotFoundError` if ``uid``
        doesn't resolve, or
        :class:`~mbtools.registry.client.DeviceLockedError` (with
        ``holder`` set, ``origin: "remote"`` per
        ``docs/design/registry-api.md``'s "Lock holder wire shape") if
        someone else already holds it. Held until :meth:`unlock` or
        :meth:`close` -- see the module docstring's "Session model".
        """
        self._request({"op": "lock", "uid": uid, "kind": kind})

    def unlock(self, uid: str) -> bool:
        """Release this connection's lock on ``uid``, if any.

        Returns whether a lock was actually released. Raises
        :class:`~mbtools.registry.client.DeviceNotFoundError` if ``uid``
        doesn't resolve.
        """
        resp = self._request({"op": "unlock", "uid": uid})
        return bool(resp.get("released", False))

    def flash(
        self,
        uid: str,
        hex_path: str | Path,
        log_callback: Callable[[str], None] | None = None,
    ) -> FlashResult:
        """Flash the local file at ``hex_path`` to ``uid`` via the
        remote flash op: reads and base64-stages it (``send_hex``), then
        runs it server-side (``flash``) with the exact same transient-
        retry/mass-erase robustness a local flash gets
        (``docs/design/registry-api.md``'s "Remote flash and hex
        staging") -- streaming each progress line to ``log_callback`` as
        it arrives.

        Requires a ``flash``-kind lock already held by this connection
        (call :meth:`lock` first) -- the same precondition the wire op
        itself enforces. A precondition failure (no lock held, or a
        ``hex_path`` staging problem) raises the usual typed exception,
        since that response carries a wire ``code``; an actual pyocd
        outcome -- success or failure -- is returned as a
        :class:`FlashResult`, never raised, mirroring
        ``registry.flashlogic.flash_hex``'s own "reports, doesn't raise,
        for a real flash attempt" contract.
        """
        data = Path(hex_path).read_bytes()
        encoded = base64.b64encode(data).decode("ascii")
        staged = self._request({"op": "send_hex", "data": encoded})
        staged_path = staged["hex_path"]

        resp = self._stream_request(
            {"op": "flash", "uid": uid, "hex_path": staged_path}, log_callback
        )
        if not resp.get("ok") and resp.get("code"):
            # A precondition failure (invalid_request/not_locked) -- the
            # one case a "flash" response carries a wire code, per
            # remote_api._op_flash's own _flash_error helper.
            _raise_for_error(resp)
        return FlashResult(
            success=bool(resp.get("success")),
            exit_code=resp.get("exit_code"),
            error=resp.get("error"),
        )

    def open_stream(self, uid: str) -> RemoteStream:
        """Switch this connection into the binary data+control
        sub-protocol for ``uid`` (``docs/design/registry-api.md``'s
        "Stream sub-protocol") and return a :class:`RemoteStream` for it.

        Requires a ``serial``-kind lock already held by this connection
        (call :meth:`lock` first) -- raises
        :class:`~mbtools.registry.client.RegistryClientError`
        (``code == "not_locked"``) otherwise, or
        :class:`~mbtools.registry.client.DeviceNotFoundError` if ``uid``
        doesn't resolve. On success, this connection's JSON-framed
        session ends permanently (the wire protocol's own "no way back"
        rule) -- this instance no longer owns a usable connection after
        this call returns; a further JSON op needs a fresh
        :class:`RemoteRegistryClient`.
        """
        self._request({"op": "stream", "uid": uid})
        assert self._sock is not None
        sock = self._sock
        # Ownership of the raw socket moves to the RemoteStream -- this
        # instance's own connection is now spent (no way back to JSON
        # framing on it), so close()/further ops on this instance become
        # inert rather than reaching into a connection RemoteStream now
        # owns. Closing the *file* wrappers here (not the socket -- that
        # would tear down the fd RemoteStream is about to take over) just
        # releases their own buffers; a socket's fd is only actually
        # closed once every makefile()-issued wrapper *and* the socket
        # object itself have been closed (Python's own socket.makefile()
        # contract), so this is safe and leaves the fd open for
        # RemoteStream.
        for f in (self._rfile, self._wfile):
            if f is not None:
                try:
                    f.close()
                except OSError:
                    pass
        self._sock = None
        self._rfile = None
        self._wfile = None
        return RemoteStream(sock)
