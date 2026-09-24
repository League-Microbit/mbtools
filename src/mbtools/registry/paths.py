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

**Sprint 006 decision note**: this module's darwin branches above
(:func:`system_socket_path`/:func:`user_socket_path`/
:func:`system_db_path`/:func:`user_db_path`) already returned correct,
user-writable macOS values before sprint 006 touched this file — they
are unchanged here. Reading them, together with sprint 006's own
acceptance work, is what answers
``clasi/issues/macos-default-socket-path-needs-a-user-writable-location.md``'s
"is macOS a supported daemon platform?" open question: **yes** — no
socket/db path-default rework was needed, only the launchd *service
install* mechanism sprint 006 adds (the ``macos_launch_agent_path``/
``macos_launch_daemon_path``/``macos_user_log_path``/
``macos_system_log_path`` helpers below, added by ticket 006-001 for
tickets 006-002/003/004 to build on).
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
    "macos_launch_agent_path",
    "macos_launch_daemon_path",
    "macos_user_log_path",
    "macos_system_log_path",
    "linux_user_unit_path",
    "LINUX_SYSTEM_UNIT_PATH",
    "LINUX_UDEV_RULE_PATH",
]

#: Fixed named-pipe path, in the Windows ``\\.\pipe\`` namespace. Not a
#: filesystem path — never created via ``mkdir``/``open`` — but returned
#: as a plain string on every platform so non-Windows tests can exercise
#: it too.
_WINDOWS_PIPE_NAME = r"\\.\pipe\mbregistry"

_APP = "mbregistry"

#: launchd label every rendered plist uses (ticket 006-002's own
#: ``Label``/``launchctl bootstrap ...`` argument), matching
#: docs/service.md §7's plists exactly. Shared by both
#: :func:`macos_launch_agent_path` and :func:`macos_launch_daemon_path`
#: so the label and the plist filename can never drift apart.
_LAUNCHD_LABEL = "org.jointheleague.mbregistry"


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


# -- service artifacts (ticket 006-001) --------------------------------------
#
# Where the service *manager's* own files live -- the launchd
# plist/systemd unit/log paths tickets 006-002 (macOS launchd) and
# 006-003 (Linux systemd) install, start, and report on. Same boundary as
# the db/socket helpers above: this module only knows *where* these files
# go, never *how* a service is installed (no plist/unit *content*
# rendering, no ``launchctl``/``systemctl`` invocation -- that is
# ``registry.service``'s job) and no override precedence (flags/env vars
# are a ``registry.service``/``registry.cli`` concern, same separation
# ``default_db_path``/``default_socket_path`` already draw against
# ``_resolve_path`` in ``cli.py``).
#
# Each helper here is scope-specific (one function per scope, mirroring
# ``system_db_path``/``user_db_path`` above) rather than a single
# function taking a ``scope`` string, since that is this module's
# existing convention and the acceptance criteria list them individually
# anyway.
#
# The Linux *system* unit path and the udev rule path are **not**
# redefined here as new functions -- both already exist as
# ``registry.cli``'s ``DEFAULT_UNIT_PATH``/``DEFAULT_UDEV_RULE_PATH``
# (ticket 008). This ticket adds :data:`LINUX_SYSTEM_UNIT_PATH`/
# :data:`LINUX_UDEV_RULE_PATH` as the *same* values, not a second
# formula for them, so ``registry.service`` (tickets 002/003) has a
# location-knowledge module to depend on without reaching into
# ``registry.cli`` (which would invert the dependency direction
# sprint.md's Design Rationale establishes: ``cli`` depends on
# ``service``/``paths``, never the reverse -- and would in fact be a
# real circular import, since ``cli.py`` already imports ``api.py``/
# ``store.py``, both of which import this module). Ticket 006-004 is
# where ``registry.cli``'s own two constants get replaced with imports
# of these (completing "exactly one definition"); until then,
# ``tests/registry/paths/test_paths.py`` cross-checks the two modules'
# values against each other so they cannot silently drift apart in the
# meantime.


def linux_user_unit_path() -> Path:
    """Per-user systemd unit location for ``service install --user``
    (ticket 006-003) — ``~/.config/systemd/user/mbregistry.service``,
    systemd's own fixed user-unit search path. No ``$XDG_CONFIG_HOME``
    override here (systemd's user-unit lookup itself does not honor
    that variable for this directory), matching this module's
    no-override-precedence boundary.
    """
    return _home() / ".config" / "systemd" / "user" / f"{_APP}.service"


def macos_launch_agent_path() -> Path:
    """User-scope (LaunchAgent) plist location — docs/service.md §7.2:
    ``~/Library/LaunchAgents/org.jointheleague.mbregistry.plist``.
    Loaded via ``launchctl bootstrap gui/$UID ...``, starts at login.
    """
    return _home() / "Library" / "LaunchAgents" / f"{_LAUNCHD_LABEL}.plist"


def macos_launch_daemon_path() -> Path:
    """System-scope (LaunchDaemon) plist location — docs/service.md
    §7.1: ``/Library/LaunchDaemons/org.jointheleague.mbregistry.plist``.
    Loaded via ``launchctl bootstrap system ...``, starts at boot; needs
    root to write.
    """
    return Path("/Library/LaunchDaemons") / f"{_LAUNCHD_LABEL}.plist"


def macos_user_log_path() -> Path:
    """User-scope (LaunchAgent) stdout/stderr log location —
    docs/service.md §7.2: ``~/Library/Logs/mbregistry.log``.
    """
    return _home() / "Library" / "Logs" / f"{_APP}.log"


def macos_system_log_path() -> Path:
    """System-scope (LaunchDaemon) stdout/stderr log location —
    docs/service.md §7.1: ``/Library/Logs/mbregistry.log``.
    """
    return Path("/Library/Logs") / f"{_APP}.log"


#: Standard systemd system-unit install location — same value as
#: ``registry.cli``'s ``DEFAULT_UNIT_PATH`` (ticket 008). See the
#: "service artifacts" module comment above for why this is a shared
#: value, not a second definition.
LINUX_SYSTEM_UNIT_PATH = Path("/etc/systemd/system/mbregistry.service")

#: Standard ``udev`` rules.d install location for the non-root-USB-access
#: rule — same value as ``registry.cli``'s ``DEFAULT_UDEV_RULE_PATH``
#: (ticket 008). See the "service artifacts" module comment above.
LINUX_UDEV_RULE_PATH = Path("/etc/udev/rules.d/99-mbregistry-cmsis-dap.rules")
