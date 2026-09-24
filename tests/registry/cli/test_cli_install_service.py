"""Tests for ``mbregistry install-service`` (ticket 009) -- a golden-file
assertion on the rendered systemd unit's content, plus a check that the
CLI command itself writes it to ``--output``. Installing into a real
systemd is out of scope for automated tests (sprint.md's Test Strategy);
manual verification on a spare board is a follow-up, not exercised here.

Ticket 008 extends this file with the non-root-USB-access udev rule
(``--udev-output``) install-service now also writes -- see
``test_udev_rule_*``/``test_install_service_*udev*`` below. Every
``main(["install-service", ...])`` call in this file (old and new) now
passes both ``--output`` and ``--udev-output`` pointing at ``tmp_path``:
without an explicit ``--udev-output``, ``cmd_install_service`` would try
to write :data:`~mbtools.registry.cli.DEFAULT_UDEV_RULE_PATH`
(``/etc/udev/rules.d/...``), a real system path that needs root -- the
same reason every pre-ticket-008 test already overrides ``--output``
rather than letting it fall through to
:data:`~mbtools.registry.cli.DEFAULT_UNIT_PATH`.
"""

from __future__ import annotations

import subprocess
import sys

import pytest

import mbtools.registry.cli as cli_module
from mbtools.common import EXIT_OK
from mbtools.registry.cli import (
    DEFAULT_UDEV_RULE_PATH,
    main,
    render_systemd_unit,
    render_udev_rule,
)


def test_rendered_unit_is_a_golden_file_for_the_required_directives():
    unit_text = render_systemd_unit()

    assert "[Unit]" in unit_text
    assert "[Service]" in unit_text
    assert "[Install]" in unit_text

    # correct ExecStart -- invoked through the running interpreter, per
    # flash.py's own "not a bare PATH lookup" precedent.
    assert f"ExecStart={sys.executable} -m mbtools.registry.cli run" in unit_text

    # Restart=on-failure present
    assert "Restart=on-failure" in unit_text

    # directives ensuring both /run/mbregistry/ and /var/lib/mbregistry/
    # exist before the service starts
    assert "RuntimeDirectory=mbregistry" in unit_text
    assert "StateDirectory=mbregistry" in unit_text


def test_rendered_unit_accepts_a_custom_exec_start():
    unit_text = render_systemd_unit(exec_start="/usr/bin/mbregistry run")
    assert "ExecStart=/usr/bin/mbregistry run" in unit_text


def test_unit_is_named_mbregistry_service_distinct_from_mbrelay_service(
    tmp_path, capsys, monkeypatch
):
    # Ticket 006: on real Windows, cmd_install_service dispatches to the
    # SCM install path (ticket 005) instead of writing the systemd
    # unit/udev rule this test checks for -- this test's own subject is
    # the systemd/udev install path specifically, so it forces the
    # off-Windows branch explicitly (rather than skip outright) to stay
    # a real, exercised regression test on every CI leg, including
    # windows-latest.
    monkeypatch.setattr(cli_module.sys, "platform", "linux")
    output = tmp_path / "mbregistry.service"
    udev_output = tmp_path / "99-mbregistry-cmsis-dap.rules"

    with pytest.raises(SystemExit) as excinfo:
        main(
            [
                "install-service",
                "--output",
                str(output),
                "--udev-output",
                str(udev_output),
            ]
        )

    assert excinfo.value.code == EXIT_OK
    assert output.name == "mbregistry.service"
    assert output.name != "mbrelay.service"
    assert output.exists()

    unit_text = output.read_text()
    assert "Restart=on-failure" in unit_text
    assert "RuntimeDirectory=mbregistry" in unit_text
    assert "StateDirectory=mbregistry" in unit_text

    err = capsys.readouterr().err
    assert "systemctl enable --now mbregistry.service" in err


def test_install_service_creates_parent_directories(tmp_path, monkeypatch):
    # Ticket 006: see test_unit_is_named_mbregistry_service_distinct_
    # from_mbrelay_service's own comment just above for why this forces
    # the off-Windows branch rather than skipping on real Windows.
    monkeypatch.setattr(cli_module.sys, "platform", "linux")
    output = tmp_path / "nested" / "dir" / "mbregistry.service"
    udev_output = tmp_path / "nested" / "udev-dir" / "99-mbregistry-cmsis-dap.rules"

    with pytest.raises(SystemExit) as excinfo:
        main(
            [
                "install-service",
                "--output",
                str(output),
                "--udev-output",
                str(udev_output),
            ]
        )

    assert excinfo.value.code == EXIT_OK
    assert output.exists()
    assert udev_output.exists()


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


# ---------------------------------------------------------------------------
# ticket 008 -- non-root USB access udev rule
# ---------------------------------------------------------------------------


def test_rendered_udev_rule_matches_both_tty_and_usb_device_nodes_for_the_daplink_vid_pid():
    rule_text = render_udev_rule()

    # tty device node (mbserial, pyOCD's serial transport)
    assert 'SUBSYSTEM=="tty"' in rule_text
    assert 'SUBSYSTEMS=="usb"' in rule_text

    # raw USB device node (pyOCD's CMSIS-DAP v2 / WinUSB transport)
    assert 'SUBSYSTEM=="usb"' in rule_text

    # hidraw device node (pyOCD's CMSIS-DAP v1 / HID transport)
    assert 'KERNEL=="hidraw*"' in rule_text

    # every match rule is scoped to the micro:bit DAPLink VID:PID, not left
    # open to match any USB device
    assert rule_text.count('ATTRS{idVendor}=="0d28"') == 3
    assert rule_text.count('ATTRS{idProduct}=="0204"') == 3


def test_rendered_udev_rule_grants_access_via_group_and_uaccess():
    rule_text = render_udev_rule()

    # GROUP="plugdev"/MODE="0660" (reliable on the SSH-only Nolanet nodes,
    # no logind seat/session dependency) *and* TAG+="uaccess" (immediate,
    # no-relogin grant wherever a logind seat session does apply) -- both
    # mechanisms, on every one of the three match rules; see
    # render_udev_rule()'s own docstring for why not just one.
    assert rule_text.count('GROUP="plugdev"') == 3
    assert rule_text.count('MODE="0660"') == 3
    assert rule_text.count('TAG+="uaccess"') == 3


def test_install_service_writes_the_udev_rule_to_udev_output(
    tmp_path, capsys, monkeypatch
):
    # Ticket 006: see test_unit_is_named_mbregistry_service_distinct_
    # from_mbrelay_service's own comment (top of file) for why this
    # forces the off-Windows branch rather than skipping on real
    # Windows.
    monkeypatch.setattr(cli_module.sys, "platform", "linux")
    unit_output = tmp_path / "mbregistry.service"
    udev_output = tmp_path / "99-mbregistry-cmsis-dap.rules"

    with pytest.raises(SystemExit) as excinfo:
        main(
            [
                "install-service",
                "--output",
                str(unit_output),
                "--udev-output",
                str(udev_output),
                "--user",
                "eric",
            ]
        )

    assert excinfo.value.code == EXIT_OK
    assert udev_output.exists()
    assert udev_output.read_text() == render_udev_rule()

    err = capsys.readouterr().err
    assert f"mbregistry: wrote {udev_output}" in err
    # follow-up commands are printed, not run (matching the systemd
    # follow-up commands' own existing pattern) -- this ticket's own
    # acceptance criterion.
    assert "udevadm control --reload-rules" in err
    assert "udevadm trigger" in err
    assert "usermod -aG plugdev eric" in err


@pytest.mark.skipif(
    sys.platform == "win32",
    reason=(
        "DEFAULT_UDEV_RULE_PATH is Path('/etc/udev/rules.d/...') --"
        "always POSIX-shaped, unconditionally, since udev itself is "
        "Linux-only -- and pathlib picks WindowsPath vs PosixPath from "
        "the real OS at interpreter startup, not from sys.platform (so "
        "unlike the other tests in this file, forcing sys.platform via "
        "monkeypatch cannot fix this one: str(WindowsPath('/etc/udev/"
        "...')) renders with backslashes on real Windows regardless)"
    ),
)
def test_install_service_udev_rule_write_defaults_to_the_module_constant(tmp_path):
    # DEFAULT_UDEV_RULE_PATH is exercised only for its *value* here (no
    # write to the real path, which needs root) -- confirms --udev-output
    # is documented as overriding this specific default, matching how
    # --output is tested against DEFAULT_UNIT_PATH elsewhere in this file.
    assert DEFAULT_UDEV_RULE_PATH.name == "99-mbregistry-cmsis-dap.rules"
    assert str(DEFAULT_UDEV_RULE_PATH).startswith("/etc/udev/rules.d/")


def test_install_service_udev_rule_install_is_idempotent_on_rerun(tmp_path, monkeypatch):
    # Ticket 006: see test_unit_is_named_mbregistry_service_distinct_
    # from_mbrelay_service's own comment (top of file) for why this
    # forces the off-Windows branch rather than skipping on real
    # Windows.
    monkeypatch.setattr(cli_module.sys, "platform", "linux")
    unit_output = tmp_path / "mbregistry.service"
    udev_output = tmp_path / "99-mbregistry-cmsis-dap.rules"
    argv = [
        "install-service",
        "--output",
        str(unit_output),
        "--udev-output",
        str(udev_output),
    ]

    with pytest.raises(SystemExit) as first:
        main(argv)
    assert first.value.code == EXIT_OK
    first_text = udev_output.read_text()
    first_mtime_dir_listing = sorted(p.name for p in tmp_path.iterdir())

    with pytest.raises(SystemExit) as second:
        main(argv)
    assert second.value.code == EXIT_OK
    second_text = udev_output.read_text()
    second_dir_listing = sorted(p.name for p in tmp_path.iterdir())

    # same content, and no second/duplicate rule file appeared alongside it
    # (e.g. no "99-mbregistry-cmsis-dap.rules.1" or similar) -- this
    # ticket's own "idempotent... no duplicate rule file" acceptance
    # criterion. A bare re-run of install-service never starts, stops, or
    # restarts mbregistry.service either (cmd_install_service only ever
    # writes files and prints instructions), so "no service disruption" is
    # satisfied by construction, not separately exercised here.
    assert second_text == first_text
    assert second_dir_listing == first_mtime_dir_listing


def test_resolve_operating_user_prefers_explicit_flag_over_env(monkeypatch):
    from mbtools.registry.cli import _resolve_operating_user

    monkeypatch.setenv("SUDO_USER", "someoneelse")
    assert _resolve_operating_user("eric") == "eric"


def test_resolve_operating_user_prefers_sudo_user_over_user_env(monkeypatch):
    from mbtools.registry.cli import _resolve_operating_user

    monkeypatch.setenv("SUDO_USER", "eric")
    monkeypatch.setenv("USER", "root")
    assert _resolve_operating_user(None) == "eric"


def test_resolve_operating_user_falls_back_to_user_env(monkeypatch):
    from mbtools.registry.cli import _resolve_operating_user

    monkeypatch.delenv("SUDO_USER", raising=False)
    monkeypatch.setenv("USER", "eric")
    assert _resolve_operating_user(None) == "eric"
