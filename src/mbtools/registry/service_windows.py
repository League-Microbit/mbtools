"""mbtools.registry.service_windows -- the Windows counterpart to
``mbtools.registry.cli``'s systemd-unit rendering (``render_systemd_unit``)
and install-service command (``cmd_install_service``): renders the
Windows Service Control Manager (SCM) commands that register
``mbregistry`` as a restart-on-failure service (sprint.md brief §3.1 /
spec §3.8, this sprint's ticket 004), and provides the one additional
primitive that lets ``mbregistry`` actually *behave* like an SCM service
once those commands are run for real -- see "A plain console app is not
a real service" below.

**Decision 3 (SCM via ``sc.exe``, not Task Scheduler)**: ``sc.exe
create`` plus ``sc.exe failure ... actions=
restart/60000/restart/60000/restart/60000`` gives a direct SCM
equivalent of the systemd unit's ``Restart=on-failure`` -- the same "the
OS restarts it if it dies" guarantee, and the same
``Get-Service``/``services.msc`` observability an operator used to
systemd units would look for, that a Task Scheduler task (a bounded
number of retries on the *task*, invisible to ``Get-Service``) does not
give.

**Decision 1 (no ``pywin32``)**: ``sc.exe`` ships with every Windows
install, the same way ``systemctl``/``udevadm`` ship with every systemd
Linux install this project already assumes; the one ``ctypes`` surface
this module needs beyond plain text (see below) uses the same
standard-library-only ``ctypes.windll`` approach
``registry.api_windows`` already established for the named-pipe API,
never ``pywin32``.

**A plain console app is not a real service.** Registering
``python -m mbtools.registry.cli run`` (or any other bare console-mode
process) with ``sc.exe create`` is not, by itself, sufficient: the
Windows Service Control Manager expects the process it starts to call
``StartServiceCtrlDispatcherW`` within roughly 30 seconds and report
``SERVICE_RUNNING`` through ``RegisterServiceCtrlHandlerExW``/
``SetServiceStatus``; a process that never makes these calls -- which is
exactly what ``mbregistry run``'s current implementation does, and all
this ticket's own rendered ``binPath=`` text invokes -- is killed by the
SCM with error 1053 ("The service did not respond to the start or
control request in a timely fashion") a few seconds after ``sc.exe``
starts it. This is a real Windows constraint, not a hypothetical edge
case, and unlike the systemd case (where any ordinary foreground process
is a valid ``ExecStart=`` target) it means the rendered SCM commands are
only genuinely usable once whatever they invoke actually calls the
dispatcher.

This ticket's own scope is still the *rendered command text*
(:func:`render_windows_service_install`/
:func:`render_windows_service_failure_actions`) and the *print, don't
execute* entry point (:func:`cmd_install_service_windows`) -- exactly
mirroring ``render_systemd_unit``/``cmd_install_service``'s existing
shape, which is pure text/printing and never itself starts anything
either. **Choice**: this module additionally provides
:func:`run_as_windows_service`, a minimal, standard-library-only
(``ctypes``) implementation of the
``StartServiceCtrlDispatcherW``/``RegisterServiceCtrlHandlerExW``/
``SetServiceStatus`` sequence, built the same way
``registry.api_windows._Win32PipeAPI`` wraps its own Win32 calls: a real
implementation that only ever runs on actual Windows, behind an
injectable ``win32=`` seam so its control flow (status transitions, the
stop-event wiring) is exercisable with a fake on macOS/Linux CI -- see
``tests/registry/service_windows/test_service_windows.py``. Ticket 005
("wire windows platform support into mbregistry run / install-service")
is where ``registry.cli.cmd_run``'s own Windows branch calls this
function to wrap the daemon's actual poll loop; this ticket only builds
and tests the primitive, exactly as this ticket's own Implementation
Notes ask for ticket 005's wiring to be "a thin dispatch, not new
logic".

**Alternative considered and rejected**: documenting an external
service-wrapper tool (e.g. NSSM/WinSW) instead of implementing the
dispatcher call directly -- rejected because it would add a new,
non-Python, fleet-wide *binary* dependency, which is exactly the concern
Decision 1 was written to avoid ("a new dependency installed on every
Windows fleet node") -- that concern applies to a bundled wrapper
``.exe`` just as much as to a Python package -- whereas
``ctypes.windll.advapi32`` costs nothing new and is the same category of
call ``api_windows.py`` already makes for the named-pipe transport.

**Import-safety**: every ``ctypes.windll``/``ctypes.WINFUNCTYPE`` lookup
in this module happens lazily, inside :class:`_Win32ServiceAPI`'s own
methods -- never at module import time (unlike ``api_windows.py``'s
module-scope ``if sys.platform == "win32": _kernel32 =
ctypes.windll.kernel32`` guard). That keeps this module reimport-safe
under the *simulated*-Windows sweep in
``tests/test_windows_import_safety.py`` with no exclusion needed there
(contrast that test module's own docstring, which explains why
``api_windows.py`` could not avoid one): nothing in ``service_windows``
touches ``ctypes.windll``/``ctypes.WINFUNCTYPE`` merely by being
imported or reloaded, on any platform, simulated or real.

**What this proves without Windows hardware**: the rendered ``sc.exe
create``/``sc.exe failure`` command text is deterministic and
inspectable (this module's own tests), and :func:`run_as_windows_service`'s
control flow (status transitions, the stop-event set on a simulated
``SERVICE_CONTROL_STOP``) is proven against a fake ``win32`` double.
**What is not proven**: that the real ``ctypes.windll.advapi32``
bindings inside :class:`_Win32ServiceAPI` actually match
``advapi32.dll``'s calling convention, or that a real SCM actually
accepts the rendered commands -- both need the ``windows-latest`` CI job
(ticket 006); flagged "not hardware-verified" until then, mirroring
``api_windows.py``'s own caveat.
"""

from __future__ import annotations

import ctypes
import ctypes.wintypes as _wintypes
import sys
import threading
from typing import Callable

from mbtools.common import EXIT_OK

__all__ = [
    "SERVICE_NAME",
    "render_windows_service_install",
    "render_windows_service_failure_actions",
    "cmd_install_service_windows",
    "run_as_windows_service",
]

#: The service name every rendered command and :func:`run_as_windows_service`
#: use by default -- distinct from the legacy ``mbrelay`` service, same
#: "named after the daemon it registers" precedent as the Linux unit
#: (``mbregistry.service``, not ``mbrelay.service`` -- see
#: ``tests/registry/cli/test_cli_install_service.py``'s own
#: ``test_unit_is_named_mbregistry_service_distinct_from_mbrelay_service``).
SERVICE_NAME = "mbregistry"

_DISPLAY_NAME = "mbtools micro:bit registry daemon"

#: ``sc.exe create`` command text template. ``binPath=`` and the other
#: ``key= value`` pairs each need the literal space between ``=`` and the
#: value -- an ``sc.exe`` parsing quirk (unlike most Windows CLIs), and a
#: common source of "The service name is invalid" install failures if
#: dropped.
_SERVICE_INSTALL_TEMPLATE = (
    'sc.exe create {service_name} binPath= "{exec_path}" '
    'start= auto DisplayName= "{display_name}"'
)

#: ``sc.exe failure`` command text template -- the systemd
#: ``Restart=on-failure`` equivalent (Decision 3): three restart actions
#: spaced 60000ms (60s) apart, with the failure count reset after 86400s
#: (24h) of continuous uptime, mirroring systemd's own default
#: "restart, but don't restart-loop forever on a permanently broken
#: install" shape.
_SERVICE_FAILURE_TEMPLATE = (
    "sc.exe failure {service_name} "
    "actions= restart/60000/restart/60000/restart/60000 reset= 86400"
)


def render_windows_service_install(exec_path: str | None = None) -> str:
    """The ``sc.exe create`` command text that registers ``mbregistry``
    as an auto-start SCM service -- the Windows analog of
    ``render_systemd_unit()``'s returned unit-file text.

    ``exec_path`` mirrors ``render_systemd_unit``'s ``exec_start``
    parameter exactly, including its default: when omitted, it resolves
    to ``{sys.executable} -m mbtools.registry.cli run``, invoked through
    the *running* interpreter rather than a bare PATH lookup, for the
    same reason ``render_systemd_unit``'s own docstring gives -- mbtools
    is typically installed into an isolated venv whose ``bin\\``/
    ``Scripts\\`` directory is not on the PATH the SCM starts services
    with, but the package is always importable through the interpreter
    that installed it.
    """
    if exec_path is None:
        exec_path = f"{sys.executable} -m mbtools.registry.cli run"
    return _SERVICE_INSTALL_TEMPLATE.format(
        service_name=SERVICE_NAME, exec_path=exec_path, display_name=_DISPLAY_NAME
    )


def render_windows_service_failure_actions(service_name: str = SERVICE_NAME) -> str:
    """The ``sc.exe failure`` command text that sets ``mbregistry``'s
    restart-on-failure policy -- the systemd ``Restart=on-failure``
    equivalent (Decision 3). See :data:`_SERVICE_FAILURE_TEMPLATE`'s own
    comment for what each action/reset value means.
    """
    return _SERVICE_FAILURE_TEMPLATE.format(service_name=service_name)


def cmd_install_service_windows(exec_path: str | None = None) -> int:
    """``mbregistry install-service``'s Windows entry point -- prints
    (never runs) the two rendered ``sc.exe`` command blocks, exactly
    mirroring ``cmd_install_service``'s existing "print, don't execute"
    shape for the Linux systemd/udev commands: safe to exercise without
    Administrator privilege or a real SCM, in a test or in CI, matching
    sprint.md's Test Strategy.

    Deliberately a plain, directly testable function -- not
    argparse-shaped -- so ticket 005's wiring of
    ``cmd_install_service``'s ``sys.platform == "win32"`` branch into
    ``registry.cli``'s dispatch is a thin call into this function, not
    new logic (this ticket's own Implementation Notes).
    """
    install_text = render_windows_service_install(exec_path)
    failure_text = render_windows_service_failure_actions()

    print("Run as Administrator to register the service:", file=sys.stderr)
    print(f"  {install_text}", file=sys.stderr)
    print(
        "Run as Administrator to set the restart-on-failure policy "
        "(systemd Restart=on-failure equivalent):",
        file=sys.stderr,
    )
    print(f"  {failure_text}", file=sys.stderr)
    print(
        f"Run as Administrator to start it once installed: "
        f"sc.exe start {SERVICE_NAME}",
        file=sys.stderr,
    )
    return EXIT_OK


# ---------------------------------------------------------------------------
# run_as_windows_service -- the StartServiceCtrlDispatcher primitive
# (see module docstring, "A plain console app is not a real service")
# ---------------------------------------------------------------------------

_SERVICE_CONTROL_STOP = 0x00000001
_SERVICE_ACCEPT_STOP = 0x00000001
_SERVICE_WIN32_OWN_PROCESS = 0x00000010
_SERVICE_STOP_PENDING = 0x00000003
_SERVICE_RUNNING = 0x00000004
_SERVICE_STOPPED = 0x00000001
_NO_ERROR = 0


class _SERVICE_STATUS(ctypes.Structure):
    """Win32 ``SERVICE_STATUS`` layout -- built from plain
    ``ctypes.wintypes`` aliases only, so (unlike the real dispatcher
    calls in :class:`_Win32ServiceAPI` below) this class exists
    identically on every platform; only *using* one against a real
    ``SetServiceStatus`` needs the real ``advapi32`` binding.
    """

    _fields_ = [
        ("dwServiceType", _wintypes.DWORD),
        ("dwCurrentState", _wintypes.DWORD),
        ("dwControlsAccepted", _wintypes.DWORD),
        ("dwWin32ExitCode", _wintypes.DWORD),
        ("dwServiceSpecificExitCode", _wintypes.DWORD),
        ("dwCheckPoint", _wintypes.DWORD),
        ("dwWaitHint", _wintypes.DWORD),
    ]


class _Win32ServiceAPI:
    """The real, ``ctypes``-backed
    ``StartServiceCtrlDispatcherW``/``RegisterServiceCtrlHandlerExW``/
    ``SetServiceStatus`` sequence -- only ever constructed on real
    Windows (see :func:`run_as_windows_service`'s own ``win32=None``
    default, and this class's own constructor guard). Every
    ``ctypes.windll``/``ctypes.WINFUNCTYPE`` lookup happens lazily inside
    these methods, never at module import time -- see the module
    docstring's "Import-safety". Tests substitute a fake implementing
    this same three-method surface (``register_control_handler``/
    ``set_status``/``start_dispatcher``) -- see
    ``tests/registry/service_windows/test_service_windows.py`` -- never
    exercising real ``ctypes.windll``/``ctypes.WINFUNCTYPE`` here, since
    no Windows hardware exists for this project (module docstring's
    "What is not proven").
    """

    def __init__(self, service_name: str) -> None:
        if sys.platform != "win32":
            raise RuntimeError(
                "_Win32ServiceAPI: requires sys.platform == 'win32' "
                "(inject a fake `win32=` for tests off real Windows)"
            )
        self._service_name = service_name
        self._advapi32 = ctypes.windll.advapi32
        self._status_handle: int | None = None
        self._handler_ref: object | None = None  # kept alive -- see register_control_handler

    def register_control_handler(self, handler: Callable[[int], None]) -> None:
        """Registers ``handler`` (called with the raw Win32 control
        code, e.g. :data:`_SERVICE_CONTROL_STOP`) as this service's
        control handler via ``RegisterServiceCtrlHandlerExW``.
        """
        handler_ex_type = ctypes.WINFUNCTYPE(
            _wintypes.DWORD, _wintypes.DWORD, _wintypes.DWORD, ctypes.c_void_p, ctypes.c_void_p
        )

        def _handler_ex(control: int, _event_type: int, _event_data: int, _context: int) -> int:
            handler(control)
            return _NO_ERROR

        # ctypes does not keep a Python-side reference to a callback
        # alive on its own -- stashing it on `self` is what stops it
        # from being garbage-collected out from under the SCM while the
        # service is still running.
        self._handler_ref = handler_ex_type(_handler_ex)
        self._advapi32.RegisterServiceCtrlHandlerExW.restype = _wintypes.HANDLE
        self._status_handle = self._advapi32.RegisterServiceCtrlHandlerExW(
            self._service_name, self._handler_ref, None
        )
        if not self._status_handle:
            raise OSError("service_windows: RegisterServiceCtrlHandlerExW failed")

    def set_status(self, state: int, *, accepted: int = 0) -> None:
        """Reports ``state`` (e.g. :data:`_SERVICE_RUNNING`) to the SCM
        via ``SetServiceStatus``. ``accepted`` is the bitmask of control
        codes the service currently handles (e.g.
        :data:`_SERVICE_ACCEPT_STOP`) -- zero while starting/stopping,
        matching the Win32 convention that a service must not claim to
        accept a control it is not yet, or no longer, ready to act on.
        """
        status = _SERVICE_STATUS()
        status.dwServiceType = _SERVICE_WIN32_OWN_PROCESS
        status.dwCurrentState = state
        status.dwControlsAccepted = accepted
        status.dwWin32ExitCode = _NO_ERROR
        status.dwServiceSpecificExitCode = 0
        status.dwCheckPoint = 0
        status.dwWaitHint = 0
        if not self._advapi32.SetServiceStatus(self._status_handle, ctypes.byref(status)):
            raise OSError("service_windows: SetServiceStatus failed")

    def start_dispatcher(self, service_main: Callable[[], None]) -> None:
        """Calls ``StartServiceCtrlDispatcherW`` with a one-entry service
        table naming :data:`SERVICE_NAME` and ``service_main`` as its
        ``ServiceMain`` callback -- this is the call that actually tells
        the SCM "this process is a real service, ready to be dispatched
        to". On real Windows it blocks until every registered service
        has stopped.
        """
        service_main_type = ctypes.WINFUNCTYPE(
            None, _wintypes.DWORD, ctypes.POINTER(_wintypes.LPWSTR)
        )

        class _SERVICE_TABLE_ENTRY(ctypes.Structure):
            _fields_ = [
                ("lpServiceName", _wintypes.LPWSTR),
                ("lpServiceProc", service_main_type),
            ]

        def _service_main(_argc: int, _argv: object) -> None:
            service_main()

        callback = service_main_type(_service_main)
        # A NULL-terminated table: one real entry, then an all-NULL
        # sentinel entry -- exactly what StartServiceCtrlDispatcherW's
        # own contract requires to know where the table ends.
        table = (_SERVICE_TABLE_ENTRY * 2)(
            _SERVICE_TABLE_ENTRY(self._service_name, callback),
            _SERVICE_TABLE_ENTRY(None, service_main_type()),
        )
        if not self._advapi32.StartServiceCtrlDispatcherW(table):
            raise OSError("service_windows: StartServiceCtrlDispatcherW failed")


def run_as_windows_service(
    main: Callable[[threading.Event], int],
    *,
    service_name: str = SERVICE_NAME,
    win32: "_Win32ServiceAPI | object | None" = None,
) -> int:
    """Runs ``main`` as a real SCM-recognized Windows service: registers
    a control handler, reports ``SERVICE_RUNNING``, calls
    ``main(stop_event)``, and reports ``SERVICE_STOPPED`` once it
    returns -- see module docstring, "A plain console app is not a real
    service".

    ``main`` is called exactly once, given a :class:`threading.Event`
    this function sets when the SCM delivers ``SERVICE_CONTROL_STOP``;
    ``main`` is responsible for noticing the event (e.g. in its own poll
    loop) and returning its own exit code -- the same "poll loop notices
    a stop signal" shape ``registry.cli.cmd_run``'s own
    SIGINT/SIGTERM handling already has on POSIX. Ticket 005 is where
    that Windows equivalent gets wired into ``cmd_run`` itself, calling
    this function with a ``main`` that runs the daemon's actual poll
    loop.

    ``win32`` is the injectable transport double: production code (real
    Windows only) leaves it ``None``, which constructs a real
    :class:`_Win32ServiceAPI` (and raises :class:`RuntimeError` off real
    Windows, since that constructor requires ``sys.platform ==
    "win32"``); every test supplies a fake implementing the same
    three-method surface (``register_control_handler``/``set_status``/
    ``start_dispatcher``) to exercise this function's control flow --
    status transitions and the stop-event wiring -- without any real
    Windows API, mirroring ``api_windows.py``'s own ``win32=`` seam.
    """
    api = win32 if win32 is not None else _Win32ServiceAPI(service_name)
    stop_event = threading.Event()
    result: dict[str, int] = {"code": 0}

    def _handler(control: int) -> None:
        if control == _SERVICE_CONTROL_STOP:
            api.set_status(_SERVICE_STOP_PENDING)
            stop_event.set()

    def _service_main() -> None:
        api.register_control_handler(_handler)
        api.set_status(_SERVICE_RUNNING, accepted=_SERVICE_ACCEPT_STOP)
        try:
            result["code"] = main(stop_event)
        finally:
            api.set_status(_SERVICE_STOPPED)

    api.start_dispatcher(_service_main)
    return result["code"]
