"""Tests for mbtools.registry.service's command-runner seam (ticket
006-001).

Per this ticket's own acceptance criteria and sprint.md's Test Strategy
("the automated suite must never invoke real
launchctl/systemctl/udevadm/usermod/loginctl"), no test in this file
invokes a real external command -- not even a harmless one.
:class:`SubprocessCommandRunner` is exercised by monkeypatching
``mbtools.registry.service.subprocess.run`` itself with a fake that
records how it was called and hands back a scripted
:class:`subprocess.CompletedProcess`, the same "fake only at the true
I/O boundary" approach ``registry.flash``'s tests use for pyocd.
:class:`DryRunCommandRunner` and :func:`default_runner` never shell out
at all, by construction, so they need no such fake.
"""

from __future__ import annotations

import io
import subprocess

import pytest

from mbtools.registry.service import (
    CommandRunner,
    DryRunCommandRunner,
    SubprocessCommandRunner,
    default_runner,
)
from mbtools.registry import service as service_module


# -- CommandRunner protocol ---------------------------------------------------


def test_subprocess_runner_satisfies_command_runner_protocol():
    assert isinstance(SubprocessCommandRunner(), CommandRunner)


def test_dry_run_runner_satisfies_command_runner_protocol():
    assert isinstance(DryRunCommandRunner(), CommandRunner)


# -- SubprocessCommandRunner --------------------------------------------------


class _FakeRun:
    """Stands in for ``subprocess.run`` -- records the call, returns a
    scripted :class:`subprocess.CompletedProcess` (or raises the given
    exception) instead of touching a real process."""

    def __init__(self, result=None, raises=None):
        self.calls: list[tuple[list[str], dict]] = []
        self._result = result
        self._raises = raises

    def __call__(self, argv, **kwargs):
        self.calls.append((argv, kwargs))
        if self._raises is not None:
            raise self._raises
        return self._result


def test_subprocess_runner_calls_subprocess_run_with_expected_kwargs(monkeypatch):
    fake = _FakeRun(result=subprocess.CompletedProcess(["systemctl"], 0, "out", "err"))
    monkeypatch.setattr(service_module.subprocess, "run", fake)

    result = SubprocessCommandRunner().run(["systemctl", "--user", "status"])

    assert fake.calls == [
        (
            ["systemctl", "--user", "status"],
            {"check": True, "capture_output": True, "text": True},
        )
    ]
    assert result.returncode == 0
    assert result.stdout == "out"


def test_subprocess_runner_check_false_passed_through(monkeypatch):
    fake = _FakeRun(result=subprocess.CompletedProcess(["x"], 3, "", ""))
    monkeypatch.setattr(service_module.subprocess, "run", fake)

    SubprocessCommandRunner(check=False).run(["x"])

    assert fake.calls[0][1]["check"] is False


def test_subprocess_runner_check_true_propagates_called_process_error(monkeypatch):
    fake = _FakeRun(raises=subprocess.CalledProcessError(1, ["x"]))
    monkeypatch.setattr(service_module.subprocess, "run", fake)

    with pytest.raises(subprocess.CalledProcessError):
        SubprocessCommandRunner().run(["x"])


# -- DryRunCommandRunner -------------------------------------------------------


def test_dry_run_runner_never_calls_subprocess_run(monkeypatch):
    fake = _FakeRun()
    monkeypatch.setattr(service_module.subprocess, "run", fake)

    DryRunCommandRunner(stream=io.StringIO()).run(["systemctl", "--user", "status"])

    assert fake.calls == []


def test_dry_run_runner_prints_would_run_with_shell_quoted_command():
    stream = io.StringIO()
    DryRunCommandRunner(stream=stream).run(["systemctl", "--user", "status", "a b"])
    assert stream.getvalue() == "would run: systemctl --user status 'a b'\n"


def test_dry_run_runner_defaults_to_stderr(capsys):
    argv = ["launchctl", "print", "system/org.jointheleague.mbregistry"]
    DryRunCommandRunner().run(argv)
    captured = capsys.readouterr()
    assert captured.err == (
        "would run: launchctl print system/org.jointheleague.mbregistry\n"
    )
    assert captured.out == ""


def test_dry_run_runner_returns_synthetic_zero_exit_result():
    result = DryRunCommandRunner(stream=io.StringIO()).run(
        ["udevadm", "control", "--reload"]
    )
    assert isinstance(result, subprocess.CompletedProcess)
    assert result.returncode == 0
    assert result.stdout == ""
    assert result.stderr == ""
    assert result.args == ["udevadm", "control", "--reload"]


# -- default_runner ------------------------------------------------------------


def test_default_runner_picks_subprocess_runner_when_not_dry_run():
    assert isinstance(default_runner(dry_run=False), SubprocessCommandRunner)


def test_default_runner_picks_dry_run_runner_when_dry_run():
    assert isinstance(default_runner(dry_run=True), DryRunCommandRunner)


def test_default_runner_defaults_to_not_dry_run():
    assert isinstance(default_runner(), SubprocessCommandRunner)
