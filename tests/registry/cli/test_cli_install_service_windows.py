"""Tests for sprint 005 ticket 005 -- wiring Windows platform support
into ``mbregistry install-service`` (``registry.cli.cmd_install_service``'s
own ``sys.platform == "win32"`` branch, which dispatches to
``service_windows.cmd_install_service_windows`` instead of writing a
systemd unit/udev rule).

Mirrors ``tests/registry/cli/test_cli_run_windows.py``'s own approach:
``cli.sys.platform`` is monkeypatched to ``"win32"`` to prove the branch
*selection* itself; ``cmd_install_service_windows``'s own rendered-text
behavior is already covered by ``tests/registry/service_windows/
test_service_windows.py`` and is not re-proven here.
"""

from __future__ import annotations

import sys

from mbtools.common import EXIT_OK
from mbtools.registry import cli


def test_cmd_install_service_dispatches_to_windows_branch_on_simulated_win32(
    monkeypatch, tmp_path
):
    monkeypatch.setattr(cli.sys, "platform", "win32")
    calls = []

    def fake_cmd_install_service_windows(exec_path):
        calls.append(exec_path)
        return EXIT_OK

    monkeypatch.setattr(cli, "cmd_install_service_windows", fake_cmd_install_service_windows)

    unit_output = tmp_path / "mbregistry.service"
    udev_output = tmp_path / "99-mbregistry-cmsis-dap.rules"
    parser = cli.build_parser()
    args = parser.parse_args(
        [
            "install-service",
            "--output",
            str(unit_output),
            "--udev-output",
            str(udev_output),
        ]
    )

    code = cli.cmd_install_service(args)

    assert code == EXIT_OK
    assert len(calls) == 1
    # the systemd-unit/udev-rule code path is never reached on Windows --
    # neither file gets written.
    assert not unit_output.exists()
    assert not udev_output.exists()


def test_windows_branch_exec_path_invokes_run_with_the_windows_service_flag(monkeypatch):
    """The rendered ``sc.exe create`` command must invoke exactly the
    SCM-aware entry point ``cmd_run``'s own ``--windows-service`` branch
    provides -- not the bare ``mbregistry run``
    ``render_windows_service_install``'s own default still renders (that
    default is unchanged, and still covered by ticket 004's own golden-
    file tests in ``tests/registry/service_windows/test_service_windows.py``).
    """
    monkeypatch.setattr(cli.sys, "platform", "win32")
    captured = {}

    def fake_cmd_install_service_windows(exec_path):
        captured["exec_path"] = exec_path
        return EXIT_OK

    monkeypatch.setattr(cli, "cmd_install_service_windows", fake_cmd_install_service_windows)

    parser = cli.build_parser()
    args = parser.parse_args(["install-service"])
    cli.cmd_install_service(args)

    assert captured["exec_path"] == (
        f"{sys.executable} -m mbtools.registry.cli run --windows-service"
    )


def test_cmd_install_service_still_writes_systemd_unit_and_udev_rule_off_windows(tmp_path):
    # sys.platform is left alone here -- this dev host's own real
    # platform -- regression guard for the pre-ticket-005 behavior,
    # already covered exhaustively by test_cli_install_service.py; this
    # is a light smoke check that the new win32 branch didn't disturb
    # the fallthrough.
    unit_output = tmp_path / "mbregistry.service"
    udev_output = tmp_path / "99-mbregistry-cmsis-dap.rules"
    parser = cli.build_parser()
    args = parser.parse_args(
        [
            "install-service",
            "--output",
            str(unit_output),
            "--udev-output",
            str(udev_output),
        ]
    )

    code = cli.cmd_install_service(args)

    assert code == EXIT_OK
    assert unit_output.exists()
    assert udev_output.exists()
