"""mbtools.registry.paths — the one place that knows where ``mbregistry``'s
on-disk/OS-namespace state lives, per platform.

Per sprint.md's Architecture (module "paths", Step 2 responsibility #2),
today's ``registry.store.DEFAULT_DB_PATH`` and ``registry.api
.DEFAULT_SOCKET_PATH`` were Linux-path literals; Windows needs
``ProgramData``-rooted equivalents (brief §9.2, spec Open decision #2)
and a named-pipe name (which has no filesystem path at all — it lives in
the ``\\\\.\\pipe\\`` namespace). This module centralizes that
platform dispatch so it exists in exactly one place rather than being
duplicated across ``store``, ``api``, ``api_windows``, and
``service_windows``.

**ASSUMPTION, not a ratified decision**: the Windows ``%ProgramData%``
root for ``default_db_path()`` (and the fixed pipe name for
``default_pipe_name()``) are this sprint's best guess at the right
Windows-native locations, per the brief's own open decision #2. They are
flagged here and restated in ``docs/migration.md`` for stakeholder
confirmation before any real Windows node is provisioned from them — do
not treat these as settled production paths.

This module does no I/O and has no dependency on ``pyserial`` or any
other platform-specific library — pure path/string computation — so
(unlike ``usbwatch``/``identity``) it needs no lazy-import guard.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

__all__ = [
    "default_db_path",
    "default_socket_path",
    "default_pipe_name",
]

#: Fixed named-pipe path, in the Windows ``\\.\pipe\`` namespace. Not a
#: filesystem path — never created via ``mkdir``/``open`` — but returned
#: as a plain string on every platform so non-Windows tests can exercise
#: it too (see module docstring's ASSUMPTION note).
_WINDOWS_PIPE_NAME = r"\\.\pipe\mbregistry"


def default_db_path() -> Path:
    """The production default for the SQLite device database.

    Linux/macOS (``sys.platform`` not ``"win32"``): unchanged from
    today's value, ``/var/lib/mbregistry/devices.db`` — identity worth
    keeping across reboots, per sprint.md's Design Rationale "file
    layout (ASSUMPTION)".

    Windows: a path under ``%ProgramData%`` (falling back to
    ``C:\\ProgramData`` if the environment variable is unset), the
    Windows-native analogue of ``/var/lib`` — machine-level, persistent
    state, not per-user. This is an ASSUMPTION per this module's
    docstring, not a stakeholder-confirmed decision.
    """
    if sys.platform == "win32":
        program_data = os.environ.get("PROGRAMDATA", r"C:\ProgramData")
        return Path(program_data) / "mbregistry" / "devices.db"
    return Path("/var/lib/mbregistry/devices.db")


def default_socket_path() -> Path:
    """The production default for the local Unix-socket API path.

    Linux/macOS: unchanged from today's value, ``/run/mbregistry/
    api.sock`` — purely-live state, cleared at boot, per sprint.md's
    Design Rationale "file layout (ASSUMPTION)".

    Windows has no Unix domain socket namespace, so this function is not
    meaningful there and raises :class:`NotImplementedError` rather than
    returning a nonsense path — callers on Windows use
    :func:`default_pipe_name` instead (ticket 005's platform branch in
    ``cli.py`` decides which of the two to call, based on
    ``sys.platform``).
    """
    if sys.platform == "win32":
        raise NotImplementedError(
            "mbtools.registry.paths: no Unix socket on Windows — use "
            "default_pipe_name() instead"
        )
    return Path("/run/mbregistry/api.sock")


def default_pipe_name() -> str:
    """The production default Windows named-pipe path, e.g.
    ``r"\\\\.\\pipe\\mbregistry"``.

    Unlike :func:`default_socket_path`, this is callable on *any*
    platform — it is just a fixed string, not I/O, so macOS/Linux tests
    can exercise it too. This is an ASSUMPTION per this module's
    docstring, not a stakeholder-confirmed decision.
    """
    return _WINDOWS_PIPE_NAME
