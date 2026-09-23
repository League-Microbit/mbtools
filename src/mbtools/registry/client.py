"""mbtools.registry.client — typed local-socket client library for the
registry's wire protocol.

Per sprint.md's Architecture (module ``registry.client``), this is the one
place any local process learns to speak the registry's Unix-socket API:
socket connect, newline-delimited-JSON framing, the ``list``/``find``/
``lock``/``unlock`` request shapes, and translating a protocol-level
``{"ok": false, "code": ...}`` response into a typed exception a caller can
catch without re-parsing strings. It knows nothing about how a caller
renders or acts on what it gets back — no argparse, no table rendering, no
business logic beyond the protocol itself.

Extracted from ``mbtools.registry.cli``'s private ``_connect``/``_request``
helpers (sprint 001, ticket 009) rather than written fresh, so
``mbregistry list`` becomes this module's first caller rather than a
parallel implementation — see that module's refactored ``cmd_list``.
Every other client-tool ticket this sprint (``mbdeploy``, ``mbserial``)
imports this module rather than talking sockets directly.

**Session model** (``docs/design/registry-api.md``: "One socket connection
is one client session. A client may send any number of requests over
it."): a :class:`RegistryClient` holds one persistent connection, opened
lazily on first use (or explicitly via :meth:`RegistryClient.connect`, or
a ``with`` block) and kept open until :meth:`RegistryClient.close`. This
matters beyond convenience — the server releases every lock a connection
acquired when that connection closes (``api.py``'s own "Lock release on
connection close" note), so a caller that wants a lock held across several
requests (e.g. ``lock`` → flash → ``unlock``, sprint 002's ``mbdeploy``)
must keep the same :class:`RegistryClient` instance connected across all
of them, not reconnect per call.

**Exceptions**: :class:`RegistryUnavailable` (socket file absent, refused,
or the connection drops mid-session) is distinct from every protocol-level
error — it means "there is no daemon to talk to," not "the daemon said
no." Protocol-level failures (``{"ok": false, "code": ...}``) raise
:class:`RegistryClientError` or one of its subclasses, each carrying the
raw ``code``/``message`` plus a stable ``exit_code`` attribute
(:mod:`mbtools.common`'s ``EXIT_*`` constants) so a CLI can do
``except RegistryClientError as exc: return exc.exit_code`` without
switching on ``code`` itself. :class:`DeviceLockedError` additionally
carries ``holder`` (``{"kind": ..., "pid": ...}``, per the wire protocol's
own ``locked`` response shape) so a caller can format UC-006's "locked for
``<kind>`` by pid ``<pid>``" without a second lookup.
"""

from __future__ import annotations

import json
import os
import socket
from pathlib import Path
from typing import Any

from mbtools.common import (
    CODE_INVALID_REQUEST,
    CODE_LOCKED,
    CODE_NOT_FOUND,
    EXIT_ERROR,
    EXIT_LOCKED,
    EXIT_NO_DEVICE,
    EXIT_USAGE,
)
from mbtools.registry.api import DEFAULT_SOCKET_PATH

__all__ = [
    "RegistryClient",
    "RegistryUnavailable",
    "RegistryClientError",
    "InvalidRequestError",
    "DeviceNotFoundError",
    "DeviceLockedError",
    "SOCKET_ENV_VAR",
    "DEFAULT_SOCKET_PATH",
    "resolve_socket_path",
]

#: Mirrors ``registry.cli``'s pre-extraction ``_SOCKET_ENV_VAR`` exactly —
#: same name, same precedence (flag > env var > default) — so no caller's
#: invocation changes (this ticket's own acceptance criterion).
SOCKET_ENV_VAR = "MBREGISTRY_SOCKET"


def resolve_socket_path(
    flag_value: str | None,
    env_var: str = SOCKET_ENV_VAR,
    default: str | Path = DEFAULT_SOCKET_PATH,
) -> Path:
    """``flag_value`` wins if given; else ``$env_var`` if set; else
    ``default``. The one place socket-path precedence is decided, shared
    by every caller (``registry.cli``'s ``list``/``run``, and sprint
    002's ``mbdeploy``/``mbserial``) so it can't drift between them.
    """
    if flag_value:
        return Path(flag_value)
    env_value = os.environ.get(env_var)
    if env_value:
        return Path(env_value)
    return Path(default)


# ---------------------------------------------------------------------------
# exceptions
# ---------------------------------------------------------------------------


class RegistryUnavailable(Exception):
    """The registry's api socket could not be reached at all -- socket
    file absent, connection refused, or the connection dropped mid-
    session. Distinct from every protocol-level error below: this means
    there is no daemon to talk to (UC-004's error flow, ``EXIT_NO_DAEMON``),
    not that the daemon answered with a failure.
    """


class RegistryClientError(Exception):
    """Base for a protocol-level ``{"ok": false, "code": ..., "error":
    ...}`` response. Carries the raw ``code``/``message`` plus a stable
    ``exit_code`` (an :mod:`mbtools.common` ``EXIT_*`` constant) so a
    caller can translate a caught exception to a process exit code without
    re-parsing ``code`` itself. Any ``code`` this module doesn't map to a
    more specific subclass below (``not_locked``, ``internal_error``, an
    op-specific code from a future op) raises this base class directly,
    with ``exit_code`` defaulting to ``EXIT_ERROR`` -- the same generic
    fallback ``registry.cli``'s pre-extraction code used for an
    unrecognized failure.
    """

    exit_code = EXIT_ERROR

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        self.message = message
        super().__init__(message)


class InvalidRequestError(RegistryClientError):
    """``code == "invalid_request"`` -- malformed request, unknown op, or
    (per sprint.md's Deployment-sequencing risk note) an op an older
    daemon doesn't recognize yet.
    """

    exit_code = EXIT_USAGE


class DeviceNotFoundError(RegistryClientError):
    """``code == "not_found"`` -- no device matches the given uid/token."""

    exit_code = EXIT_NO_DEVICE


class DeviceLockedError(RegistryClientError):
    """``code == "locked"`` -- a ``lock`` request lost to an existing
    holder. ``holder`` (``{"kind": ..., "pid": ...}``) is preserved from
    the response, never discarded, so a caller can format "locked for
    ``<kind>`` by pid ``<pid>``" (UC-006's error flow / SUC-005) without a
    second round-trip.
    """

    exit_code = EXIT_LOCKED

    def __init__(self, code: str, message: str, holder: dict[str, Any] | None) -> None:
        super().__init__(code, message)
        self.holder = holder


_ERROR_CLASSES: dict[str, type[RegistryClientError]] = {
    CODE_NOT_FOUND: DeviceNotFoundError,
    CODE_INVALID_REQUEST: InvalidRequestError,
    # CODE_LOCKED is handled separately below -- it needs `holder`, which
    # no other error code carries.
}


def _raise_for_error(resp: dict[str, Any]) -> None:
    code = resp.get("code") or ""
    message = resp.get("error") or "unknown error"
    if code == CODE_LOCKED:
        raise DeviceLockedError(code, message, resp.get("holder"))
    error_cls = _ERROR_CLASSES.get(code, RegistryClientError)
    raise error_cls(code, message)


# ---------------------------------------------------------------------------
# client
# ---------------------------------------------------------------------------


class RegistryClient:
    """A typed client for the registry's Unix-socket wire protocol.

    One instance is one client session (see the module docstring): the
    underlying connection is opened lazily on first request (or eagerly
    via :meth:`connect`/``with``) and stays open across every call until
    :meth:`close`. Every method returns a plain Python value (a list of
    dicts, or a dict) on success and raises a typed exception on failure
    -- no raw socket or JSON leaks past this class's boundary.
    """

    def __init__(self, socket_path: str | Path) -> None:
        self.socket_path = Path(socket_path)
        self._sock: socket.socket | None = None
        self._rfile: Any = None
        self._wfile: Any = None

    # -- connection lifecycle --------------------------------------------

    def connect(self) -> None:
        """Open the underlying socket connection, if not already open.
        Raises :class:`RegistryUnavailable` if the socket file is absent
        or the daemon refuses the connection -- mirrors
        ``registry.cli``'s pre-extraction ``_connect``/``cmd_list``
        behavior exactly (same ``OSError`` catch, same ``EXIT_NO_DAEMON``
        trigger one level up).
        """
        if self._sock is not None:
            return
        try:
            sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            sock.connect(str(self.socket_path))
        except OSError as exc:
            raise RegistryUnavailable(
                f"registry unavailable at {self.socket_path}: {exc}"
            ) from exc
        self._sock = sock
        self._rfile = sock.makefile("r", encoding="utf-8", newline="\n")
        self._wfile = sock.makefile("w", encoding="utf-8", newline="\n")

    def close(self) -> None:
        """Close the underlying connection, if open. Idempotent -- safe
        to call more than once, and safe to call on a never-connected
        instance.
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

    def __enter__(self) -> "RegistryClient":
        self.connect()
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    # -- wire protocol -----------------------------------------------------

    def _request(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Send one newline-delimited-JSON request and read its one
        response line, per ``docs/design/registry-api.md``'s framing.
        Mirrors ``registry.cli``'s pre-extraction ``_request`` and
        ``tests/registry/api/test_api.py``'s own ``_Client`` shape, minus
        the streaming ``flash`` op (no client of this module calls it --
        ``deploy.flash``/ticket 007 owns that op's own streaming
        wrapper).

        Raises :class:`RegistryUnavailable` if the connection drops or
        the daemon closes it mid-session; raises the appropriate
        :class:`RegistryClientError` subclass if the response is
        ``{"ok": false, ...}``.
        """
        self.connect()
        assert self._wfile is not None and self._rfile is not None
        try:
            self._wfile.write(json.dumps(payload))
            self._wfile.write("\n")
            self._wfile.flush()
            line = self._rfile.readline()
        except OSError as exc:
            raise RegistryUnavailable(f"registry unavailable: {exc}") from exc
        if not line:
            raise RegistryUnavailable("connection closed by mbregistry daemon")
        try:
            resp: dict[str, Any] = json.loads(line)
        except json.JSONDecodeError as exc:
            raise RegistryUnavailable(f"registry unavailable: {exc}") from exc

        if not resp.get("ok"):
            _raise_for_error(resp)
        return resp

    # -- typed ops -----------------------------------------------------

    def list(self) -> list[dict[str, Any]]:
        """Every device known to the registry, per ``docs/design/registry-
        api.md``'s ``list`` op -- each dict shaped like
        ``registry.api._device_dict`` (device record fields plus
        ``lock_kind``/``lock_pid``).
        """
        resp = self._request({"op": "list"})
        return list(resp.get("devices", []))

    def find(self, uid: str) -> dict[str, Any]:
        """One device by uid or short-uid token, per the ``find``/``get``
        op. Raises :class:`DeviceNotFoundError` if nothing matches.
        """
        resp = self._request({"op": "find", "uid": uid})
        return dict(resp["device"])

    def lock(self, uid: str, kind: str) -> None:
        """Acquire a ``kind``-kind lock on ``uid`` for this connection's
        pid. Raises :class:`DeviceNotFoundError` if ``uid`` doesn't
        resolve, or :class:`DeviceLockedError` (with ``holder`` set) if
        someone else already holds it. The lock is held by this
        connection until :meth:`unlock` or :meth:`close` -- see the
        module docstring's "Session model".
        """
        self._request({"op": "lock", "uid": uid, "kind": kind})

    def unlock(self, uid: str) -> bool:
        """Release this connection's lock on ``uid``, if any.

        Returns whether a lock was actually released (``False`` if this
        connection held no lock on ``uid``). Raises
        :class:`DeviceNotFoundError` if ``uid`` doesn't resolve.
        """
        resp = self._request({"op": "unlock", "uid": uid})
        return bool(resp.get("released", False))
