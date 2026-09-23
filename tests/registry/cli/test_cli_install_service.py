"""Tests for ``mbregistry install-service`` (ticket 009) -- a golden-file
assertion on the rendered systemd unit's content, plus a check that the
CLI command itself writes it to ``--output``. Installing into a real
systemd is out of scope for automated tests (sprint.md's Test Strategy);
manual verification on a spare board is a follow-up, not exercised here.
"""

from __future__ import annotations

import subprocess
import sys

import pytest

from mbtools.common import EXIT_OK
from mbtools.registry.cli import main, render_systemd_unit


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


def test_unit_is_named_mbregistry_service_distinct_from_mbrelay_service(tmp_path, capsys):
    output = tmp_path / "mbregistry.service"

    with pytest.raises(SystemExit) as excinfo:
        main(["install-service", "--output", str(output)])

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


def test_install_service_creates_parent_directories(tmp_path):
    output = tmp_path / "nested" / "dir" / "mbregistry.service"

    with pytest.raises(SystemExit) as excinfo:
        main(["install-service", "--output", str(output)])

    assert excinfo.value.code == EXIT_OK
    assert output.exists()


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
