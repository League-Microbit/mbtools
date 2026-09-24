"""Tests for mbtools.registry.service_windows -- the SCM command
rendering (ticket 004's own explicit scope) plus the ctypes-only
``StartServiceCtrlDispatcher`` primitive (:func:`run_as_windows_service`)
that makes the rendered commands actually correspond to a real,
non-1053-erroring Windows service; see the module's own docstring, "A
plain console app is not a real service", for why the latter exists.

Ticket 003/004 wrote this suite assuming it would only ever run off
real Windows; ticket 006 makes that no longer universally true (the
``windows-latest`` CI job runs it on real Windows too). Every
``_Win32ServiceAPI``-touching test still drives
:func:`run_as_windows_service` against a scripted fake standing in for
the ``ctypes.windll`` calls -- mirrors
``tests/registry/api_windows/test_api_windows.py``'s own fake-transport
approach -- and the two tests whose own subject is specifically the
*off-Windows* guard (``test_win32_service_api_requires_real_windows``/
``test_run_as_windows_service_without_a_fake_raises_off_windows``) now
force a non-``"win32"`` platform explicitly via ``monkeypatch`` rather
than assuming the ambient host, so they stay meaningful (and green) on
every CI leg. What is genuinely unverifiable here -- whether the real
``ctypes.windll.advapi32`` bindings inside ``_Win32ServiceAPI`` actually
match ``advapi32.dll``'s calling convention, and whether a real SCM
actually accepts the rendered ``sc.exe`` commands -- is explicitly out
of scope for this file; see the module's own docstring "What is not
proven" and ticket 006's ``windows-latest`` CI job.
"""

from __future__ import annotations

import subprocess
import sys

import pytest

from mbtools.common import EXIT_OK
from mbtools.registry import service_windows

# ---------------------------------------------------------------------------
# AC1: render_windows_service_install
# ---------------------------------------------------------------------------


def test_rendered_install_is_a_golden_file_for_required_directives():
    text = service_windows.render_windows_service_install()

    assert text.startswith(f"sc.exe create {service_windows.SERVICE_NAME} ")
    # invoked through the running interpreter, per render_systemd_unit's
    # own "not on the PATH systemd/SCM uses" precedent.
    assert f'binPath= "{sys.executable} -m mbtools.registry.cli run"' in text
    assert "start= auto" in text
    assert "DisplayName=" in text


def test_rendered_install_accepts_a_custom_exec_path():
    exec_path = r"C:\mbtools\venv\Scripts\python.exe -m mbtools.registry.cli run"
    text = service_windows.render_windows_service_install(exec_path=exec_path)
    assert f'binPath= "{exec_path}"' in text


def test_install_service_named_mbregistry_distinct_from_mbrelay():
    text = service_windows.render_windows_service_install()
    assert "mbregistry" in text
    assert "mbrelay" not in text


# ---------------------------------------------------------------------------
# AC2: render_windows_service_failure_actions
# ---------------------------------------------------------------------------


def test_rendered_failure_actions_is_a_golden_file():
    text = service_windows.render_windows_service_failure_actions()

    assert text.startswith(f"sc.exe failure {service_windows.SERVICE_NAME} ")
    # a real restart action, not a placeholder -- three restart/60000
    # legs (systemd Restart=on-failure equivalent, Decision 3) plus a
    # non-zero reset= window.
    assert "actions= restart/60000/restart/60000/restart/60000" in text
    assert "reset= 86400" in text
    assert "reset= 0" not in text


def test_rendered_failure_actions_accepts_a_custom_service_name():
    text = service_windows.render_windows_service_failure_actions(service_name="custom-svc")
    assert text.startswith("sc.exe failure custom-svc ")


# ---------------------------------------------------------------------------
# AC3: no actual SCM operation, no subprocess execution
# ---------------------------------------------------------------------------


def test_calling_render_functions_performs_no_subprocess_call(monkeypatch):
    def _forbidden(*args, **kwargs):
        raise AssertionError("render_* functions must never invoke subprocess")

    monkeypatch.setattr(subprocess, "run", _forbidden)
    monkeypatch.setattr(subprocess, "Popen", _forbidden)

    service_windows.render_windows_service_install()
    service_windows.render_windows_service_failure_actions()


# ---------------------------------------------------------------------------
# AC4: cmd_install_service_windows prints, never runs
# ---------------------------------------------------------------------------


def test_cmd_install_service_windows_prints_both_blocks_and_makes_no_subprocess_call(
    capsys, monkeypatch
):
    def _forbidden(*args, **kwargs):
        raise AssertionError("cmd_install_service_windows must not invoke subprocess")

    monkeypatch.setattr(subprocess, "run", _forbidden)
    monkeypatch.setattr(subprocess, "Popen", _forbidden)

    code = service_windows.cmd_install_service_windows()

    assert code == EXIT_OK
    err = capsys.readouterr().err
    assert service_windows.render_windows_service_install() in err
    assert service_windows.render_windows_service_failure_actions() in err


def test_cmd_install_service_windows_accepts_a_custom_exec_path(capsys):
    exec_path = r"C:\custom\mbregistry.exe run"
    service_windows.cmd_install_service_windows(exec_path)
    err = capsys.readouterr().err
    assert exec_path in err


# ---------------------------------------------------------------------------
# AC5: no pywin32 import
# ---------------------------------------------------------------------------


def test_no_pywin32_import():
    """AC: 'No pywin32 (or any other new third-party package) import
    anywhere in this module.' Checked by parsing the module's *actual
    import statements* via `ast` (not a substring search over the whole
    source -- this module's own identifiers, e.g. `_Win32ServiceAPI`,
    lowercase to something containing "win32service" and would
    false-positive a naive text search) -- mirrors
    `tests/registry/api_windows/test_api_windows.py`'s own
    `test_no_pywin32_import`.
    """
    import ast
    import inspect

    allowed_stdlib = {"__future__", "ctypes", "sys", "threading", "typing"}
    source = inspect.getsource(service_windows)
    tree = ast.parse(source)
    imported_roots: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                imported_roots.add(alias.name.split(".")[0])
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported_roots.add(node.module.split(".")[0])

    for root in imported_roots:
        assert root == "mbtools" or root in allowed_stdlib, (
            f"unexpected non-stdlib, non-mbtools import root: {root!r}"
        )
    forbidden_roots = {
        "win32api",
        "win32pipe",
        "win32file",
        "win32security",
        "win32service",
        "win32serviceutil",
        "pywintypes",
        "win32con",
    }
    assert imported_roots.isdisjoint(forbidden_roots)


# ---------------------------------------------------------------------------
# AC6: module docstring states the Decision-3 rationale
# ---------------------------------------------------------------------------


def test_module_docstring_states_decision_3_rationale():
    doc = service_windows.__doc__ or ""
    assert "Decision 3" in doc
    assert "sc.exe" in doc
    assert "Task Scheduler" in doc


# ---------------------------------------------------------------------------
# run_as_windows_service -- control flow proven against a fake win32 double
# (see module docstring, "A plain console app is not a real service")
# ---------------------------------------------------------------------------


class _FakeWin32ServiceAPI:
    """Stands in for `_Win32ServiceAPI`'s three-method surface, mirroring
    `api_windows.py`'s own fake-transport test-double role. `start_dispatcher`
    calls `service_main` synchronously -- exactly what a real SCM does from
    this process's point of view (it blocks inside
    `StartServiceCtrlDispatcherW` until the service stops), just without
    a real OS thread boundary.
    """

    def __init__(self) -> None:
        self.status_calls: list[tuple[int, int]] = []
        self.handler = None
        self.dispatcher_started = False

    def register_control_handler(self, handler) -> None:
        self.handler = handler

    def set_status(self, state, *, accepted=0) -> None:
        self.status_calls.append((state, accepted))

    def start_dispatcher(self, service_main) -> None:
        self.dispatcher_started = True
        service_main()


def test_run_as_windows_service_reports_running_then_stopped_and_calls_main():
    fake = _FakeWin32ServiceAPI()
    calls = []

    def main(stop_event):
        calls.append(stop_event.is_set())
        return 7

    code = service_windows.run_as_windows_service(main, win32=fake)

    assert code == 7
    assert calls == [False]  # stop was never signalled in this test
    assert fake.dispatcher_started
    assert fake.status_calls[0] == (
        service_windows._SERVICE_RUNNING,
        service_windows._SERVICE_ACCEPT_STOP,
    )
    assert fake.status_calls[-1] == (service_windows._SERVICE_STOPPED, 0)


def test_run_as_windows_service_stop_control_sets_event_and_reports_stop_pending():
    fake = _FakeWin32ServiceAPI()
    observed = {}

    def main(stop_event):
        # Simulates the SCM delivering SERVICE_CONTROL_STOP while `main`
        # is "running" -- `fake.handler` is populated by
        # `register_control_handler`, called before this callable runs.
        fake.handler(service_windows._SERVICE_CONTROL_STOP)
        observed["stop_event_set"] = stop_event.is_set()
        return 0

    service_windows.run_as_windows_service(main, win32=fake)

    assert observed["stop_event_set"] is True
    states = [state for state, _accepted in fake.status_calls]
    assert states == [
        service_windows._SERVICE_RUNNING,
        service_windows._SERVICE_STOP_PENDING,
        service_windows._SERVICE_STOPPED,
    ]


def test_run_as_windows_service_reports_stopped_even_if_main_raises():
    fake = _FakeWin32ServiceAPI()

    def main(stop_event):
        raise ValueError("boom")

    with pytest.raises(ValueError):
        service_windows.run_as_windows_service(main, win32=fake)

    assert fake.status_calls[-1] == (service_windows._SERVICE_STOPPED, 0)


def test_win32_service_api_requires_real_windows(monkeypatch):
    # Ticket 006: this file's own module docstring assumption ("every
    # test here runs off real Windows") no longer holds -- the
    # windows-latest CI job runs this suite on real Windows, where
    # sys.platform genuinely is "win32". This test's own subject is
    # _Win32ServiceAPI's *off-Windows* guard, so it forces a non-win32
    # value explicitly rather than relying on the ambient host platform
    # -- deterministic on every CI leg, including this one.
    monkeypatch.setattr(service_windows.sys, "platform", "linux")
    with pytest.raises(RuntimeError):
        service_windows._Win32ServiceAPI(service_windows.SERVICE_NAME)


def test_run_as_windows_service_without_a_fake_raises_off_windows(monkeypatch):
    monkeypatch.setattr(service_windows.sys, "platform", "linux")
    with pytest.raises(RuntimeError):
        service_windows.run_as_windows_service(lambda stop_event: 0)
