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

**Windows transport (sprint 005 ticket 005)**: ticket 003 left this class's
Windows side as a documented seam -- see the historical note on
:meth:`RegistryClient.connect` below. This ticket closes it:
:meth:`RegistryClient.connect` dispatches on ``sys.platform`` exactly like
``registry.cli``'s own ``run``/``install-service`` branches do, and on
``"win32"`` opens ``registry.api_windows.WindowsPipeAPIServer``'s named
pipe from the client side via ``api_windows._Win32PipeAPI
.open_client_pipe`` (``CreateFileW``) instead of ``socket.AF_UNIX``. It
then reuses that same module's ``_PipeLineReader``/``_PipeWriter``
newline-JSON framing classes unchanged -- they only need an object with
``read_file``/``write_file`` methods and a handle, which
``_Win32PipeAPI`` already provides identically for a client-opened handle
as for a server-accepted one, so :meth:`RegistryClient._request` (below)
needed zero changes: ``_PipeLineReader.readline()``/``_PipeWriter
.write``/``.flush`` present the exact same interface
``socket.makefile()`` already gave it. The pipe name itself is kept as a
plain ``str``, deliberately never routed through :class:`pathlib.Path`
(unlike ``self.socket_path`` on every other platform) -- ``Path``'s own
normalization of a UNC-shaped string (``registry.paths
.default_pipe_name()``'s ``r"\\\\.\\pipe\\mbregistry"``) on a real
``WindowsPath`` is exactly the kind of thing this project cannot verify
without Windows hardware, the same caveat ticket 003 originally flagged
this gap with. Every lock this session acquires is still released when
the pipe closes -- :class:`~mbtools.registry.api_windows
.WindowsPipeAPIServer`'s own ``_handle_connection`` releases a
connection's locks in its own ``finally`` block exactly like
``api.RegistryAPIServer`` does, unaffected by which side (Unix socket or
named pipe) closed first.

Proven with fakes on macOS/Linux (``tests/registry/client
/test_client_windows.py``, an injected ``win32=`` double mirroring
``api_windows``'s own test precedent) -- a real pipe round-trip (server
*and* client both for real) needs actual Windows and is
``skipif(sys.platform != "win32")`` there, first exercised by ticket
006's ``windows-latest`` CI job.
"""

from __future__ import annotations

import json
import os
import socket
import sys
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
from mbtools.registry.api_windows import _PipeLineReader, _PipeWriter, _Win32PipeAPI
from mbtools.registry.paths import default_pipe_name, find_client_socket

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
    "resolve_local_api_address",
    "find_local_api_address",
]

#: Mirrors ``registry.cli``'s pre-extraction ``_SOCKET_ENV_VAR`` exactly —
#: same name, same precedence (flag > env var > default) — so no caller's
#: invocation changes (this ticket's own acceptance criterion).
SOCKET_ENV_VAR = "MBREGISTRY_SOCKET"


def resolve_socket_path(
    flag_value: str | None,
    env_var: str = SOCKET_ENV_VAR,
    default: str | Path | None = DEFAULT_SOCKET_PATH,
) -> Path:
    """``flag_value`` wins if given; else ``$env_var`` if set; else
    ``default``. The one place socket-path precedence is decided, shared
    by every caller (``registry.cli``'s ``list``/``run``, and sprint
    002's ``mbdeploy``/``mbserial``) so it can't drift between them.

    ``default`` is ``None`` on Windows (``DEFAULT_SOCKET_PATH`` is not
    meaningful there -- see ``api.py``'s own docstring on that constant)
    -- calling this with no ``flag_value``/``env_var`` override on
    Windows raises ``TypeError`` from ``Path(None)`` below rather than
    silently returning a nonsense path. No caller in this sprint's scope
    (ticket 003) reaches that on Windows; ticket 005's Windows platform
    branch in ``cli.py`` is where a Windows-aware caller resolves the
    named pipe (``registry.paths.default_pipe_name``) instead of calling
    this function at all.
    """
    if flag_value:
        return Path(flag_value)
    env_value = os.environ.get(env_var)
    if env_value:
        return Path(env_value)
    return Path(default)


def resolve_local_api_address(
    flag_value: str | None, env_var: str = SOCKET_ENV_VAR
) -> str | Path:
    """``flag_value`` wins if given; else ``$env_var``; else this
    platform's own production default for the local query/control API --
    the platform-dispatching counterpart to :func:`resolve_socket_path`
    above, and the one place every local-registry CLI (``mbregistry``,
    ``mbdeploy``, ``mbserial``, ``mbrelay``) should resolve the address
    it hands to :class:`RegistryClient`, rather than calling
    :func:`resolve_socket_path` directly.

    That distinction matters on Windows: :func:`resolve_socket_path`'s
    own ``default`` parameter is :data:`DEFAULT_SOCKET_PATH`, which is
    ``None`` on ``sys.platform == "win32"`` (see that constant's
    docstring in ``registry.api`` -- there is no Unix-socket namespace to
    have a default path for there). A caller that calls
    :func:`resolve_socket_path` unconditionally, with no ``flag_value``/
    ``$env_var`` override in effect, hits ``Path(None)`` and raises
    ``TypeError`` before ever reaching :class:`RegistryClient`. This
    function dispatches on ``sys.platform`` *first*, so that failure
    mode never occurs: on ``"win32"`` the default (and any override) is
    returned as a plain ``str`` pipe name
    (:func:`~mbtools.registry.paths.default_pipe_name`) -- never routed
    through :class:`pathlib.Path`, matching :class:`RegistryClient`'s own
    "keep the pipe name as a plain str" contract (this module's
    docstring, "Windows transport"). Everywhere else, this delegates to
    :func:`resolve_socket_path` unchanged -- same flag > env var >
    :data:`DEFAULT_SOCKET_PATH` precedence as before.

    Originally sprint 005 ticket 005's private ``registry.cli
    ._resolve_local_api_address`` (shared there by ``cmd_run``/
    ``cmd_list``, the two ``mbregistry`` subcommands that need to know
    the daemon's own local-API address). Moved here in ticket 006, since
    ticket 005 only wired the platform dispatch into ``mbregistry``
    itself -- ``deploy.cli``/``serial.cli``/``relay.cli`` each still
    called :func:`resolve_socket_path` directly and would raise the
    ``TypeError`` above on Windows with no ``--socket``/
    ``$MBREGISTRY_SOCKET`` override given. ``registry.cli`` keeps
    ``_resolve_local_api_address`` as a thin alias of this function (see
    that module), so its own already-passing tests
    (``tests/registry/cli/test_cli_run_windows.py``) need no changes.
    """
    if sys.platform == "win32":
        if flag_value:
            return flag_value
        env_value = os.environ.get(env_var)
        if env_value:
            return env_value
        return default_pipe_name()
    return resolve_socket_path(flag_value, env_var, DEFAULT_SOCKET_PATH)


def find_local_api_address(
    flag_value: str | None, env_var: str = SOCKET_ENV_VAR
) -> str | Path:
    """Client-side counterpart of :func:`resolve_local_api_address`:
    ``flag_value`` wins, then ``$env_var``, then whichever daemon is
    actually running — this user's own per-user daemon first, then the
    system (root/systemd) daemon (:func:`~mbtools.registry.paths
    .find_client_socket`). Every client command (``mbregistry list``,
    ``mbdeploy``, ``mbserial``, ``mbrelay``) resolves through this, so a
    normal user reaches a root daemon with no flags. ``mbregistry run``
    itself keeps :func:`resolve_local_api_address`, since a daemon binds
    its own default rather than looking for another one.
    """
    if sys.platform == "win32":
        return resolve_local_api_address(flag_value, env_var)
    if flag_value:
        return Path(flag_value)
    env_value = os.environ.get(env_var)
    if env_value:
        return Path(env_value)
    return find_client_socket()


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

    **Windows transport (sprint 005 ticket 005)**: on ``sys.platform ==
    "win32"``, :meth:`connect` opens ``registry.api_windows
    .WindowsPipeAPIServer``'s named pipe instead of an ``AF_UNIX`` socket
    -- see the module docstring's "Windows transport" section for the
    full rationale, and the historical note this replaced (ticket 003
    originally left this class's Windows side as a documented seam).
    ``win32`` (constructor-injectable, mirroring every other Windows
    module's own ``win32=`` test seam in this package) is the pipe
    transport double: production code leaves it ``None`` (constructs a
    real ``api_windows._Win32PipeAPI``, which only works on real
    Windows); tests inject a fake implementing the same
    ``open_client_pipe``/``read_file``/``write_file``/``close_handle``
    surface.
    """

    def __init__(self, socket_path: str | Path, *, win32: Any | None = None) -> None:
        # On Windows, `socket_path` is actually a pipe name
        # (`registry.paths.default_pipe_name()`'s
        # r"\\.\pipe\mbregistry") -- kept as a plain str, deliberately
        # never routed through pathlib.Path (see module docstring's
        # "Windows transport"). Everywhere else, unchanged: a real
        # filesystem path to the AF_UNIX socket.
        if sys.platform == "win32":
            self.socket_path: str | Path = str(socket_path)
        else:
            self.socket_path = Path(socket_path)
        self._sock: socket.socket | None = None
        self._pipe_handle: int | None = None
        self._rfile: Any = None
        self._wfile: Any = None
        self._win32 = win32 if win32 is not None else _Win32PipeAPI()

    # -- connection lifecycle --------------------------------------------

    def connect(self) -> None:
        """Open the underlying connection, if not already open. Raises
        :class:`RegistryUnavailable` if the daemon can't be reached --
        mirrors ``registry.cli``'s pre-extraction ``_connect``/
        ``cmd_list`` behavior exactly (same ``OSError`` catch, same
        ``EXIT_NO_DAEMON`` trigger one level up) on every platform,
        including the Windows named-pipe branch below (``CreateFileW``
        failure -- no listener, or the pipe name is wrong -- raises
        :class:`OSError`, caught here exactly like a refused
        ``AF_UNIX`` connect).
        """
        if self._sock is not None or self._pipe_handle is not None:
            return
        if sys.platform == "win32":
            try:
                handle = self._win32.open_client_pipe(str(self.socket_path))
            except OSError as exc:
                raise RegistryUnavailable(
                    f"registry unavailable at {self.socket_path}: {exc}"
                ) from exc
            self._pipe_handle = handle
            # _PipeLineReader/_PipeWriter (api_windows.py) only need an
            # object with read_file/write_file and a handle -- the same
            # interface _Win32PipeAPI gives a client-opened handle as a
            # server-accepted one, so this framing is unchanged from the
            # server side (see module docstring).
            self._rfile = _PipeLineReader(self._win32, handle)
            self._wfile = _PipeWriter(self._win32, handle)
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
        instance. Releases every lock this session acquired -- on the
        Windows named-pipe branch this happens exactly the same way as
        on ``AF_UNIX``: ``WindowsPipeAPIServer._handle_connection``'s own
        ``finally`` block releases them when it observes the handle
        close, mirroring ``RegistryAPIServer``'s identical behavior (see
        module docstring).
        """
        for f in (self._rfile, self._wfile):
            if f is not None:
                try:
                    f.close()
                except OSError:
                    pass
        if self._pipe_handle is not None:
            try:
                self._win32.close_handle(self._pipe_handle)
            except OSError:
                pass
            self._pipe_handle = None
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

    def force_unlock(self, uid: str) -> dict[str, Any] | None:
        """Forcibly release ``uid``'s lock, regardless of who holds it,
        and close the holder's own connection so it observes EOF rather
        than silently losing exclusivity (sprint 008, ticket 003 --
        ``mbregistry unlock --force``'s own op, local-socket only; there
        is no remote-TCP-port equivalent, per sprint.md's Decisions).

        Returns ``None`` (not an error) if ``uid`` was already unlocked
        -- mirrors :meth:`~mbtools.registry.locks.LockManager
        .force_release`'s own "no-op, not an error" contract. Otherwise
        returns ``{"kind": ..., "holder": {...}}`` -- the released
        lock's kind and the same holder wire shape a ``locked`` response
        carries (including ``label``/``since``), so a caller can report
        what it broke. Raises :class:`DeviceNotFoundError` if ``uid``
        doesn't resolve.
        """
        resp = self._request({"op": "force_unlock", "uid": uid})
        if not resp.get("released"):
            return None
        return {"kind": resp["kind"], "holder": resp["holder"]}

    # -- name-registry ops (sprint 004, ticket 005) -----------------------
    #
    # Not device ops -- no uid, no lock -- see ``registry._api_base``'s
    # own "name-registry ops" section docstring for why they skip this
    # class's usual find/lock shape entirely.

    def names_get(self, name: str) -> dict[str, Any] | None:
        """The name registry's row for ``name``, or ``None`` if it has
        none yet -- the non-creating lookup (``names_get`` op,
        :meth:`~mbtools.registry.store.Store.get_name`). Unlike
        ``find``, an absent row is not an error: a caller wanting "is
        this name registered at all" (``mbrelay connect``'s own
        distinct-error requirement) tells that apart from a real
        protocol failure by checking for ``None`` here, not by catching
        an exception.
        """
        resp = self._request({"op": "names_get", "name": name})
        entry = resp.get("entry")
        return dict(entry) if entry is not None else None

    def names_set(self, name: str, channel: int, group: int) -> dict[str, Any]:
        """Explicitly assign ``name`` to ``(channel, group)`` (``names_set``
        op, :meth:`~mbtools.registry.store.Store.set`) -- overwrites any
        existing row, derived or previously registered. Returns the new
        row.
        """
        resp = self._request(
            {"op": "names_set", "name": name, "channel": channel, "group": group}
        )
        return dict(resp["entry"])

    def names_clear(self, name: str) -> None:
        """Drop ``name``'s row, if any (``names_clear`` op,
        :meth:`~mbtools.registry.store.Store.clear`). Not an error if
        ``name`` has no row.
        """
        self._request({"op": "names_clear", "name": name})

    def names_list(self) -> list[dict[str, Any]]:
        """Every ``name_registry`` row, each annotated with its own
        ``conflict``/``channel_conflict`` names (``names_list`` op,
        :meth:`~mbtools.registry.store.Store.listing`).
        """
        resp = self._request({"op": "names_list"})
        return list(resp.get("entries", []))

    def mark_flashed(self, uid: str) -> None:
        """Record that ``uid`` was just flashed, per the ``mark_flashed``
        op (ticket 003) -- bookkeeping only, for a flash that ran
        *outside* the registry's own ``flash`` op (sprint 002's
        ``mbdeploy``, which flashes locally via pyocd directly rather
        than through ``flash``; see ``docs/design/registry-api.md``).
        Increments ``store.flash_count`` for ``uid`` by one.

        Requires a ``flash``-kind lock already held by this connection
        (call :meth:`lock` first) -- the same precondition ``flash``
        itself has. Raises :class:`DeviceNotFoundError` if ``uid``
        doesn't resolve, or :class:`RegistryClientError` (``code ==
        "not_locked"``) if this connection doesn't hold the lock.
        """
        self._request({"op": "mark_flashed", "uid": uid})
