"""mbtools.registry.paths — the one place that knows where ``mbregistry``'s
on-disk/OS-namespace state lives, per platform and per privilege level.

``mbregistry run`` must work with no flags however it is started:

=========================  =========================================  ==========================================
                           database                                   local API socket
=========================  =========================================  ==========================================
Linux, root (systemd)      ``/var/lib/mbregistry/devices.db``         ``/run/mbregistry/api.sock``
Linux, normal user         ``$XDG_STATE_HOME/mbregistry/devices.db``  ``$XDG_RUNTIME_DIR/mbregistry/api.sock``
                           (default ``~/.local/state``)               (fallback ``~/.cache/mbregistry/api.sock``)
macOS, root                ``/Library/Application Support/            ``/var/run/mbregistry/api.sock``
                           mbregistry/devices.db``
macOS, normal user         ``~/Library/Application Support/           ``~/Library/Application Support/
                           mbregistry/devices.db``                    mbregistry/api.sock``
Windows                    ``%ProgramData%\\mbregistry\\devices.db``    named pipe ``\\\\.\\pipe\\mbregistry``
=========================  =========================================  ==========================================

The daemon uses the location matching the user it runs as. Client tools
(``mbregistry list``, ``mbdeploy``, ``mbserial``, ``mbrelay``) must find
whichever daemon is running, so they try :func:`client_socket_candidates`
in order: your own per-user daemon first, then the system daemon. That way
a normal user on a Pi finds the root systemd daemon with no flags.

Flags (``--db``/``--socket``) and ``$MBREGISTRY_DB``/``$MBREGISTRY_SOCKET``
still override all of this.

This module does no I/O (apart from the existence checks in
:func:`find_client_socket`) and needs no platform-specific libraries.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

__all__ = [
    "is_root",
    "system_db_path",
    "user_db_path",
    "default_db_path",
    "system_socket_path",
    "user_socket_path",
    "default_socket_path",
    "client_socket_candidates",
    "find_client_socket",
    "default_pipe_name",
]

#: Fixed named-pipe path, in the Windows ``\\.\pipe\`` namespace. Not a
#: filesystem path — never created via ``mkdir``/``open`` — but returned
#: as a plain string on every platform so non-Windows tests can exercise
#: it too.
_WINDOWS_PIPE_NAME = r"\\.\pipe\mbregistry"

_APP = "mbregistry"


def is_root() -> bool:
    """True when running with root privileges (never on Windows)."""
    if sys.platform == "win32":
        return False
    return os.geteuid() == 0


def _home() -> Path:
    return Path(os.path.expanduser("~"))


def _no_unix_socket() -> NotImplementedError:
    return NotImplementedError(
        "mbtools.registry.paths: no Unix socket on Windows — use "
        "default_pipe_name() instead"
    )


# -- database ---------------------------------------------------------------


def system_db_path() -> Path:
    """Machine-level database location, used when running as root."""
    if sys.platform == "win32":
        program_data = os.environ.get("PROGRAMDATA", r"C:\ProgramData")
        return Path(program_data) / _APP / "devices.db"
    if sys.platform == "darwin":
        return Path("/Library/Application Support") / _APP / "devices.db"
    return Path("/var/lib") / _APP / "devices.db"


def user_db_path() -> Path:
    """Per-user database location, used when running as a normal user."""
    if sys.platform == "win32":
        return system_db_path()
    if sys.platform == "darwin":
        return _home() / "Library" / "Application Support" / _APP / "devices.db"
    state = os.environ.get("XDG_STATE_HOME") or str(_home() / ".local" / "state")
    return Path(state) / _APP / "devices.db"


def default_db_path() -> Path:
    """The database the daemon uses when no ``--db``/``$MBREGISTRY_DB``
    is given: the system location as root, the per-user one otherwise."""
    return system_db_path() if is_root() else user_db_path()


# -- local API socket -------------------------------------------------------


def system_socket_path() -> Path:
    """Machine-level socket location, used by a root (service) daemon."""
    if sys.platform == "win32":
        raise _no_unix_socket()
    if sys.platform == "darwin":
        return Path("/var/run") / _APP / "api.sock"
    return Path("/run") / _APP / "api.sock"


def user_socket_path() -> Path:
    """Per-user socket location, used by a daemon run as a normal user."""
    if sys.platform == "win32":
        raise _no_unix_socket()
    if sys.platform == "darwin":
        return _home() / "Library" / "Application Support" / _APP / "api.sock"
    runtime = os.environ.get("XDG_RUNTIME_DIR")
    if runtime and os.path.isdir(runtime):
        return Path(runtime) / _APP / "api.sock"
    return _home() / ".cache" / _APP / "api.sock"


def default_socket_path() -> Path:
    """The socket the daemon binds when no ``--socket``/``$MBREGISTRY_SOCKET``
    is given: the system location as root, the per-user one otherwise.

    Raises :class:`NotImplementedError` on Windows, which uses
    :func:`default_pipe_name` instead.
    """
    return system_socket_path() if is_root() else user_socket_path()


def client_socket_candidates() -> list[Path]:
    """Where a client should look for a running daemon, in order: the
    daemon matching this user first, then the other one (so a normal user
    finds the root systemd daemon, and root finds a user's daemon)."""
    if sys.platform == "win32":
        raise _no_unix_socket()
    first, second = (
        (system_socket_path(), user_socket_path())
        if is_root()
        else (user_socket_path(), system_socket_path())
    )
    return [first] if first == second else [first, second]


def find_client_socket() -> Path:
    """The first candidate that exists, or the first candidate if none do
    (so the "registry unavailable" error names the expected path)."""
    candidates = client_socket_candidates()
    for candidate in candidates:
        if candidate.exists():
            return candidate
    return candidates[0]


def default_pipe_name() -> str:
    """The Windows named-pipe path, ``r"\\\\.\\pipe\\mbregistry"``.

    Callable on any platform — it is just a fixed string — so macOS/Linux
    tests can exercise it too.
    """
    return _WINDOWS_PIPE_NAME
