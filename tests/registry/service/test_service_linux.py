"""Tests for mbtools.registry.service's Linux (systemd/udev) support
(ticket 006-003): the moved systemd-unit/udev-rule renderers, the new
user-scope unit variant, and install/uninstall/status orchestration for
both scopes, including the ``--user`` plugdev/udev preflight refusal.

Per sprint.md's Test Strategy and this ticket's own acceptance criteria,
no test here invokes a real ``systemctl``/``udevadm``/``usermod``/
``loginctl`` or writes to a real ``/etc``/``~/.config`` path. Every path
a test touches is redirected into ``tmp_path`` first:

- ``HOME`` is monkeypatched (env var), which is what ``registry.paths``'
  ``_home()`` resolves against -- same convention
  ``test_service_macos.py`` uses.
- The *system*-scope path constants (``LINUX_SYSTEM_UNIT_PATH``,
  ``LINUX_UDEV_RULE_PATH``) have no env override in ``registry.paths``
  at all (fixed ``/etc/...`` paths by design) -- monkeypatched directly
  on ``service_module`` instead, the same "fake only at the true
  boundary" approach ``test_service_macos.py`` uses for
  ``macos_launch_daemon_path``/``macos_system_log_path``.
- ``_linux_user_in_plugdev`` (the ``grp``/``pwd``-based preflight check)
  is monkeypatched directly on ``service_module`` rather than faking
  ``grp``/``pwd`` themselves -- this suite's own "fake only at the true
  boundary" call, applied to a boundary that is a plain function, not a
  subprocess.

Every ``systemctl``/``udevadm``/``usermod``/``loginctl`` invocation goes
through a hand-rolled :class:`CommandRunner` fake, never a real mock
library, matching ``test_service_runner.py``'s/``test_service_macos.py``'s
existing convention.
"""

from __future__ import annotations

import subprocess
import sys

import pytest

import mbtools.registry.service as service_module
from mbtools.registry.service import (
    LinuxUserPreflightError,
    ServiceStatus,
    linux_install,
    linux_status,
    linux_uninstall,
    render_systemd_unit,
    render_udev_rule,
)


# ---------------------------------------------------------------------------
# Fixtures / fakes
# ---------------------------------------------------------------------------


@pytest.fixture
def linux_paths(monkeypatch, tmp_path):
    """Redirect every Linux path this ticket's functions touch into
    ``tmp_path``. Returns a small namespace of the resolved paths so
    tests can assert against them directly."""
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

    user_unit = home / ".config" / "systemd" / "user" / "mbregistry.service"

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
    p.user_unit = user_unit
    p.system_unit = system_unit
    p.udev_rule = udev_rule
    p.system_socket = system_socket
    p.user_socket = user_socket
    p.system_db = system_db
    p.user_db = user_db
    return p


class _FakeRunner:
    """A :class:`CommandRunner` fake that records every call and, when
    ``fail_argvs`` names one, raises :class:`subprocess.CalledProcessError`
    instead of "succeeding"."""

    def __init__(self, *, fail_argvs: set[tuple[str, ...]] | None = None):
        self.calls: list[list[str]] = []
        self._fail_argvs = fail_argvs or set()

    def run(self, argv: list[str]) -> subprocess.CompletedProcess[str]:
        self.calls.append(list(argv))
        if tuple(argv) in self._fail_argvs:
            raise subprocess.CalledProcessError(1, argv)
        return subprocess.CompletedProcess(argv, 0, "", "")


@pytest.fixture
def plugdev_ready(monkeypatch, linux_paths):
    """Simulate the "--user install is allowed" precondition: operating
    user already in plugdev, and the udev rule already exists."""
    monkeypatch.setattr(service_module, "_linux_user_in_plugdev", lambda user: True)
    linux_paths.udev_rule.parent.mkdir(parents=True, exist_ok=True)
    linux_paths.udev_rule.write_text("existing rule")
    return linux_paths


# ---------------------------------------------------------------------------
# render_systemd_unit -- golden-file (system scope unchanged, new user scope)
# ---------------------------------------------------------------------------


def test_render_systemd_unit_rejects_unknown_scope():
    with pytest.raises(ValueError):
        render_systemd_unit(scope="bogus")


def test_render_systemd_unit_defaults_to_system_scope_unchanged():
    unit_text = render_systemd_unit()

    assert "[Unit]" in unit_text
    assert "[Service]" in unit_text
    assert "[Install]" in unit_text
    assert f"ExecStart={sys.executable} -m mbtools.registry.cli run" in unit_text
    assert "Restart=on-failure" in unit_text
    assert "RuntimeDirectory=mbregistry" in unit_text
    assert "StateDirectory=mbregistry" in unit_text
    assert "WantedBy=multi-user.target" in unit_text


def test_render_systemd_unit_accepts_a_custom_exec_start():
    unit_text = render_systemd_unit(exec_start="/usr/bin/mbregistry run")
    assert "ExecStart=/usr/bin/mbregistry run" in unit_text


def test_render_systemd_unit_user_scope_has_no_runtime_or_state_directory():
    unit_text = render_systemd_unit(scope="user")

    assert "RuntimeDirectory=" not in unit_text
    assert "StateDirectory=" not in unit_text
    assert "WantedBy=default.target" in unit_text
    assert "WantedBy=multi-user.target" not in unit_text
    assert f"ExecStart={sys.executable} -m mbtools.registry.cli run" in unit_text


def test_render_systemd_unit_user_scope_accepts_custom_exec_start():
    unit_text = render_systemd_unit(exec_start="/opt/venv/bin/python -m mbtools.registry.cli run", scope="user")
    assert "ExecStart=/opt/venv/bin/python -m mbtools.registry.cli run" in unit_text


# ---------------------------------------------------------------------------
# render_udev_rule -- unchanged golden file (moved, not rewritten)
# ---------------------------------------------------------------------------


def test_render_udev_rule_matches_both_tty_and_usb_device_nodes_for_the_daplink_vid_pid():
    rule_text = render_udev_rule()

    assert 'SUBSYSTEM=="tty"' in rule_text
    assert 'SUBSYSTEMS=="usb"' in rule_text
    assert 'SUBSYSTEM=="usb"' in rule_text
    assert 'KERNEL=="hidraw*"' in rule_text
    assert rule_text.count('ATTRS{idVendor}=="0d28"') == 3
    assert rule_text.count('ATTRS{idProduct}=="0204"') == 3


def test_render_udev_rule_grants_access_via_group_and_uaccess():
    rule_text = render_udev_rule()

    assert rule_text.count('GROUP="plugdev"') == 3
    assert rule_text.count('MODE="0660"') == 3
    assert rule_text.count('TAG+="uaccess"') == 3


# ---------------------------------------------------------------------------
# linux_install -- scope="system"
# ---------------------------------------------------------------------------


def test_linux_install_system_writes_files_and_calls_expected_sequence(linux_paths):
    runner = _FakeRunner()
    result_path = linux_install(
        "system", dry_run=False, runner=runner, operating_user="eric"
    )

    assert result_path == linux_paths.system_unit
    assert linux_paths.system_unit.exists()
    assert linux_paths.udev_rule.exists()
    assert linux_paths.system_unit.read_text() == render_systemd_unit(scope="system")
    assert linux_paths.udev_rule.read_text() == render_udev_rule()

    assert runner.calls == [
        ["systemctl", "daemon-reload"],
        ["systemctl", "enable", "--now", "mbregistry.service"],
        ["udevadm", "control", "--reload-rules"],
        ["udevadm", "trigger"],
        ["usermod", "-aG", "plugdev", "eric"],
    ]


def test_linux_install_system_usermod_is_actually_invoked_not_only_printed(
    linux_paths, capsys
):
    # This is this ticket's own key acceptance criterion distinguishing
    # linux_install from the old print-only install-service.
    runner = _FakeRunner()
    linux_install("system", dry_run=False, runner=runner, operating_user="eric")

    assert ["usermod", "-aG", "plugdev", "eric"] in runner.calls

    captured = capsys.readouterr()
    assert "log in again" in captured.err


def test_linux_install_system_resolves_operating_user_from_sudo_user(
    linux_paths, monkeypatch
):
    monkeypatch.setenv("SUDO_USER", "operator")
    runner = _FakeRunner()
    linux_install("system", dry_run=False, runner=runner, operating_user=None)

    assert ["usermod", "-aG", "plugdev", "operator"] in runner.calls


def test_linux_install_system_dry_run_writes_nothing_and_prints_would_run(linux_paths):
    result_path = linux_install("system", dry_run=True, runner=None, operating_user="eric")

    assert result_path == linux_paths.system_unit
    assert not linux_paths.system_unit.exists()
    assert not linux_paths.udev_rule.exists()


# ---------------------------------------------------------------------------
# linux_install -- scope="user"
# ---------------------------------------------------------------------------


def test_linux_install_user_refuses_when_plugdev_missing(linux_paths, monkeypatch):
    monkeypatch.setattr(service_module, "_linux_user_in_plugdev", lambda user: False)
    # No udev rule written either -- both missing.
    runner = _FakeRunner()

    with pytest.raises(LinuxUserPreflightError) as excinfo:
        linux_install("user", dry_run=False, runner=runner, operating_user="eric")

    assert "usermod -aG plugdev eric" in str(excinfo.value)
    assert "service install --system" in str(excinfo.value)
    assert runner.calls == []
    assert not linux_paths.user_unit.exists()


def test_linux_install_user_refuses_when_only_udev_rule_missing(linux_paths, monkeypatch):
    monkeypatch.setattr(service_module, "_linux_user_in_plugdev", lambda user: True)
    runner = _FakeRunner()

    with pytest.raises(LinuxUserPreflightError) as excinfo:
        linux_install("user", dry_run=False, runner=runner, operating_user="eric")

    assert "usermod -aG plugdev" not in str(excinfo.value)
    assert "service install --system" in str(excinfo.value)
    assert runner.calls == []
    assert not linux_paths.user_unit.exists()


def test_linux_install_user_refusal_prints_remediation_to_stderr(linux_paths, monkeypatch, capsys):
    monkeypatch.setattr(service_module, "_linux_user_in_plugdev", lambda user: False)

    with pytest.raises(LinuxUserPreflightError):
        linux_install("user", dry_run=False, runner=_FakeRunner(), operating_user="eric")

    captured = capsys.readouterr()
    assert "sudo usermod -aG plugdev eric" in captured.err


def test_linux_install_user_writes_unit_and_calls_expected_sequence_when_ready(
    plugdev_ready,
):
    runner = _FakeRunner()
    result_path = linux_install(
        "user", dry_run=False, runner=runner, operating_user="eric"
    )

    assert result_path == plugdev_ready.user_unit
    assert plugdev_ready.user_unit.exists()
    assert plugdev_ready.user_unit.read_text() == render_systemd_unit(scope="user")

    assert runner.calls == [
        ["systemctl", "--user", "daemon-reload"],
        ["systemctl", "--user", "enable", "--now", "mbregistry.service"],
        ["loginctl", "enable-linger", "eric"],
    ]


def test_linux_install_user_dry_run_writes_nothing(plugdev_ready):
    result_path = linux_install(
        "user", dry_run=True, runner=None, operating_user="eric"
    )

    assert result_path == plugdev_ready.user_unit
    assert not plugdev_ready.user_unit.exists()


# ---------------------------------------------------------------------------
# linux_uninstall
# ---------------------------------------------------------------------------


def test_linux_uninstall_system_removes_unit_and_udev_rule_and_calls_expected_sequence(
    linux_paths,
):
    linux_install("system", dry_run=False, runner=_FakeRunner(), operating_user="eric")
    assert linux_paths.system_unit.exists()
    assert linux_paths.udev_rule.exists()

    runner = _FakeRunner()
    message = linux_uninstall("system", runner=runner)

    assert not linux_paths.system_unit.exists()
    assert not linux_paths.udev_rule.exists()
    assert runner.calls == [
        ["systemctl", "disable", "--now", "mbregistry.service"],
        ["systemctl", "daemon-reload"],
        ["udevadm", "control", "--reload-rules"],
    ]
    assert str(linux_paths.system_unit) in message
    assert all("usermod" not in argv for argv in runner.calls)


def test_linux_uninstall_user_removes_unit_and_calls_expected_sequence(plugdev_ready):
    linux_install("user", dry_run=False, runner=_FakeRunner(), operating_user="eric")
    assert plugdev_ready.user_unit.exists()

    runner = _FakeRunner()
    message = linux_uninstall("user", runner=runner)

    assert not plugdev_ready.user_unit.exists()
    assert runner.calls == [["systemctl", "--user", "disable", "--now", "mbregistry.service"]]
    assert str(plugdev_ready.user_unit) in message


def test_linux_uninstall_never_calls_usermod(linux_paths):
    linux_install("system", dry_run=False, runner=_FakeRunner(), operating_user="eric")

    runner = _FakeRunner()
    linux_uninstall("system", purge=True, runner=runner)

    assert all("usermod" not in argv for argv in runner.calls)


def test_linux_uninstall_removes_socket_file_when_not_purging(linux_paths):
    linux_install("system", dry_run=False, runner=_FakeRunner(), operating_user="eric")
    linux_paths.system_socket.parent.mkdir(parents=True, exist_ok=True)
    linux_paths.system_socket.write_text("")

    linux_uninstall("system", purge=False, runner=_FakeRunner())

    assert not linux_paths.system_socket.exists()


def test_linux_uninstall_keeps_devices_db_without_purge(linux_paths):
    linux_install("system", dry_run=False, runner=_FakeRunner(), operating_user="eric")
    linux_paths.system_db.parent.mkdir(parents=True, exist_ok=True)
    linux_paths.system_db.write_text("data")

    linux_uninstall("system", purge=False, runner=_FakeRunner())

    assert linux_paths.system_db.exists()


def test_linux_uninstall_purge_removes_state_directory(linux_paths):
    linux_install("system", dry_run=False, runner=_FakeRunner(), operating_user="eric")
    linux_paths.system_db.parent.mkdir(parents=True, exist_ok=True)
    linux_paths.system_db.write_text("data")

    linux_uninstall("system", purge=True, runner=_FakeRunner())

    assert not linux_paths.system_db.exists()
    assert not linux_paths.system_db.parent.exists()


def test_linux_uninstall_is_a_noop_when_nothing_installed(linux_paths):
    runner = _FakeRunner()
    message = linux_uninstall("user", runner=runner)

    assert runner.calls == []
    assert "not installed" in message


def test_linux_uninstall_noop_message_names_other_scope_if_installed(linux_paths):
    linux_install("system", dry_run=False, runner=_FakeRunner(), operating_user="eric")

    message = linux_uninstall("user", runner=_FakeRunner())

    assert "not installed" in message
    assert str(linux_paths.system_unit) in message


def test_linux_uninstall_tolerates_disable_failure_when_already_stopped(linux_paths):
    linux_install("system", dry_run=False, runner=_FakeRunner(), operating_user="eric")

    runner = _FakeRunner(
        fail_argvs={("systemctl", "disable", "--now", "mbregistry.service")}
    )
    # Must not raise even though disable fails (already stopped).
    linux_uninstall("system", runner=runner)

    assert not linux_paths.system_unit.exists()


# ---------------------------------------------------------------------------
# linux_status
# ---------------------------------------------------------------------------


def test_linux_status_not_installed(linux_paths):
    status = linux_status("user", runner=_FakeRunner())

    assert status == ServiceStatus(
        scope="user", path=linux_paths.user_unit, installed=False, running=False
    )
    assert status.describe() == "not installed"


def test_linux_status_installed_but_not_running(linux_paths):
    linux_install("system", dry_run=False, runner=_FakeRunner(), operating_user="eric")

    class _NotActiveRunner:
        def run(self, argv):
            return subprocess.CompletedProcess(argv, 3, "", "inactive")

    status = linux_status("system", runner=_NotActiveRunner())

    assert status.installed is True
    assert status.running is False
    assert status.describe() == "installed but not running"


def test_linux_status_installed_and_running(linux_paths):
    linux_install("system", dry_run=False, runner=_FakeRunner(), operating_user="eric")

    class _ActiveRunner:
        def run(self, argv):
            return subprocess.CompletedProcess(argv, 0, "active", "")

    status = linux_status("system", runner=_ActiveRunner())

    assert status.installed is True
    assert status.running is True
    assert status.describe() == "installed and running"


def test_linux_status_never_calls_runner_when_not_installed(linux_paths):
    runner = _FakeRunner()
    linux_status("user", runner=runner)
    assert runner.calls == []


def test_linux_status_user_scope_uses_systemctl_user_flag(plugdev_ready):
    linux_install("user", dry_run=False, runner=_FakeRunner(), operating_user="eric")

    runner = _FakeRunner()
    linux_status("user", runner=runner)

    assert runner.calls == [["systemctl", "--user", "is-active", "mbregistry"]]


# ---------------------------------------------------------------------------
# Platform-branch safety: nothing here relies on the real host platform.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("real_platform", ["linux", "darwin", "win32"])
def test_linux_functions_dont_depend_on_sys_platform(linux_paths, monkeypatch, real_platform):
    """These functions are Linux-specific by construction (ticket
    006-004 is what dispatches on ``sys.platform`` in ``registry.cli``)
    -- they never branch on it themselves, so forcing it to each of the
    three platforms here must not change behavior, keeping this whole
    suite safe to run on macOS/Windows CI, matching
    ``test_service_macos.py``'s own cross-platform-import guarantee.
    """
    monkeypatch.setattr(service_module.sys, "platform", real_platform)

    result_path = linux_install(
        "system", dry_run=False, runner=_FakeRunner(), operating_user="eric"
    )
    assert result_path == linux_paths.system_unit
    assert linux_paths.system_unit.exists()
