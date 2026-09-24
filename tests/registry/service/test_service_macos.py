"""Tests for mbtools.registry.service's macOS (launchd) support (ticket
006-002): plist rendering plus install/uninstall/status orchestration.

Per sprint.md's Test Strategy and this ticket's own acceptance criteria,
no test here invokes a real ``launchctl`` or writes to a real
``~/Library``/``/Library`` path. Every path a test touches is redirected
into ``tmp_path`` first:

- ``HOME`` is monkeypatched (env var), which is what
  ``registry.paths``' ``_home()`` (and ``pathlib.Path.home()``, which
  ``render_launchd_plist``'s ``WorkingDirectory`` uses for the user
  scope) both resolve against on every platform this suite runs on,
  including Windows (``ntpath.expanduser`` checks ``HOME`` first) --
  same convention ``tests/registry/paths/test_paths.py`` already uses.
- The two *system*-scope path helpers
  (``macos_launch_daemon_path``/``macos_system_log_path``) have no env
  override in ``registry.paths`` at all (they are fixed
  ``/Library/...`` paths by design) -- those are monkeypatched directly
  on ``service_module`` instead, the same "fake only at the true
  boundary" approach ``test_service_runner.py`` uses for
  ``subprocess.run``.
- ``os.getuid`` is monkeypatched with ``raising=False`` so the "user"
  scope's ``gui/<uid>`` domain is deterministic even on a platform
  (Windows) where ``os.getuid`` does not exist at all -- matching
  ``_macos_domain``'s own ``getattr(os, "getuid", None)`` guard.

Every ``launchctl`` invocation goes through a hand-rolled
:class:`CommandRunner` fake, never a real mock library, matching this
package's existing convention (``test_service_runner.py``'s
``_FakeRun``).
"""

from __future__ import annotations

import plistlib
import subprocess
import sys

import pytest

import mbtools.registry.service as service_module
from mbtools.registry.service import (
    ServiceStatus,
    macos_install,
    macos_status,
    macos_uninstall,
    render_launchd_plist,
)


# ---------------------------------------------------------------------------
# Fixtures / fakes
# ---------------------------------------------------------------------------


@pytest.fixture
def macos_paths(monkeypatch, tmp_path):
    """Redirect every macOS path this ticket's functions touch into
    ``tmp_path``, and fix the "user" scope's uid so ``gui/<uid>`` is
    deterministic. Returns a small namespace of the resolved paths so
    tests can assert against them directly."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("USERPROFILE", raising=False)

    daemon_plist = tmp_path / "Library" / "LaunchDaemons" / "org.jointheleague.mbregistry.plist"
    daemon_log = tmp_path / "Library" / "Logs" / "mbregistry.log"
    monkeypatch.setattr(service_module, "macos_launch_daemon_path", lambda: daemon_plist)
    monkeypatch.setattr(service_module, "macos_system_log_path", lambda: daemon_log)

    system_db = tmp_path / "Library" / "Application Support" / "mbregistry" / "devices.db"
    monkeypatch.setattr(service_module, "system_db_path", lambda: system_db)

    monkeypatch.setattr(service_module.os, "getuid", lambda: 501, raising=False)

    agent_plist = home / "Library" / "LaunchAgents" / "org.jointheleague.mbregistry.plist"
    agent_log = home / "Library" / "Logs" / "mbregistry.log"
    user_db = home / "Library" / "Application Support" / "mbregistry" / "devices.db"
    monkeypatch.setattr(service_module, "user_db_path", lambda: user_db)

    class _Paths:
        pass

    p = _Paths()
    p.home = home
    p.user_plist = agent_plist
    p.user_log = agent_log
    p.user_db = user_db
    p.system_plist = daemon_plist
    p.system_log = daemon_log
    p.system_db = system_db
    return p


class _FakeRunner:
    """A :class:`CommandRunner` fake that records every call and, when
    ``fail_argvs`` names one, raises :class:`subprocess.CalledProcessError`
    instead of "succeeding" -- enough to exercise the idempotent
    bootout-then-enable-then-bootstrap sequence without a real
    ``launchctl``."""

    def __init__(self, *, fail_argvs: set[tuple[str, ...]] | None = None):
        self.calls: list[list[str]] = []
        self._fail_argvs = fail_argvs or set()

    def run(self, argv: list[str]) -> subprocess.CompletedProcess[str]:
        self.calls.append(list(argv))
        if tuple(argv) in self._fail_argvs:
            raise subprocess.CalledProcessError(1, argv)
        return subprocess.CompletedProcess(argv, 0, "", "")


class _StatefulLaunchctl:
    """A closer-to-real fake: tracks whether the label is "loaded" and
    fails ``bootout``/``bootstrap`` exactly the way real ``launchctl``
    would (bootout fails when nothing is loaded; bootstrap fails when
    something already is) -- used by the re-install idempotency test."""

    def __init__(self) -> None:
        self.calls: list[list[str]] = []
        self.loaded = False

    def run(self, argv: list[str]) -> subprocess.CompletedProcess[str]:
        self.calls.append(list(argv))
        verb = argv[1]
        if verb == "bootout":
            if not self.loaded:
                raise subprocess.CalledProcessError(1, argv)
            self.loaded = False
        elif verb == "bootstrap":
            if self.loaded:
                raise subprocess.CalledProcessError(1, argv)
            self.loaded = True
        elif verb == "print":
            return subprocess.CompletedProcess(argv, 0 if self.loaded else 1, "", "")
        return subprocess.CompletedProcess(argv, 0, "", "")


# ---------------------------------------------------------------------------
# render_launchd_plist -- golden-file (round-tripped through plistlib)
# ---------------------------------------------------------------------------


def test_render_launchd_plist_rejects_unknown_scope(macos_paths):
    with pytest.raises(ValueError):
        render_launchd_plist("bogus")


def test_render_launchd_plist_user_matches_docs_directives(macos_paths):
    text = render_launchd_plist("user", exec_path="/opt/venv/bin/python")
    parsed = plistlib.loads(text.encode("utf-8"))

    assert parsed["Label"] == "org.jointheleague.mbregistry"
    assert parsed["ProgramArguments"] == [
        "/opt/venv/bin/python",
        "-m",
        "mbtools.registry.cli",
        "run",
    ]
    assert parsed["KeepAlive"] == {"SuccessfulExit": False}
    assert parsed["ThrottleInterval"] == 10
    assert parsed["RunAtLoad"] is True
    assert parsed["WorkingDirectory"] == str(macos_paths.home)
    assert parsed["StandardOutPath"] == str(macos_paths.user_log)
    assert parsed["StandardErrorPath"] == str(macos_paths.user_log)


def test_render_launchd_plist_system_matches_docs_directives(macos_paths):
    text = render_launchd_plist("system", exec_path="/opt/mbtools/bin/python")
    parsed = plistlib.loads(text.encode("utf-8"))

    assert parsed["Label"] == "org.jointheleague.mbregistry"
    assert parsed["ProgramArguments"] == [
        "/opt/mbtools/bin/python",
        "-m",
        "mbtools.registry.cli",
        "run",
    ]
    assert parsed["KeepAlive"] == {"SuccessfulExit": False}
    assert parsed["ThrottleInterval"] == 10
    assert parsed["WorkingDirectory"] == "/"
    assert parsed["StandardOutPath"] == str(macos_paths.system_log)
    assert parsed["StandardErrorPath"] == str(macos_paths.system_log)


def test_render_launchd_plist_defaults_exec_path_to_current_interpreter(macos_paths):
    parsed = plistlib.loads(render_launchd_plist("user").encode("utf-8"))
    assert parsed["ProgramArguments"][0] == sys.executable


# ---------------------------------------------------------------------------
# macos_install
# ---------------------------------------------------------------------------


def test_macos_install_user_writes_plist_and_calls_expected_sequence(macos_paths):
    runner = _FakeRunner()
    result_path = macos_install("user", dry_run=False, runner=runner)

    assert result_path == macos_paths.user_plist
    assert macos_paths.user_plist.exists()

    label = "org.jointheleague.mbregistry"
    domain = "gui/501"
    assert runner.calls == [
        ["launchctl", "bootout", f"{domain}/{label}"],
        ["launchctl", "enable", f"{domain}/{label}"],
        ["launchctl", "bootstrap", domain, str(macos_paths.user_plist)],
    ]


def test_macos_install_system_writes_plist_and_calls_expected_sequence(macos_paths):
    runner = _FakeRunner()
    result_path = macos_install("system", dry_run=False, runner=runner)

    assert result_path == macos_paths.system_plist
    assert macos_paths.system_plist.exists()

    label = "org.jointheleague.mbregistry"
    assert runner.calls == [
        ["launchctl", "bootout", f"system/{label}"],
        ["launchctl", "enable", f"system/{label}"],
        ["launchctl", "bootstrap", "system", str(macos_paths.system_plist)],
    ]


def test_macos_install_tolerates_bootout_failure_on_fresh_install(macos_paths):
    label = "org.jointheleague.mbregistry"
    domain = "gui/501"
    runner = _FakeRunner(fail_argvs={("launchctl", "bootout", f"{domain}/{label}")})

    # Must not raise even though bootout fails (nothing was loaded yet).
    macos_install("user", dry_run=False, runner=runner)

    assert runner.calls[1] == ["launchctl", "enable", f"{domain}/{label}"]
    assert runner.calls[2] == ["launchctl", "bootstrap", domain, str(macos_paths.user_plist)]


def test_macos_install_dry_run_writes_nothing_and_prints_would_run(macos_paths, capsys):
    result_path = macos_install("user", dry_run=True, runner=None)

    assert result_path == macos_paths.user_plist
    assert not macos_paths.user_plist.exists()

    captured = capsys.readouterr()
    assert "would run: launchctl bootout" in captured.err
    assert "would run: launchctl enable" in captured.err
    assert "would run: launchctl bootstrap" in captured.err


def test_macos_install_reinstall_with_same_runner_is_idempotent(macos_paths):
    runner = _StatefulLaunchctl()

    macos_install("user", dry_run=False, runner=runner)
    assert runner.loaded is True

    # Re-running against the same (now "already loaded") runner must not
    # raise, and must end in the same loaded state.
    macos_install("user", dry_run=False, runner=runner)
    assert runner.loaded is True

    label = "org.jointheleague.mbregistry"
    domain = "gui/501"
    bootstrap_argv = ["launchctl", "bootstrap", domain, str(macos_paths.user_plist)]
    assert runner.calls.count(bootstrap_argv) == 2


# ---------------------------------------------------------------------------
# macos_uninstall
# ---------------------------------------------------------------------------


def test_macos_uninstall_removes_plist_and_calls_bootout(macos_paths):
    macos_install("user", dry_run=False, runner=_FakeRunner())
    assert macos_paths.user_plist.exists()

    runner = _FakeRunner()
    message = macos_uninstall("user", runner=runner)

    assert not macos_paths.user_plist.exists()
    label = "org.jointheleague.mbregistry"
    domain = "gui/501"
    assert runner.calls == [["launchctl", "bootout", f"{domain}/{label}"]]
    assert str(macos_paths.user_plist) in message


def test_macos_uninstall_keeps_devices_db_without_purge(macos_paths):
    macos_paths.user_db.parent.mkdir(parents=True)
    macos_paths.user_db.write_text("data")
    macos_install("user", dry_run=False, runner=_FakeRunner())

    macos_uninstall("user", purge=False, runner=_FakeRunner())

    assert macos_paths.user_db.exists()


def test_macos_uninstall_purge_removes_state_directory(macos_paths):
    macos_paths.user_db.parent.mkdir(parents=True)
    macos_paths.user_db.write_text("data")
    macos_install("user", dry_run=False, runner=_FakeRunner())

    macos_uninstall("user", purge=True, runner=_FakeRunner())

    assert not macos_paths.user_db.exists()
    assert not macos_paths.user_db.parent.exists()


def test_macos_uninstall_is_a_noop_when_nothing_installed(macos_paths):
    runner = _FakeRunner()
    message = macos_uninstall("user", runner=runner)

    assert runner.calls == []
    assert "not installed" in message


def test_macos_uninstall_noop_message_names_other_scope_if_installed(macos_paths):
    macos_install("system", dry_run=False, runner=_FakeRunner())

    message = macos_uninstall("user", runner=_FakeRunner())

    assert "not installed" in message
    assert str(macos_paths.system_plist) in message


# ---------------------------------------------------------------------------
# macos_status
# ---------------------------------------------------------------------------


def test_macos_status_not_installed(macos_paths):
    status = macos_status("user", runner=_FakeRunner())

    assert status == ServiceStatus(
        scope="user", path=macos_paths.user_plist, installed=False, running=False
    )
    assert status.describe() == "not installed"


def test_macos_status_installed_but_not_running(macos_paths):
    macos_install("user", dry_run=False, runner=_StatefulLaunchctl())
    # Simulate "loaded=False" by asking status with a runner whose
    # `print` reports a nonzero (not-loaded) exit.

    class _NotLoadedRunner:
        def run(self, argv):
            return subprocess.CompletedProcess(argv, 1, "", "not loaded")

    status = macos_status("user", runner=_NotLoadedRunner())

    assert status.installed is True
    assert status.running is False
    assert status.describe() == "installed but not running"


def test_macos_status_installed_and_running(macos_paths):
    macos_install("user", dry_run=False, runner=_StatefulLaunchctl())

    class _LoadedRunner:
        def run(self, argv):
            return subprocess.CompletedProcess(argv, 0, "", "")

    status = macos_status("user", runner=_LoadedRunner())

    assert status.installed is True
    assert status.running is True
    assert status.describe() == "installed and running"


def test_macos_status_defaults_to_check_false_subprocess_runner(macos_paths, monkeypatch):
    macos_install("user", dry_run=False, runner=_StatefulLaunchctl())

    calls: list[tuple[list[str], dict]] = []

    def fake_run(argv, **kwargs):
        calls.append((argv, kwargs))
        return subprocess.CompletedProcess(argv, 1, "", "")

    monkeypatch.setattr(service_module.subprocess, "run", fake_run)

    status = macos_status("user", runner=None)

    assert status.installed is True
    assert status.running is False
    assert calls[0][1]["check"] is False


def test_macos_status_never_calls_runner_when_not_installed(macos_paths):
    runner = _FakeRunner()
    macos_status("user", runner=runner)
    assert runner.calls == []


# ---------------------------------------------------------------------------
# Platform-branch safety: nothing here relies on the real host platform.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("real_platform", ["linux", "darwin", "win32"])
def test_macos_functions_dont_depend_on_sys_platform(macos_paths, monkeypatch, real_platform):
    """These functions are macOS-specific by construction (ticket
    006-004 is what dispatches on ``sys.platform`` in ``registry.cli``)
    -- they never branch on it themselves, so forcing it to each of the
    three platforms here must not change behavior, keeping this whole
    suite safe to run on Linux/Windows CI, exactly like
    ``tests/registry/cli/test_cli_install_service.py`` already does for
    the Linux systemd branch.
    """
    monkeypatch.setattr(service_module.sys, "platform", real_platform)

    result_path = macos_install("user", dry_run=False, runner=_FakeRunner())
    assert result_path == macos_paths.user_plist
    assert macos_paths.user_plist.exists()
