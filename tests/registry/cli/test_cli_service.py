"""Tests for ``mbregistry service install|uninstall|status`` (ticket
006-004) -- the CLI wiring into ``registry.service``'s per-platform
orchestration functions (tickets 006-002/003).

Every test drives ``main([...])`` (or, for a couple of pure dispatch/
argument-forwarding checks, ``cli.cmd_service_*`` directly), matching
``tests/registry/cli/test_cli_install_service.py``'s own convention.
``sys.platform`` is forced via monkeypatch so both the macOS and the
Linux orchestration path are exercised on every CI leg, regardless of
which OS actually runs the suite -- the same reasoning
``test_cli_install_service.py``'s existing tests already use.

No test here invokes a real ``launchctl``/``systemctl``/``udevadm``/
``usermod``/``loginctl``, per sprint.md's Test Strategy: every real
(non-``--dry-run``) install/uninstall/status call goes through a
recording fake substituted for
``registry.service.SubprocessCommandRunner`` -- the class
``registry.service.default_runner()``/the status functions themselves
fall back to whenever no ``runner`` is explicitly injected. Patching it
at that one boundary (rather than mocking ``subprocess.run`` directly,
or reaching into each call site) is "the runner mocked at the
``service.py`` boundary" this ticket's own plan calls for -- the exact
seam ``tests/registry/service/test_service_{macos,linux}.py`` already
use for the lower-level functions this suite calls through ``main``.
Every filesystem path a test touches is redirected into ``tmp_path``,
mirroring those same two files' own fixtures.
"""

from __future__ import annotations

import subprocess

import pytest

import mbtools.registry.cli as cli_module
import mbtools.registry.service as service_module
from mbtools.common import EXIT_ERROR, EXIT_LINUX_USER_PREFLIGHT, EXIT_OK
from mbtools.registry.cli import main


# ---------------------------------------------------------------------------
# Fixtures / fakes
# ---------------------------------------------------------------------------


@pytest.fixture
def fake_runner_calls(monkeypatch):
    """Substitute a recording fake for
    ``registry.service.SubprocessCommandRunner`` -- the real
    implementation ``default_runner(dry_run=False)`` and the ``status``
    functions' own ``check=False`` fallback construct when no ``runner``
    is injected. Returns the shared list every fake instance appends its
    calls into, so a test can assert on what *would have* run without a
    real ``launchctl``/``systemctl``/``udevadm``/``usermod``/``loginctl``
    ever executing.
    """
    calls: list[list[str]] = []

    class _RecordingRunner:
        def __init__(self, *, check: bool = True) -> None:
            pass

        def run(self, argv: list[str]) -> subprocess.CompletedProcess[str]:
            calls.append(list(argv))
            # A status check (`launchctl print ...`/`systemctl [--user]
            # is-active ...`) defaults to "not currently loaded" -- the
            # realistic outcome for a file this suite just wrote itself,
            # never through a real launchctl/systemctl. Every other verb
            # (install/uninstall's bootout/enable/bootstrap/daemon-reload/
            # etc.) "succeeds", matching a fresh install/uninstall.
            is_status_check = "print" in argv or "is-active" in argv
            returncode = 1 if is_status_check else 0
            return subprocess.CompletedProcess(argv, returncode, "", "")

    monkeypatch.setattr(service_module, "SubprocessCommandRunner", _RecordingRunner)
    return calls


@pytest.fixture
def as_linux(monkeypatch, fake_runner_calls, tmp_path):
    """Force the Linux orchestration path and redirect every path
    ``linux_install``/``linux_uninstall``/``linux_status`` touch into
    ``tmp_path`` -- same redirection ``tests/registry/service/
    test_service_linux.py``'s own ``linux_paths`` fixture uses."""
    monkeypatch.setattr(cli_module.sys, "platform", "linux")

    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("USERPROFILE", raising=False)
    monkeypatch.delenv("XDG_RUNTIME_DIR", raising=False)
    monkeypatch.delenv("XDG_STATE_HOME", raising=False)

    system_unit = tmp_path / "etc" / "systemd" / "system" / "mbregistry.service"
    udev_rule = tmp_path / "etc" / "udev" / "rules.d" / "99-mbregistry-cmsis-dap.rules"
    monkeypatch.setattr(service_module, "LINUX_SYSTEM_UNIT_PATH", system_unit)
    monkeypatch.setattr(service_module, "LINUX_UDEV_RULE_PATH", udev_rule)

    system_socket = tmp_path / "run" / "mbregistry" / "api.sock"
    user_socket = home / ".cache" / "mbregistry" / "api.sock"
    monkeypatch.setattr(service_module, "system_socket_path", lambda: system_socket)
    monkeypatch.setattr(service_module, "user_socket_path", lambda: user_socket)

    system_db = tmp_path / "var" / "lib" / "mbregistry" / "devices.db"
    user_db = home / ".local" / "state" / "mbregistry" / "devices.db"
    monkeypatch.setattr(service_module, "system_db_path", lambda: system_db)
    monkeypatch.setattr(service_module, "user_db_path", lambda: user_db)

    class _Paths:
        pass

    p = _Paths()
    p.home = home
    p.user_unit = home / ".config" / "systemd" / "user" / "mbregistry.service"
    p.system_unit = system_unit
    p.udev_rule = udev_rule
    return p


@pytest.fixture
def linux_user_ready(monkeypatch, as_linux):
    """Satisfy ``linux_install``'s ``--user`` preflight (plugdev
    membership + udev rule present) so ``service install --user`` on
    Linux succeeds instead of refusing."""
    monkeypatch.setattr(service_module, "_linux_user_in_plugdev", lambda user: True)
    as_linux.udev_rule.parent.mkdir(parents=True, exist_ok=True)
    as_linux.udev_rule.write_text("existing rule")
    return as_linux


@pytest.fixture
def as_macos(monkeypatch, fake_runner_calls, tmp_path):
    """Force the macOS orchestration path and redirect every path
    ``macos_install``/``macos_uninstall``/``macos_status`` touch into
    ``tmp_path`` -- same redirection ``tests/registry/service/
    test_service_macos.py``'s own ``macos_paths`` fixture uses."""
    monkeypatch.setattr(cli_module.sys, "platform", "darwin")

    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("USERPROFILE", raising=False)

    daemon_plist = (
        tmp_path / "Library" / "LaunchDaemons" / "org.jointheleague.mbregistry.plist"
    )
    daemon_log = tmp_path / "Library" / "Logs" / "mbregistry.log"
    monkeypatch.setattr(service_module, "macos_launch_daemon_path", lambda: daemon_plist)
    monkeypatch.setattr(service_module, "macos_system_log_path", lambda: daemon_log)

    system_db = (
        tmp_path / "Library" / "Application Support" / "mbregistry" / "devices.db"
    )
    monkeypatch.setattr(service_module, "system_db_path", lambda: system_db)
    system_socket = tmp_path / "var" / "run" / "mbregistry" / "api.sock"
    monkeypatch.setattr(service_module, "system_socket_path", lambda: system_socket)

    monkeypatch.setattr(service_module.os, "getuid", lambda: 501, raising=False)

    class _Paths:
        pass

    p = _Paths()
    p.home = home
    p.agent_plist = home / "Library" / "LaunchAgents" / "org.jointheleague.mbregistry.plist"
    p.daemon_plist = daemon_plist
    return p


# ---------------------------------------------------------------------------
# service install
# ---------------------------------------------------------------------------


def test_service_install_system_on_linux_writes_unit_and_runs_no_real_command(
    as_linux, fake_runner_calls, capsys
):
    with pytest.raises(SystemExit) as excinfo:
        main(["service", "install", "--system"])

    assert excinfo.value.code == EXIT_OK
    assert as_linux.system_unit.exists()
    assert as_linux.udev_rule.exists()
    # every systemctl/udevadm/usermod call went through the fake -- never
    # a real subprocess.
    assert ["systemctl", "daemon-reload"] in fake_runner_calls
    assert ["systemctl", "enable", "--now", "mbregistry.service"] in fake_runner_calls

    err = capsys.readouterr().err
    assert "installed the system service" in err


def test_service_install_user_on_macos_writes_plist_and_runs_no_real_command(
    as_macos, fake_runner_calls, capsys
):
    with pytest.raises(SystemExit) as excinfo:
        main(["service", "install", "--user"])

    assert excinfo.value.code == EXIT_OK
    assert as_macos.agent_plist.exists()
    assert any(argv[:2] == ["launchctl", "bootstrap"] for argv in fake_runner_calls)

    err = capsys.readouterr().err
    assert "installed the user service" in err


def test_service_install_dry_run_writes_nothing_on_linux(as_linux, fake_runner_calls):
    with pytest.raises(SystemExit) as excinfo:
        main(["service", "install", "--system", "--dry-run"])

    assert excinfo.value.code == EXIT_OK
    assert not as_linux.system_unit.exists()
    assert not as_linux.udev_rule.exists()
    # dry-run's own runner (DryRunCommandRunner) never touches the fake
    # SubprocessCommandRunner substitute at all.
    assert fake_runner_calls == []


def test_service_install_requires_a_scope():
    with pytest.raises(SystemExit) as excinfo:
        main(["service", "install"])
    assert excinfo.value.code == 2


def test_service_install_rejects_both_scopes():
    with pytest.raises(SystemExit) as excinfo:
        main(["service", "install", "--user", "--system"])
    assert excinfo.value.code == 2


def test_service_install_user_on_linux_refuses_without_plugdev_preflight(
    monkeypatch, as_linux, capsys
):
    # Explicit, deterministic "not in plugdev yet" -- rather than relying
    # on this host's real /etc/group, matching test_service_linux.py's
    # own negative-preflight tests.
    monkeypatch.setattr(service_module, "_linux_user_in_plugdev", lambda user: False)

    with pytest.raises(SystemExit) as excinfo:
        main(["service", "install", "--user"])

    assert excinfo.value.code == EXIT_LINUX_USER_PREFLIGHT
    assert not as_linux.user_unit.exists()
    err = capsys.readouterr().err
    assert "cannot install --user" in err


def test_service_install_user_on_linux_succeeds_once_preflight_is_satisfied(
    linux_user_ready,
):
    with pytest.raises(SystemExit) as excinfo:
        main(["service", "install", "--user"])

    assert excinfo.value.code == EXIT_OK
    assert linux_user_ready.user_unit.exists()


# ---------------------------------------------------------------------------
# service uninstall
# ---------------------------------------------------------------------------


def test_service_uninstall_is_a_safe_noop_when_nothing_installed(as_linux, capsys):
    with pytest.raises(SystemExit) as excinfo:
        main(["service", "uninstall", "--system"])

    assert excinfo.value.code == EXIT_OK
    err = capsys.readouterr().err
    assert "not installed" in err


def test_service_uninstall_removes_an_existing_install_on_linux(
    as_linux, fake_runner_calls, capsys
):
    as_linux.system_unit.parent.mkdir(parents=True, exist_ok=True)
    as_linux.system_unit.write_text("unit")
    as_linux.udev_rule.parent.mkdir(parents=True, exist_ok=True)
    as_linux.udev_rule.write_text("rule")

    with pytest.raises(SystemExit) as excinfo:
        main(["service", "uninstall", "--system"])

    assert excinfo.value.code == EXIT_OK
    assert not as_linux.system_unit.exists()
    assert not as_linux.udev_rule.exists()
    err = capsys.readouterr().err
    assert "Removed Linux system service" in err


def test_service_uninstall_purge_forwarded_to_orchestration(monkeypatch, as_linux):
    captured = {}

    def fake_linux_uninstall(scope, *, purge=False, dry_run=False, runner=None):
        captured["scope"] = scope
        captured["purge"] = purge
        captured["dry_run"] = dry_run
        return "ok"

    monkeypatch.setattr(cli_module, "linux_uninstall", fake_linux_uninstall)

    with pytest.raises(SystemExit) as excinfo:
        main(["service", "uninstall", "--system", "--purge"])

    assert excinfo.value.code == EXIT_OK
    assert captured == {"scope": "system", "purge": True, "dry_run": False}


def test_service_uninstall_requires_a_scope():
    with pytest.raises(SystemExit) as excinfo:
        main(["service", "uninstall"])
    assert excinfo.value.code == 2


# ---------------------------------------------------------------------------
# service status
# ---------------------------------------------------------------------------


def test_service_status_reports_both_scopes_when_nothing_installed(as_linux, capsys):
    with pytest.raises(SystemExit) as excinfo:
        main(["service", "status"])

    assert excinfo.value.code == EXIT_OK
    out = capsys.readouterr().out
    assert "user" in out
    assert "system" in out
    assert "not installed" in out


def test_service_status_reports_installed_but_not_running_on_macos(as_macos, capsys):
    as_macos.daemon_plist.parent.mkdir(parents=True, exist_ok=True)
    as_macos.daemon_plist.write_text("plist")

    with pytest.raises(SystemExit) as excinfo:
        main(["service", "status"])

    assert excinfo.value.code == EXIT_OK
    out = capsys.readouterr().out
    assert "installed but not running" in out


def test_service_status_takes_no_scope_flags():
    with pytest.raises(SystemExit) as excinfo:
        main(["service", "status", "--user"])
    assert excinfo.value.code == 2


# ---------------------------------------------------------------------------
# Windows guard -- every `service` subcommand, never reaching macos_*/linux_*
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "argv",
    [
        ["service", "install", "--user"],
        ["service", "uninstall", "--system"],
        ["service", "status"],
    ],
)
def test_service_subcommands_are_not_supported_on_simulated_windows(
    monkeypatch, argv, capsys
):
    monkeypatch.setattr(cli_module.sys, "platform", "win32")
    calls = {"macos": 0, "linux": 0}
    monkeypatch.setattr(
        cli_module, "macos_install", lambda *a, **k: calls.__setitem__("macos", calls["macos"] + 1)
    )
    monkeypatch.setattr(
        cli_module, "linux_install", lambda *a, **k: calls.__setitem__("linux", calls["linux"] + 1)
    )

    with pytest.raises(SystemExit) as excinfo:
        main(argv)

    assert excinfo.value.code == EXIT_ERROR
    assert excinfo.value.code != EXIT_OK
    err = capsys.readouterr().err
    assert "not supported on Windows" in err
    # never dispatched into the platform-specific orchestration at all.
    assert calls == {"macos": 0, "linux": 0}


def test_service_subcommand_names_the_real_platform_when_neither_windows_nor_supported(
    monkeypatch, capsys
):
    monkeypatch.setattr(cli_module.sys, "platform", "freebsd13")

    with pytest.raises(SystemExit) as excinfo:
        main(["service", "status"])

    assert excinfo.value.code == EXIT_ERROR
    err = capsys.readouterr().err
    assert "not supported on freebsd13" in err
