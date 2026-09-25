"""Tests for the deprecated, hidden ``mbregistry install-service`` alias
(ticket 006-004).

Ticket 006-004 replaces this command's old, direct write-the-two-files-
and-print-instructions implementation with a thin deprecation shim that
delegates entirely to ``registry.service``'s ``macos_install``/
``linux_install`` -- the same functions ``mbregistry service install``
uses -- called with ``dry_run=False`` (so the unit/udev-rule file *is*
still written, matching this command's old, pre-006-004 observable
behavior) and an explicit :class:`~mbtools.registry.service.DryRunCommandRunner`
as ``runner`` (so the follow-up ``systemctl``/``udevadm``/``usermod``
commands are only ever printed, never executed) -- see
``cli.cmd_install_service``'s own docstring for the full reconciliation
this choice is built on.

The golden-file assertions on ``render_systemd_unit()``/
``render_udev_rule()`` themselves, and the udev VID:PID/group content
checks, moved to ``registry.service`` in ticket 006-003 and are already
covered by ``tests/registry/service/test_service_linux.py`` -- not
re-tested here.

Behavior change from the pre-006-004 command, called out rather than
silently dropped: ``--output``/``--udev-output`` (path-redirection flags)
no longer exist. ``registry.service.linux_install``/``macos_install``
always write to the platform's one fixed, real location
(``registry.paths.LINUX_SYSTEM_UNIT_PATH``/``LINUX_UDEV_RULE_PATH``, or
the macOS launchd equivalent) -- there is no override mechanism to plumb
a CLI flag through to, matching this command's new "thin wrapper around
`service install --system`" shape. Every test below that needs a
writable path monkeypatches those module-level constants directly
(``tests/registry/service/test_service_linux.py``'s own convention)
instead of passing ``--output``/``--udev-output``. ``--user`` (a string
override for the operating user named in the printed ``usermod`` line)
is unaffected and still accepted.
"""

from __future__ import annotations

import subprocess
import sys

import pytest

import mbtools.registry.cli as cli_module
import mbtools.registry.service as service_module
from mbtools.common import EXIT_OK
from mbtools.registry.cli import main
from mbtools.registry.service import render_systemd_unit, render_udev_rule


@pytest.fixture
def linux_unit_paths(monkeypatch, tmp_path):
    """Redirect the two fixed system paths ``linux_install`` writes into
    ``tmp_path`` -- the only way to exercise a real write in this suite,
    since the deprecated command no longer takes ``--output``/
    ``--udev-output``. Matches ``tests/registry/service/
    test_service_linux.py``'s own ``LINUX_SYSTEM_UNIT_PATH``/
    ``LINUX_UDEV_RULE_PATH`` monkeypatch.
    """
    monkeypatch.setattr(cli_module.sys, "platform", "linux")
    unit_path = tmp_path / "mbregistry.service"
    udev_path = tmp_path / "99-mbregistry-cmsis-dap.rules"
    monkeypatch.setattr(service_module, "LINUX_SYSTEM_UNIT_PATH", unit_path)
    monkeypatch.setattr(service_module, "LINUX_UDEV_RULE_PATH", udev_path)

    class _Paths:
        pass

    p = _Paths()
    p.unit_path = unit_path
    p.udev_path = udev_path
    return p


def test_install_service_is_deprecated_and_still_writes_the_unit_and_udev_rule(
    linux_unit_paths, capsys
):
    with pytest.raises(SystemExit) as excinfo:
        main(["install-service"])

    assert excinfo.value.code == EXIT_OK
    assert linux_unit_paths.unit_path.exists()
    assert linux_unit_paths.udev_path.exists()
    assert linux_unit_paths.unit_path.read_text() == render_systemd_unit()
    assert linux_unit_paths.udev_path.read_text() == render_udev_rule()

    err = capsys.readouterr().err
    assert (
        "install-service is deprecated, use 'mbregistry service install "
        "--system'" in err
    )


def test_install_service_never_runs_a_real_systemctl_or_udevadm_command(
    monkeypatch, linux_unit_paths, capsys
):
    real_run = subprocess.run
    calls: list[list[str]] = []

    def spy_run(argv, *args, **kwargs):
        calls.append(list(argv))
        return real_run(argv, *args, **kwargs)

    monkeypatch.setattr(subprocess, "run", spy_run)

    with pytest.raises(SystemExit) as excinfo:
        main(["install-service"])

    assert excinfo.value.code == EXIT_OK
    # subprocess.run is never reached at all -- DryRunCommandRunner only
    # ever calls print().
    assert calls == []

    err = capsys.readouterr().err
    assert "would run: systemctl daemon-reload" in err
    assert "would run: systemctl enable --now mbregistry.service" in err
    assert "would run: udevadm control --reload-rules" in err
    assert "would run: udevadm trigger" in err
    assert "would run: usermod -aG plugdev" in err


def test_install_service_user_flag_still_names_the_operating_user(
    linux_unit_paths, capsys
):
    with pytest.raises(SystemExit) as excinfo:
        main(["install-service", "--user", "eric"])

    assert excinfo.value.code == EXIT_OK
    err = capsys.readouterr().err
    assert "would run: usermod -aG plugdev eric" in err


def test_install_service_is_idempotent_on_rerun(linux_unit_paths):
    with pytest.raises(SystemExit) as first:
        main(["install-service"])
    assert first.value.code == EXIT_OK
    first_text = linux_unit_paths.unit_path.read_text()

    with pytest.raises(SystemExit) as second:
        main(["install-service"])
    assert second.value.code == EXIT_OK
    second_text = linux_unit_paths.unit_path.read_text()

    assert first_text == second_text


def test_install_service_is_hidden_from_the_help_subcommand_listing():
    parser = cli_module.build_parser()
    help_text = parser.format_help()
    # still reachable (argparse has no clean way to remove it from the
    # usage line's own choice brace -- see build_parser()'s own comment)
    # but no longer described in the subcommand listing below it.
    assert "write the mbregistry systemd unit" not in help_text


def test_module_invocation_shape_that_the_rendered_unit_s_execstart_uses_actually_runs():
    """Regression test for a bug ticket 010's real-hardware pass found:
    ``render_systemd_unit()``'s ``ExecStart=`` (and this module's own
    docstring) both document ``{python} -m mbtools.registry.cli run`` as
    the production entry point -- but ``cli.py`` had no
    ``if __name__ == "__main__":`` guard, so that exact invocation shape
    only imported the module and exited 0 *without ever calling
    ``main()``*. Every other test in this suite drives ``cli.main()`` or
    the ``mbregistry`` console script (``pyproject.toml``'s
    ``[project.scripts]``, which calls ``main()`` directly) -- neither
    exercises ``python -m ...``, so nothing caught this until a real
    systemd unit's ``ExecStart=`` silently did nothing on ``meili``.

    Runs the real module as a subprocess (the only way to reproduce
    "invoked via ``-m``" faithfully -- an in-process import can't
    simulate ``__name__ == "__main__"``) and asserts ``--help`` actually
    produces argparse's usage text, not a silent no-op exit(0).
    """
    result = subprocess.run(
        [sys.executable, "-m", "mbtools.registry.cli", "--help"],
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert result.returncode == 0
    assert "usage: mbregistry" in result.stdout
    assert "install-service" in result.stdout
