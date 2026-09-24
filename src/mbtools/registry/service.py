"""mbtools.registry.service — install/uninstall/status the ``mbregistry``
service, at whichever platform/scope is asked for.

Per sprint.md's Architecture (Step 3, module ``registry.service``), this
module owns plist/unit/udev-rule *rendering*, the command-runner seam,
and per-platform/scope *orchestration* — everything responsibility 4
("orchestrating install/uninstall/status per platform+scope") in Step 2
needs, sitting between ``registry.cli`` (presentation, calls in) and
``registry.paths`` (location knowledge, called out to). See that
section's component diagram for the shape of the whole graph this module
sits in.

Ticket 006-001 built the foundation: the command-execution seam every
real orchestration function below calls through, instead of shelling
out itself — a real implementation by default
(:class:`SubprocessCommandRunner`) and a dry-run double
(:class:`DryRunCommandRunner`) that produces the exact same "here's what
would run" text ``--dry-run`` needs. Matches sprint.md's Design
Rationale ("a single injectable command-runner seam, not per-tool
mocks"), the same injectable-seam convention ``registry.flash``'s
``runner``/``registry.usbwatch``'s ``PortWatcher`` already use in this
package.

Ticket 006-002 (this ticket) adds the macOS half: launchd plist
rendering and install/uninstall/status orchestration, in the "macOS:
launchd" section below. Ticket 006-003 adds the Linux half (systemd
unit/udev-rule rendering and orchestration) alongside it, in its own
section, sharing :class:`ServiceStatus` and the command-runner seam but
otherwise kept platform-separate so neither reaches into the other's
helpers. Ticket 006-004 wires both into ``registry.cli`` — nothing here
is called from anywhere real yet.

No test in this module's own test suite invokes a real external command
— see :class:`SubprocessCommandRunner`'s docstring for why that
guarantee is safe to state even though it is the "real" implementation.
"""

from __future__ import annotations

import getpass
import os
import plistlib
import shlex
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import IO, Protocol, runtime_checkable

from .paths import (
    LINUX_SYSTEM_UNIT_PATH,
    LINUX_UDEV_RULE_PATH,
    linux_user_unit_path,
    macos_launch_agent_path,
    macos_launch_daemon_path,
    macos_system_log_path,
    macos_user_log_path,
    system_db_path,
    system_socket_path,
    user_db_path,
    user_socket_path,
)

__all__ = [
    "CommandRunner",
    "SubprocessCommandRunner",
    "DryRunCommandRunner",
    "default_runner",
    "ServiceStatus",
    "render_launchd_plist",
    "macos_install",
    "macos_uninstall",
    "macos_status",
    "render_systemd_unit",
    "render_udev_rule",
    "LinuxUserPreflightError",
    "linux_install",
    "linux_uninstall",
    "linux_status",
]


@runtime_checkable
class CommandRunner(Protocol):
    """The one command-execution seam every ``registry.service``
    orchestration function (tickets 006-002/003) calls through, instead
    of shelling out itself. Deliberately narrow — a single method, no
    knowledge of *which* command it is running (``launchctl``,
    ``systemctl``, ``udevadm``, ``usermod``, ``loginctl`` are all just
    ``argv`` to this interface) — so :class:`DryRunCommandRunner` can
    stand in for :class:`SubprocessCommandRunner` at exactly the same
    call sites, and a test can inject its own fake the same way
    ``registry.flash.FlashOp``'s ``runner`` parameter already does.
    """

    def run(self, argv: list[str]) -> subprocess.CompletedProcess[str]:
        """Run ``argv`` and return a
        :class:`subprocess.CompletedProcess`-shaped result (``args``,
        ``returncode``, ``stdout``, ``stderr``) so a caller that needs
        the output — e.g. ``launchctl print``/``systemctl --user
        is-active`` for :func:`~mbtools.registry.service` status
        reporting (ticket 006-002/003) — can read it. A caller that only
        cares whether the command succeeded checks ``.returncode``
        itself; this seam never raises on a nonzero exit by itself (see
        :class:`SubprocessCommandRunner`'s own ``check`` parameter for
        where that choice is made).
        """
        ...


class SubprocessCommandRunner:
    """The real, production :class:`CommandRunner`: shells out via
    :func:`subprocess.run`.

    ``check=True`` (the default) raises :class:`subprocess.CalledProcessError`
    on a nonzero exit, matching this package's other real-implementation
    seams (e.g. ``registry.flash``'s pyocd invocation treats a nonzero
    exit as a flash failure, not a silent return) — an orchestration
    function that wants to inspect a failing exit code instead of having
    it raised (e.g. "is the unit loaded" status checks, which are
    expected to fail cleanly when nothing is installed) constructs its
    own instance with ``check=False``.

    Never constructed or ``.run()``-called by this ticket's own tests —
    only :class:`DryRunCommandRunner` and hand-rolled fakes are, per the
    Test Strategy's "no test invokes a real external command." A future
    ticket (006-002/003) is free to unit-test this class itself against
    a harmless real command (e.g. ``["true"]``/``["echo", "hi"]``) if it
    wants coverage of the ``subprocess.run`` call itself, but that is not
    this ticket's job.
    """

    def __init__(self, *, check: bool = True) -> None:
        self._check = check

    def run(self, argv: list[str]) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            argv,
            check=self._check,
            capture_output=True,
            text=True,
        )


class DryRunCommandRunner:
    """``--dry-run``'s :class:`CommandRunner`: never executes ``argv`` —
    prints ``would run: <command>`` to ``stream`` (stderr by default, so
    it never pollutes a machine-readable stdout, e.g. ``status``'s
    ``--json`` output) and returns a synthetic, always-zero-exit
    :class:`subprocess.CompletedProcess` instead.

    This is what lets every orchestration function in tickets 002/003
    have exactly one code path for "render, write, run commands" — told
    to print instead of execute, rather than a parallel dry-run branch
    that could silently drift from what real execution does (sprint.md's
    Design Rationale, same section as :class:`CommandRunner`'s own
    docstring). The synthetic result's always-zero ``returncode`` means
    an orchestration function must not branch on the *result* of a
    dry-run command to decide what to do next — it already knows it is
    in dry-run mode by construction (it chose this runner), so nothing
    in 002/003 should need to.
    """

    def __init__(self, *, stream: IO[str] | None = None) -> None:
        self._stream = stream if stream is not None else sys.stderr

    def run(self, argv: list[str]) -> subprocess.CompletedProcess[str]:
        print(f"would run: {shlex.join(argv)}", file=self._stream)
        return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")


def default_runner(*, dry_run: bool = False) -> CommandRunner:
    """The one place that turns ``--dry-run`` into "which
    :class:`CommandRunner`": :class:`DryRunCommandRunner` when
    ``dry_run`` is true, :class:`SubprocessCommandRunner` otherwise.
    Tickets 002/003's orchestration functions take a
    ``runner: CommandRunner | None = None`` parameter (matching
    ``registry.flash.FlashOp``'s ``runner``-parameter convention) and
    call this to fill it in when no runner is injected — a test always
    injects its own fake instead, so this function itself is only ever
    exercised by production code and by this ticket's own unit tests
    asserting it picks the right class.
    """
    return DryRunCommandRunner() if dry_run else SubprocessCommandRunner()


# ---------------------------------------------------------------------------
# Service status — shared across platforms (ticket 006-003 reuses this for
# systemd, rather than each platform inventing its own status shape).
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ServiceStatus:
    """The result of a ``status`` check, for one platform+scope.

    ``installed`` is "does the plist/unit file exist at this scope's
    path" — a pure filesystem check, never a ``launchctl``/``systemctl``
    call. ``running`` is only meaningful when ``installed`` is true (it
    is always ``False`` otherwise) and comes from asking the service
    manager whether the label/unit is currently loaded — see
    :func:`macos_status`.
    """

    scope: str
    path: Path
    installed: bool
    running: bool

    def describe(self) -> str:
        """One of the three human-readable states the acceptance
        criteria name: ``"not installed"``, ``"installed but not
        running"``, ``"installed and running"``."""
        if not self.installed:
            return "not installed"
        return "installed and running" if self.running else "installed but not running"


# ---------------------------------------------------------------------------
# macOS: launchd (ticket 006-002)
# ---------------------------------------------------------------------------

_MACOS_SCOPES = ("user", "system")


def _validate_macos_scope(scope: str) -> None:
    if scope not in _MACOS_SCOPES:
        raise ValueError(
            f"unknown macOS service scope: {scope!r} (expected 'user' or 'system')"
        )


def _macos_plist_path(scope: str) -> Path:
    """The plist path for ``scope`` — ticket 001's
    :func:`~mbtools.registry.paths.macos_launch_agent_path`/
    :func:`~mbtools.registry.paths.macos_launch_daemon_path`, dispatched
    on scope so every macOS function below shares one lookup."""
    _validate_macos_scope(scope)
    return macos_launch_agent_path() if scope == "user" else macos_launch_daemon_path()


def _macos_log_path(scope: str) -> Path:
    """The stdout/stderr log path for ``scope`` — ticket 001's
    :func:`~mbtools.registry.paths.macos_user_log_path`/
    :func:`~mbtools.registry.paths.macos_system_log_path`."""
    _validate_macos_scope(scope)
    return macos_user_log_path() if scope == "user" else macos_system_log_path()


def _macos_domain(scope: str) -> str:
    """The ``launchctl`` domain target for ``scope`` — ``system`` for
    the LaunchDaemon, ``gui/<uid>`` for the LaunchAgent (docs/service.md
    §7's ``gui/$(id -u)``).

    Reads the current uid via ``os.getuid`` rather than hard-coding it,
    but through ``getattr`` rather than a bare call: ``os.getuid``
    doesn't exist on Windows, and this ticket's acceptance criteria
    require these tests to run (not skip) on Windows CI too, entirely
    through a mocked runner — the uid value itself is never asserted on
    a platform where it wouldn't resolve for real.
    """
    _validate_macos_scope(scope)
    if scope == "system":
        return "system"
    getuid = getattr(os, "getuid", None)
    return f"gui/{getuid() if getuid is not None else 0}"


def _macos_working_directory(scope: str) -> str:
    """``/`` for the LaunchDaemon (docs/service.md §7.1), the operating
    user's home directory for the LaunchAgent (§7.2, where launchd
    doesn't expand ``~`` so the doc itself substitutes ``$HOME``)."""
    _validate_macos_scope(scope)
    return "/" if scope == "system" else str(Path.home())


def render_launchd_plist(scope: str, *, exec_path: str | None = None) -> str:
    """The launchd plist text for ``scope`` (``"user"`` → LaunchAgent,
    ``"system"`` → LaunchDaemon), matching docs/service.md §7.1/§7.2's
    hand-written plists: same ``Label`` (derived from the plist's own
    filename, so it can never drift from
    :func:`~mbtools.registry.paths.macos_launch_agent_path`/
    :func:`~mbtools.registry.paths.macos_launch_daemon_path`), same
    ``ProgramArguments`` shape (``exec_path -m mbtools.registry.cli
    run``, invoked through the running interpreter by default — same
    "not a bare PATH lookup" reasoning as ``render_systemd_unit``), same
    ``KeepAlive``/``SuccessfulExit=false``/``ThrottleInterval=10``, and
    the ticket-001 log path as both ``StandardOutPath`` and
    ``StandardErrorPath``.

    Built with :mod:`plistlib` rather than hand-rolled XML, per this
    ticket's own instructions — the returned text round-trips through
    :func:`plistlib.loads` to the same dict every golden-file test in
    this ticket's suite asserts against.
    """
    _validate_macos_scope(scope)
    plist_path = _macos_plist_path(scope)
    log_path = str(_macos_log_path(scope))
    program = exec_path if exec_path is not None else sys.executable

    plist: dict[str, object] = {
        "Label": plist_path.stem,
        "ProgramArguments": [program, "-m", "mbtools.registry.cli", "run"],
        "EnvironmentVariables": {"PYTHONUNBUFFERED": "1"},
        "WorkingDirectory": _macos_working_directory(scope),
        "RunAtLoad": True,
        "KeepAlive": {"SuccessfulExit": False},
        "ThrottleInterval": 10,
        "StandardOutPath": log_path,
        "StandardErrorPath": log_path,
    }
    return plistlib.dumps(plist, fmt=plistlib.FMT_XML).decode("utf-8")


def macos_install(
    scope: str,
    *,
    dry_run: bool = False,
    runner: CommandRunner | None = None,
) -> Path:
    """Install the ``mbregistry`` launchd service at ``scope``: write
    the plist (skipped entirely when ``dry_run`` — the plist write is
    direct file I/O, not part of the command-runner seam, so it needs
    its own explicit gate), then run the same three ``launchctl`` verbs
    docs/service.md §7.1/§7.2 lists, through ``runner``
    (:func:`default_runner` when none is injected, which is what turns
    ``dry_run`` into "print, don't execute" for the commands
    themselves).

    **Idempotent by construction**: ``bootout`` runs first, tolerating
    the expected failure when nothing was loaded yet (a fresh install),
    before ``enable``+``bootstrap`` — the safe sequence
    docs/service.md's own "Manage it" commands are built from, and the
    reason re-running this function against an already-loaded label
    does not raise: unlike ``enable``/``bootstrap``, ``bootout``'s
    failure is the *expected* case on a fresh install, not a real error.

    Returns the plist path written (or that would have been written,
    under ``dry_run``) — this function does no user-facing printing of
    its own (ticket 006-004's job); a caller wanting to report full
    state calls :func:`macos_status` afterward.
    """
    _validate_macos_scope(scope)
    plist_path = _macos_plist_path(scope)
    domain = _macos_domain(scope)
    label = plist_path.stem
    service_target = f"{domain}/{label}"
    runner = runner if runner is not None else default_runner(dry_run=dry_run)

    if not dry_run:
        plist_path.parent.mkdir(parents=True, exist_ok=True)
        log_path = _macos_log_path(scope)
        log_path.parent.mkdir(parents=True, exist_ok=True)
        plist_path.write_text(render_launchd_plist(scope))

    try:
        runner.run(["launchctl", "bootout", service_target])
    except subprocess.CalledProcessError:
        pass  # expected when nothing was loaded yet -- not a real failure
    runner.run(["launchctl", "enable", service_target])
    runner.run(["launchctl", "bootstrap", domain, str(plist_path)])

    return plist_path


def macos_uninstall(
    scope: str,
    *,
    purge: bool = False,
    dry_run: bool = False,
    runner: CommandRunner | None = None,
) -> str:
    """Uninstall the ``mbregistry`` launchd service at ``scope``: stop
    it (``launchctl bootout``, tolerating the "not loaded" failure the
    same way :func:`macos_install` does) and remove its plist file.
    Keeps ``devices.db`` unless ``purge=True``, in which case the whole
    state directory (ticket 001's
    :func:`~mbtools.registry.paths.system_db_path`/
    :func:`~mbtools.registry.paths.user_db_path`, parent directory) is
    removed too. macOS has no udev/``plugdev`` equivalent to clean up at
    either scope.

    Safe no-op (returns a message, never raises) when nothing is
    installed at ``scope`` — issues no ``launchctl`` call at all in that
    case, since there is nothing to stop. Checks the *other* scope's
    plist too and names it in the returned message when found, per this
    ticket's acceptance criteria.

    **Also removes the socket and log files** (the linked issue's own
    "removes the plist/unit, ... the socket and log files" uninstall
    behavior), unconditionally on every real (non-``dry_run``) uninstall
    — not gated on ``purge``. ``devices.db`` is the one thing kept
    unless ``purge=True``, in which case the whole state directory
    (parent of :func:`~mbtools.registry.paths.system_db_path`/
    :func:`~mbtools.registry.paths.user_db_path`) is removed too; on
    macOS that directory is also where the socket file lives for both
    scopes, so a purge removes it twice-over (the explicit unlink below,
    then the ``rmtree``) — both are ``missing_ok=True``/tolerant of the
    directory already being gone, so the order never raises.

    Returns a human-readable outcome message; ticket 006-004 is the one
    that prints it.
    """
    _validate_macos_scope(scope)
    plist_path = _macos_plist_path(scope)
    other_scope = "system" if scope == "user" else "user"
    other_plist_path = _macos_plist_path(other_scope)

    if not plist_path.exists():
        message = f"macOS {scope} service is not installed (no {plist_path})."
        if other_plist_path.exists():
            message += (
                f" A {other_scope}-scope install exists at {other_plist_path}."
            )
        return message

    domain = _macos_domain(scope)
    label = plist_path.stem
    service_target = f"{domain}/{label}"
    runner = runner if runner is not None else default_runner(dry_run=dry_run)

    try:
        runner.run(["launchctl", "bootout", service_target])
    except subprocess.CalledProcessError:
        pass  # already stopped/unloaded -- not a real failure

    if not dry_run:
        plist_path.unlink(missing_ok=True)

        socket_path = system_socket_path() if scope == "system" else user_socket_path()
        socket_path.unlink(missing_ok=True)
        log_path = _macos_log_path(scope)
        log_path.unlink(missing_ok=True)

        if purge:
            db_path = system_db_path() if scope == "system" else user_db_path()
            state_dir = db_path.parent
            if state_dir.exists():
                shutil.rmtree(state_dir)

    return f"Removed macOS {scope} service ({plist_path})."


def macos_status(scope: str, *, runner: CommandRunner | None = None) -> ServiceStatus:
    """Report install/running state for ``scope``: ``installed`` is a
    plain filesystem check on the plist path; ``running`` (only checked
    when installed) comes from ``launchctl print <domain>/<label>`` —
    any non-error exit is treated as "loaded", per this ticket's own
    "keep this simple and testable" guidance, rather than parsing the
    full state-dump output.

    Defaults to a ``check=False`` :class:`SubprocessCommandRunner` (not
    :func:`default_runner`) when no ``runner`` is injected: unlike
    ``install``/``uninstall``, a failing exit here is the ordinary,
    expected "not currently loaded" outcome, not a dry-run concern — see
    :class:`CommandRunner`'s own docstring for this exact case.
    """
    _validate_macos_scope(scope)
    plist_path = _macos_plist_path(scope)
    installed = plist_path.exists()
    running = False
    if installed:
        domain = _macos_domain(scope)
        label = plist_path.stem
        service_target = f"{domain}/{label}"
        active_runner = runner if runner is not None else SubprocessCommandRunner(check=False)
        result = active_runner.run(["launchctl", "print", service_target])
        running = result.returncode == 0

    return ServiceStatus(scope=scope, path=plist_path, installed=installed, running=running)


# ---------------------------------------------------------------------------
# Linux: systemd/udev (ticket 006-003)
# ---------------------------------------------------------------------------
#
# render_systemd_unit()/render_udev_rule() below are a *move*, not a
# rewrite, of the two functions that used to live in registry.cli (ticket
# 008/009) -- their system-scope content, signatures, and golden-file test
# assertions are unchanged; render_systemd_unit() gains a ``scope``
# parameter (extending the existing signature, per this ticket's own
# instructions, rather than adding a parallel function) so it can also
# render the new user-scope unit variant. registry.cli keeps importing
# both names from here (see that module's own updated imports) so
# cmd_install_service keeps working unchanged until ticket 006-004
# rewires it onto registry.service's orchestration functions directly.

_LINUX_SCOPES = ("user", "system")

#: The unit name every ``systemctl``/``loginctl`` invocation below
#: targets -- the same basename both :func:`~mbtools.registry.paths
#: .linux_user_unit_path` and :data:`~mbtools.registry.paths
#: .LINUX_SYSTEM_UNIT_PATH` end in, so it can never drift from either.
_LINUX_UNIT_NAME = "mbregistry.service"

#: The micro:bit DAPLink interface's fixed VID:PID (sprint.md SUC-005,
#: Decision 9) -- moved verbatim from ``registry.cli`` (tickets
#: 008/009), unchanged. Shared between the tty/usb/hidraw match rules in
#: :func:`render_udev_rule` so they can never drift apart from each
#: other.
_USB_VENDOR_ID = "0d28"
_USB_PRODUCT_ID = "0204"

#: The group :func:`render_udev_rule`'s rules grant access via, alongside
#: ``TAG+="uaccess"`` -- moved verbatim from ``registry.cli``. Also the
#: group :func:`linux_install`'s ``--system`` path adds the operator to,
#: and the group :func:`_linux_user_in_plugdev` checks membership of for
#: the ``--user`` preflight refusal below.
_UDEV_GROUP = "plugdev"


def _validate_linux_scope(scope: str) -> None:
    if scope not in _LINUX_SCOPES:
        raise ValueError(
            f"unknown Linux service scope: {scope!r} (expected 'user' or 'system')"
        )


def _linux_unit_path(scope: str) -> Path:
    """The systemd unit path for ``scope`` — ticket 001's
    :func:`~mbtools.registry.paths.linux_user_unit_path`/
    :data:`~mbtools.registry.paths.LINUX_SYSTEM_UNIT_PATH`, dispatched on
    scope so every Linux function below shares one lookup (mirrors
    :func:`_macos_plist_path`'s own shape)."""
    _validate_linux_scope(scope)
    return linux_user_unit_path() if scope == "user" else LINUX_SYSTEM_UNIT_PATH


def _resolve_linux_operating_user(flag_value: str | None) -> str:
    """Same precedence as ``registry.cli``'s own
    ``_resolve_operating_user`` (tickets 008/009): an explicit
    ``flag_value`` wins; else ``$SUDO_USER``; else ``$USER``; else
    :func:`getpass.getuser`.

    Duplicated here rather than imported from ``registry.cli`` because
    sprint.md's Architecture fixes the dependency direction as ``cli``
    -> ``service``, never the reverse — importing from ``cli`` here
    would also be a real circular import, since ``cli.py`` already
    imports this module. This ticket's own plan flags this as "moved or
    imported from wherever ticket 004 leaves it"; until ticket 006-004
    consolidates the two copies (moving ``cli.py``'s version here, the
    same "move, not duplicate" treatment this ticket already gives
    ``render_systemd_unit``/``render_udev_rule``), this is a second,
    behavior-identical copy, not a second formula.
    """
    if flag_value:
        return flag_value
    sudo_user = os.environ.get("SUDO_USER")
    if sudo_user:
        return sudo_user
    user = os.environ.get("USER")
    if user:
        return user
    return getpass.getuser()


def _linux_user_in_plugdev(user: str) -> bool:
    """Whether ``user`` is a member of :data:`_UDEV_GROUP` (checked as
    primary *or* supplementary group membership) — the first half of
    :func:`linux_install`'s ``--user`` preflight check.

    Uses :mod:`grp`/:mod:`pwd`, imported *locally* (not at module scope)
    so importing ``registry.service`` on Windows — this module's own
    macOS tests already parametrize over ``win32`` to prove the module
    imports cleanly there — never fails just because this function
    exists; neither module exists on Windows. Real production code
    never calls this on Windows: ``registry.cli``'s Windows
    short-circuit (ticket 006-004) never reaches :func:`linux_install`
    at all.

    A ``user`` with no such account (the shape every test in this
    ticket's suite uses, since no test may depend on this host's real
    accounts) resolves to ``False`` via the same :class:`KeyError` path
    a real "no such user" would take.
    """
    import grp
    import pwd

    try:
        pw_record = pwd.getpwnam(user)
    except KeyError:
        return False
    try:
        if grp.getgrgid(pw_record.pw_gid).gr_name == _UDEV_GROUP:
            return True
    except KeyError:
        pass
    try:
        return user in grp.getgrnam(_UDEV_GROUP).gr_mem
    except KeyError:
        return False


class LinuxUserPreflightError(RuntimeError):
    """Raised by :func:`linux_install` (``scope="user"``) when the
    operating user is not (yet) in :data:`_UDEV_GROUP`, or the
    system-scope udev rule does not exist — the issue's own explicit
    "install --user prints the exact sudo commands and stops" behavior,
    rather than installing a user service that can never open the
    boards it's meant to manage (a lingering user service has no
    logind seat, so the udev ``uaccess`` tag grants it nothing).

    The exact remediation text is both printed to stderr (before this
    is raised) and carried on the exception itself (``str(exc)``), so a
    caller that wants the message without re-parsing stderr can read
    it. Ticket 006-004's CLI wiring is expected to catch this and turn
    it into a distinct nonzero exit code, per this ticket's acceptance
    criteria.
    """


#: Rendered with ``str.format(exec_start=...)`` for the **system** scope
#: -- moved verbatim from ``registry.cli`` (tickets 008/009), unchanged.
#: ``RuntimeDirectory=``/``StateDirectory=`` are systemd's own directive
#: for "create this subdirectory of /run or /var/lib, owned by this
#: service, before ExecStart runs", so this module never has to shell
#: out to ``mkdir`` itself for those two paths. Named ``mbregistry.service``,
#: distinct from the fleet's existing ``mbrelay.service``.
_SYSTEMD_UNIT_TEMPLATE = """\
[Unit]
Description=mbregistry -- micro:bit device registry daemon
After=network.target

[Service]
Type=simple
ExecStart={exec_start}
Restart=on-failure
RestartSec=2
RuntimeDirectory=mbregistry
StateDirectory=mbregistry

[Install]
WantedBy=multi-user.target
"""

#: Rendered with ``str.format(exec_start=...)`` for the **user** scope --
#: no ``RuntimeDirectory=``/``StateDirectory=`` (those are
#: system-manager-only directives; a user unit relies on
#: :func:`~mbtools.registry.paths.user_socket_path`/
#: :func:`~mbtools.registry.paths.user_db_path`, which already resolve to
#: locations that exist without them), ``WantedBy=default.target``
#: instead of ``multi-user.target`` (systemd user managers have no
#: ``multi-user.target``). Same ``[Unit]``/``ExecStart=``/``Restart=``
#: shape as the system-scope template just above it, otherwise.
_SYSTEMD_USER_UNIT_TEMPLATE = """\
[Unit]
Description=mbregistry -- micro:bit device registry daemon
After=network.target

[Service]
Type=simple
ExecStart={exec_start}
Restart=on-failure
RestartSec=2

[Install]
WantedBy=default.target
"""


def render_systemd_unit(exec_start: str | None = None, *, scope: str = "system") -> str:
    """The systemd unit's text for ``scope`` (``"system"``, the default
    — preserves this function's pre-006-003 signature/behavior exactly,
    since every existing call site and golden-file test calls it with no
    ``scope`` at all — or ``"user"``, the ticket 006-003 addition; see
    :data:`_SYSTEMD_USER_UNIT_TEMPLATE`'s own comment for what differs).
    ``exec_start`` defaults to invoking ``mbregistry run`` through the
    current interpreter (``{sys.executable} -m mbtools.registry.cli
    run``) -- the same "invoke through the running interpreter, not a
    bare PATH lookup" reasoning ``flash.py``'s own ``_PYOCD`` already
    documents: mbtools is typically installed into an isolated venv
    whose ``bin/`` directory is not on the ``PATH`` systemd uses for a
    unit's ``ExecStart=``, but the package is always importable through
    the interpreter that installed it.
    """
    _validate_linux_scope(scope)
    if exec_start is None:
        exec_start = f"{sys.executable} -m mbtools.registry.cli run"
    template = _SYSTEMD_UNIT_TEMPLATE if scope == "system" else _SYSTEMD_USER_UNIT_TEMPLATE
    return template.format(exec_start=exec_start)


#: Three match rules, one per device node ``mbserial``/pyOCD open for the
#: micro:bit DAPLink interface (sprint.md SUC-005) -- moved verbatim from
#: ``registry.cli`` (tickets 008/009); see the original module's history
#: for the full per-rule rationale (CDC-ACM tty / raw USB / hidraw, and
#: why both ``GROUP=``/``MODE=`` *and* ``TAG+="uaccess"`` are written into
#: every rule).
_UDEV_RULE_TEMPLATE = """\
# mbregistry -- non-root access to the micro:bit DAPLink interface
# (VID:PID {vendor_id}:{product_id}). Written by `mbregistry service install`
# (ticket 006-003) -- re-running it overwrites this file with identical
# content, so re-running install is idempotent. See
# mbtools.registry.service.render_udev_rule()'s docstring for why each rule
# below grants access via both the {group} group and uaccess rather than
# just one of the two.

# CDC-ACM tty device node (mbserial, and pyOCD's DAPLink serial transport)
SUBSYSTEM=="tty", SUBSYSTEMS=="usb", ATTRS{{idVendor}}=="{vendor_id}", ATTRS{{idProduct}}=="{product_id}", GROUP="{group}", MODE="0660", TAG+="uaccess"

# Raw USB device node (CMSIS-DAP v2 / WinUSB transport, opened directly by pyOCD)
SUBSYSTEM=="usb", ATTRS{{idVendor}}=="{vendor_id}", ATTRS{{idProduct}}=="{product_id}", GROUP="{group}", MODE="0660", TAG+="uaccess"

# hidraw device node (CMSIS-DAP v1 / HID transport)
KERNEL=="hidraw*", ATTRS{{idVendor}}=="{vendor_id}", ATTRS{{idProduct}}=="{product_id}", GROUP="{group}", MODE="0660", TAG+="uaccess"
"""


def render_udev_rule() -> str:
    """The udev rule text :func:`linux_install` (``scope="system"``)
    writes to :data:`~mbtools.registry.paths.LINUX_UDEV_RULE_PATH`, to
    grant the operating (non-root) user access to the micro:bit DAPLink
    interface (VID:PID ``0d28:0204``). Moved verbatim from
    ``registry.cli`` (tickets 008/009) -- system-scope udev rules have no
    per-user/per-scope variant, unlike :func:`render_systemd_unit`, so
    this function's signature is unchanged.
    """
    return _UDEV_RULE_TEMPLATE.format(
        vendor_id=_USB_VENDOR_ID, product_id=_USB_PRODUCT_ID, group=_UDEV_GROUP
    )


def linux_install(
    scope: str,
    *,
    dry_run: bool = False,
    runner: CommandRunner | None = None,
    operating_user: str | None = None,
) -> Path:
    """Install the ``mbregistry`` systemd service at ``scope``.

    ``scope="system"``: write the unit + udev rule (skipped under
    ``dry_run``, same explicit gate :func:`macos_install` uses for its
    plist write), then run ``systemctl daemon-reload``, ``systemctl
    enable --now``, ``udevadm control --reload-rules``, ``udevadm
    trigger``, and — the behavior change from the old, print-only
    ``install-service`` — actually run ``usermod -aG plugdev
    <operating_user>`` through ``runner`` rather than merely printing
    it. ``operating_user`` resolves via
    :func:`_resolve_linux_operating_user` when not given. Prints the
    "log in again" follow-up note the same as the old command did.

    ``scope="user"``: a **preflight check** runs first, before writing
    anything — see :class:`LinuxUserPreflightError`. Only when the
    operating user is already in ``plugdev`` *and* the system-scope udev
    rule already exists does this write the user unit and run
    ``systemctl --user daemon-reload``, ``systemctl --user enable
    --now``, and ``loginctl enable-linger``.

    Returns the unit path written (or that would have been written,
    under ``dry_run``) on success. Raises :class:`LinuxUserPreflightError`
    for the ``--user`` refusal case — never a bare nonzero return, so a
    caller cannot mistake it for a `Path`. This function does no
    user-facing printing of its own beyond the preflight refusal's
    remediation text and the ``--system`` "log in again" note; ticket
    006-004 is what prints the rest.
    """
    _validate_linux_scope(scope)
    runner = runner if runner is not None else default_runner(dry_run=dry_run)

    if scope == "user":
        return _linux_install_user(
            dry_run=dry_run, runner=runner, operating_user=operating_user
        )
    return _linux_install_system(
        dry_run=dry_run, runner=runner, operating_user=operating_user
    )


def _linux_install_system(
    *, dry_run: bool, runner: CommandRunner, operating_user: str | None
) -> Path:
    unit_path = _linux_unit_path("system")
    udev_path = LINUX_UDEV_RULE_PATH
    resolved_user = _resolve_linux_operating_user(operating_user)

    if not dry_run:
        unit_path.parent.mkdir(parents=True, exist_ok=True)
        unit_path.write_text(render_systemd_unit(scope="system"))
        udev_path.parent.mkdir(parents=True, exist_ok=True)
        udev_path.write_text(render_udev_rule())

    runner.run(["systemctl", "daemon-reload"])
    runner.run(["systemctl", "enable", "--now", _LINUX_UNIT_NAME])
    runner.run(["udevadm", "control", "--reload-rules"])
    runner.run(["udevadm", "trigger"])
    runner.run(["usermod", "-aG", _UDEV_GROUP, resolved_user])

    print(
        f"mbregistry: added {resolved_user!r} to the {_UDEV_GROUP!r} group -- "
        "log in again (e.g. a new SSH connection) for this to take effect.",
        file=sys.stderr,
    )
    return unit_path


def _linux_install_user(
    *, dry_run: bool, runner: CommandRunner, operating_user: str | None
) -> Path:
    resolved_user = _resolve_linux_operating_user(operating_user)
    in_plugdev = _linux_user_in_plugdev(resolved_user)
    udev_rule_present = LINUX_UDEV_RULE_PATH.exists()

    if not in_plugdev or not udev_rule_present:
        lines = [
            f"mbregistry: cannot install --user for {resolved_user!r} yet -- "
            "USB access needs a one-time root-run setup first:",
        ]
        if not in_plugdev:
            lines.append(f"  sudo usermod -aG {_UDEV_GROUP} {resolved_user}")
        if not udev_rule_present:
            lines.append(
                "  sudo mbregistry service install --system  "
                "# writes the udev rule this --user install needs"
            )
        message = "\n".join(lines)
        print(message, file=sys.stderr)
        raise LinuxUserPreflightError(message)

    unit_path = _linux_unit_path("user")

    if not dry_run:
        unit_path.parent.mkdir(parents=True, exist_ok=True)
        unit_path.write_text(render_systemd_unit(scope="user"))

    runner.run(["systemctl", "--user", "daemon-reload"])
    runner.run(["systemctl", "--user", "enable", "--now", _LINUX_UNIT_NAME])
    runner.run(["loginctl", "enable-linger", resolved_user])

    return unit_path


def linux_uninstall(
    scope: str,
    *,
    purge: bool = False,
    dry_run: bool = False,
    runner: CommandRunner | None = None,
) -> str:
    """Uninstall the ``mbregistry`` systemd service at ``scope``: stop it
    (``systemctl [--user] disable --now``, tolerating the "not loaded"
    failure the same way :func:`macos_uninstall` does) and remove its
    unit file. ``scope="system"`` also removes the udev rule and runs
    ``systemctl daemon-reload`` + ``udevadm control --reload-rules``.
    Never touches ``plugdev`` membership at either scope, per this
    ticket's own acceptance criteria — only :func:`linux_install`
    (``scope="system"``) ever adds to that group.

    **Also removes the socket file** (the linked issue's uninstall
    behavior, same as :func:`macos_uninstall`'s own extension) —
    unconditionally on every real uninstall, not gated on ``purge``.
    Linux has no equivalent log *file* to remove: unlike the launchd
    plist's explicit ``StandardOutPath``, this module's systemd unit
    sets no ``StandardOutput=``/``StandardError=``, so ``journald``
    captures the service's output by default and there is no path this
    module ever wrote to clean up.

    Keeps ``devices.db`` unless ``purge=True``, in which case the whole
    state directory is removed too. Safe no-op (returns a message, never
    raises) when nothing is installed at ``scope`` — names the *other*
    scope in the message when it has an install, matching
    :func:`macos_uninstall`.
    """
    _validate_linux_scope(scope)
    unit_path = _linux_unit_path(scope)
    other_scope = "system" if scope == "user" else "user"
    other_unit_path = _linux_unit_path(other_scope)

    if not unit_path.exists():
        message = f"Linux {scope} service is not installed (no {unit_path})."
        if other_unit_path.exists():
            message += (
                f" A {other_scope}-scope install exists at {other_unit_path}."
            )
        return message

    runner = runner if runner is not None else default_runner(dry_run=dry_run)
    systemctl = ["systemctl", "--user"] if scope == "user" else ["systemctl"]

    try:
        runner.run([*systemctl, "disable", "--now", _LINUX_UNIT_NAME])
    except subprocess.CalledProcessError:
        pass  # already stopped/disabled -- not a real failure

    if not dry_run:
        unit_path.unlink(missing_ok=True)

        socket_path = system_socket_path() if scope == "system" else user_socket_path()
        socket_path.unlink(missing_ok=True)

    if scope == "system":
        if not dry_run:
            LINUX_UDEV_RULE_PATH.unlink(missing_ok=True)
        runner.run(["systemctl", "daemon-reload"])
        try:
            runner.run(["udevadm", "control", "--reload-rules"])
        except subprocess.CalledProcessError:
            pass

    if not dry_run and purge:
        db_path = system_db_path() if scope == "system" else user_db_path()
        state_dir = db_path.parent
        if state_dir.exists():
            shutil.rmtree(state_dir)

    return f"Removed Linux {scope} service ({unit_path})."


def linux_status(scope: str, *, runner: CommandRunner | None = None) -> ServiceStatus:
    """Report install/running state for ``scope``: ``installed`` is a
    plain filesystem check on the unit path; ``running`` (only checked
    when installed) comes from ``systemctl [--user] is-active
    mbregistry`` — any zero exit is treated as "active", mirroring
    :func:`macos_status`'s own "keep this simple and testable" shape.

    Defaults to a ``check=False`` :class:`SubprocessCommandRunner` (not
    :func:`default_runner`) when no ``runner`` is injected, for the same
    reason :func:`macos_status` does: a failing exit here is the
    ordinary "not currently active" outcome, not a dry-run concern.
    """
    _validate_linux_scope(scope)
    unit_path = _linux_unit_path(scope)
    installed = unit_path.exists()
    running = False
    if installed:
        systemctl = ["systemctl", "--user"] if scope == "user" else ["systemctl"]
        active_runner = runner if runner is not None else SubprocessCommandRunner(check=False)
        result = active_runner.run([*systemctl, "is-active", "mbregistry"])
        running = result.returncode == 0

    return ServiceStatus(scope=scope, path=unit_path, installed=installed, running=running)
