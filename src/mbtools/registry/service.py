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
    macos_launch_agent_path,
    macos_launch_daemon_path,
    macos_system_log_path,
    macos_user_log_path,
    system_db_path,
    user_db_path,
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
