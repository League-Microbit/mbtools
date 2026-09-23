"""Unit tests for mbtools.deploy.flash.flash_hex.

Ported near-verbatim from today's ``mbdeploy``'s own
``tests/test_flash.py`` (see ticket 005 in this sprint) -- the
mechanical proof that the port preserved pyOCD's argv, messages, and
return codes exactly.

Every fake here patches ``subprocess.Popen`` (not ``subprocess.run``)
and returns a fake process object exposing just the two members
``_run_streamed`` touches: ``.stdout`` (an iterable of already-
``\\n``-terminated lines) and ``.wait()`` (the exit code) -- mirroring
how a real ``Popen`` instance is used, without invoking pyocd for real.
"""

from __future__ import annotations

import subprocess
import sys

import pytest

from mbtools.deploy import flash as flash_mod

_UID = "9906" + "c" * 36  # 40 hex chars, matches the style used elsewhere
_MCU = "nrf52833"

_PYOCD = [sys.executable, "-m", "pyocd"]

#: A minimal, complete, valid Intel HEX file: just the EOF record.
_VALID_HEX_CONTENT = ":00000001FF\n"

#: A locked-device failure signature (see flash.py::_LOCKED_SIGNATURES).
_LOCKED_SIGNATURE_LINES = ("flash erase sector failure (0x67)",)


@pytest.fixture
def valid_hex(tmp_path) -> str:
    """A real, on-disk, valid Intel HEX file's path.

    ``flash_hex`` validates ``hex_path`` with ``intelhex`` before ever
    invoking pyocd, so every test below that expects the faked
    ``subprocess.Popen`` step to be reached needs a real file on disk.
    """
    path = tmp_path / "valid.hex"
    path.write_text(_VALID_HEX_CONTENT)
    return str(path)


class _FakeProcess:
    """Stand-in for a ``subprocess.Popen`` instance.

    ``flash.py::_run_streamed`` only ever iterates ``.stdout`` for lines
    and calls ``.wait()`` for the exit code, so that's all this fake
    needs to provide.
    """

    def __init__(self, returncode: int, lines: tuple[str, ...] = ()):
        self.returncode = returncode
        self.stdout = iter(f"{line}\n" for line in lines)

    def wait(self) -> int:
        return self.returncode


def _result(rc: int, lines: tuple[str, ...] = ()):
    return _FakeProcess(rc, lines)


class TestArgvConstruction:
    """A successful first flash should invoke exactly flash + reset."""

    def test_flash_and_reset_argv(self, monkeypatch, valid_hex):
        calls: list[list[str]] = []

        def fake_run(cmd, **kw):
            calls.append(cmd)
            return _result(0)

        monkeypatch.setattr(subprocess, "Popen", fake_run)

        rc = flash_mod.flash_hex(_UID, valid_hex, target_mcu=_MCU)

        assert rc == 0
        assert len(calls) == 2
        flash_cmd, reset_cmd = calls

        assert flash_cmd == [
            *_PYOCD, "flash",
            "-t", _MCU,
            "--uid", _UID,
            valid_hex,
        ]
        assert reset_cmd == [
            *_PYOCD, "reset",
            "-t", _MCU,
            "--uid", _UID,
        ]
        # No mass erase on a clean first flash.
        assert not any("erase" in c for c in calls)

    def test_erase_argv_on_recovery(self, monkeypatch, valid_hex):
        calls: list[list[str]] = []
        state = {"flash": 0}

        def fake_run(cmd, **kw):
            calls.append(cmd)
            if "flash" in cmd:
                state["flash"] += 1
                if state["flash"] == 1:
                    return _result(1, _LOCKED_SIGNATURE_LINES)
                return _result(0)
            return _result(0)

        monkeypatch.setattr(subprocess, "Popen", fake_run)

        rc = flash_mod.flash_hex(_UID, valid_hex, target_mcu=_MCU)

        assert rc == 0
        erase_calls = [c for c in calls if "erase" in c]
        assert len(erase_calls) == 1
        assert erase_calls[0] == [
            *_PYOCD, "erase",
            "-t", _MCU,
            "--uid", _UID,
            "--mass",
        ]


class TestMassEraseRecovery:
    def test_flash_retries_after_mass_erase(self, monkeypatch, valid_hex):
        """First flash fails, mass erase succeeds, second flash + reset succeed."""
        calls: list[list[str]] = []
        state = {"flash": 0}

        def fake_run(cmd, **kw):
            calls.append(cmd)
            if "flash" in cmd:
                state["flash"] += 1
                if state["flash"] == 1:                 # first flash fails
                    return _result(1, _LOCKED_SIGNATURE_LINES)
                return _result(0)
            return _result(0)                           # erase / reset succeed

        monkeypatch.setattr(subprocess, "Popen", fake_run)

        rc = flash_mod.flash_hex(_UID, valid_hex, target_mcu=_MCU)

        assert rc == 0
        assert state["flash"] == 2                      # flashed twice
        assert any("erase" in c and "--mass" in c for c in calls)

    def test_mass_erase_failure_aborts_without_retry(self, monkeypatch, capsys, valid_hex):
        """If the mass erase itself fails, flash_hex aborts and does not re-flash."""
        state = {"flash": 0}

        def fake_run(cmd, **kw):
            if "flash" in cmd:
                state["flash"] += 1
                return _result(1, _LOCKED_SIGNATURE_LINES)
            elif "erase" in cmd:
                return _result(5)
            return _result(0)

        monkeypatch.setattr(subprocess, "Popen", fake_run)

        rc = flash_mod.flash_hex(_UID, valid_hex, target_mcu=_MCU)

        assert rc == 5
        assert state["flash"] == 1                      # no retry after erase failure
        assert "mass erase failed" in capsys.readouterr().err.lower()

    def test_successful_flash_skips_mass_erase(self, monkeypatch, valid_hex):
        """The normal path never mass-erases when the first flash succeeds."""
        calls: list[list[str]] = []

        def fake_run(cmd, **kw):
            calls.append(cmd)
            return _result(0)

        monkeypatch.setattr(subprocess, "Popen", fake_run)

        rc = flash_mod.flash_hex(_UID, valid_hex, target_mcu=_MCU)

        assert rc == 0
        assert not any("erase" in c for c in calls)

    def test_flash_still_failing_after_mass_erase_returns_flash_rc(self, monkeypatch, capsys, valid_hex):
        """Mass erase succeeds but the retried flash still fails: return its rc."""
        state = {"flash": 0}

        def fake_run(cmd, **kw):
            if "flash" in cmd:
                state["flash"] += 1
                return _result(7, _LOCKED_SIGNATURE_LINES)
            return _result(0)  # erase succeeds

        monkeypatch.setattr(subprocess, "Popen", fake_run)

        rc = flash_mod.flash_hex(_UID, valid_hex, target_mcu=_MCU)

        assert rc == 7
        assert state["flash"] == 2
        assert "flash still failed after mass erase" in capsys.readouterr().err.lower()


class TestLogRouting:
    """log=None must print to stderr; a supplied log callable must intercept it."""

    def test_log_none_prints_to_stderr(self, monkeypatch, capsys, valid_hex):
        def fake_run(cmd, **kw):
            if "flash" in cmd:
                return _result(1, _LOCKED_SIGNATURE_LINES)
            elif "erase" in cmd:
                return _result(5)
            return _result(0)

        monkeypatch.setattr(subprocess, "Popen", fake_run)

        rc = flash_mod.flash_hex(_UID, valid_hex, target_mcu=_MCU)

        assert rc == 5
        err = capsys.readouterr().err
        assert "flash failed" in err.lower()
        assert "mass erase failed" in err.lower()

    def test_supplied_log_receives_messages_and_stderr_stays_clean(self, monkeypatch, capsys, valid_hex):
        messages: list[str] = []

        def fake_run(cmd, **kw):
            if "flash" in cmd:
                return _result(1, _LOCKED_SIGNATURE_LINES)
            elif "erase" in cmd:
                return _result(5)
            return _result(0)

        monkeypatch.setattr(subprocess, "Popen", fake_run)

        rc = flash_mod.flash_hex(
            _UID, valid_hex, target_mcu=_MCU, log=messages.append
        )

        assert rc == 5
        assert any("flash failed" in m.lower() for m in messages)
        assert any("mass erase failed" in m.lower() for m in messages)
        assert capsys.readouterr().err == ""


class TestStreamedOutputRelay:
    """Regression guard: a single ``flash_hex`` call whose (faked) pyocd
    subprocess emits several lines of progress output must route every
    one of them through ``log`` individually -- not batched into one
    call, not dropped, not limited to a few fixed status messages.
    """

    def test_multiple_pyocd_lines_are_each_relayed_to_log(self, monkeypatch, valid_hex):
        messages: list[str] = []
        flash_progress = (
            "Erasing...",
            "Programming...",
            "Erased 463872 bytes (114 sectors), "
            "programmed 463872 bytes (114 pages) at 13.96 kB/s",
        )
        reset_progress = ("Resetting target.",)

        def fake_run(cmd, **kw):
            if "flash" in cmd:
                return _result(0, flash_progress)
            return _result(0, reset_progress)

        monkeypatch.setattr(subprocess, "Popen", fake_run)

        rc = flash_mod.flash_hex(
            _UID, valid_hex, target_mcu=_MCU, log=messages.append
        )

        assert rc == 0
        # Every streamed line arrived as its own log() call, in order,
        # with no coalescing and none dropped.
        assert messages == list(flash_progress) + list(reset_progress)

    def test_multiple_pyocd_lines_each_print_to_stderr_when_log_is_none(
        self, monkeypatch, capsys, valid_hex
    ):
        flash_progress = ("Erasing...", "Programming...", "Verifying...")

        def fake_run(cmd, **kw):
            if "flash" in cmd:
                return _result(0, flash_progress)
            return _result(0, ())

        monkeypatch.setattr(subprocess, "Popen", fake_run)

        rc = flash_mod.flash_hex(_UID, valid_hex, target_mcu=_MCU)

        assert rc == 0
        err_lines = capsys.readouterr().err.splitlines()
        assert err_lines == list(flash_progress)


class TestTransientRetry:
    """A transient probe/communication signature on the first flash gets
    exactly one blind retry, logged visibly, before anything else
    (including a mass-erase decision) is considered.
    """

    def test_transient_signature_retries_once_and_succeeds(
        self, monkeypatch, valid_hex
    ):
        """A probe timeout on the first flash retries once and succeeds,
        with no mass erase anywhere in the invocation sequence."""
        calls: list[list[str]] = []
        state = {"flash": 0}
        messages: list[str] = []

        def fake_run(cmd, **kw):
            calls.append(cmd)
            if "flash" in cmd:
                state["flash"] += 1
                if state["flash"] == 1:
                    return _result(1, ("Timeout reading from probe.",))
                return _result(0)
            return _result(0)  # reset

        monkeypatch.setattr(subprocess, "Popen", fake_run)

        rc = flash_mod.flash_hex(
            _UID, valid_hex, target_mcu=_MCU, log=messages.append
        )

        assert rc == 0
        assert state["flash"] == 2
        assert not any("erase" in c for c in calls)
        assert any(
            "retry" in m.lower() or "retrying" in m.lower() for m in messages
        )

    def test_two_consecutive_transient_failures_retries_only_once(
        self, monkeypatch, valid_hex
    ):
        """Two consecutive transient failures give up after the one
        retry -- not a loop -- and, since a merely-transient signature is
        never treated as locked, fail without ever mass-erasing,
        returning the retried flash's own rc.
        """
        calls: list[list[str]] = []
        state = {"flash": 0}

        def fake_run(cmd, **kw):
            calls.append(cmd)
            if "flash" in cmd:
                state["flash"] += 1
                return _result(1, ("DAPAccess Error: some transient fault",))
            return _result(0)

        monkeypatch.setattr(subprocess, "Popen", fake_run)

        rc = flash_mod.flash_hex(_UID, valid_hex, target_mcu=_MCU)

        assert rc == 1                                   # the retried flash's own rc
        assert state["flash"] == 2                        # exactly one retry, not a loop
        assert not any("erase" in c for c in calls)        # never reaches the erase branch

    def test_non_transient_failure_is_not_retried(self, monkeypatch, valid_hex):
        """A failure with no transient signature is not retried at all,
        and -- since it is also not a locked signature -- never
        mass-erases either: an unrecognized pyocd failure must never
        trigger erase --mass."""
        calls: list[list[str]] = []
        state = {"flash": 0}

        def fake_run(cmd, **kw):
            calls.append(cmd)
            if "flash" in cmd:
                state["flash"] += 1
                return _result(1, ("some unrelated pyocd error",))
            return _result(0)

        monkeypatch.setattr(subprocess, "Popen", fake_run)

        rc = flash_mod.flash_hex(_UID, valid_hex, target_mcu=_MCU)

        assert rc == 1
        assert state["flash"] == 1                        # no retry at all
        assert not any("erase" in c for c in calls)


class TestSignatureGating:
    """Mass erase fires only for a locked/protected signature.

    Before this behavior, ``flash_hex`` mass-erased on *any* non-zero
    pyocd exit, which a real field report shows wiping a working board
    over a malformed hex file (the hex-validation pre-flight already
    stops that specific case before it reaches pyocd at all; this class
    covers every other failure shape that reaches a real pyocd
    invocation).
    """

    def test_0x67_sector_erase_failure_triggers_mass_erase(
        self, monkeypatch, valid_hex
    ):
        """A 0x67 sector-erase failure triggers mass-erase recovery --
        the exact signature docs/acceptance/001-hardware.md's braeburn
        finding hit on real hardware."""
        calls: list[list[str]] = []
        state = {"flash": 0}

        def fake_run(cmd, **kw):
            calls.append(cmd)
            if "flash" in cmd:
                state["flash"] += 1
                if state["flash"] == 1:
                    return _result(1, ("flash erase sector failure (0x67)",))
                return _result(0)
            return _result(0)

        monkeypatch.setattr(subprocess, "Popen", fake_run)

        rc = flash_mod.flash_hex(_UID, valid_hex, target_mcu=_MCU)

        assert rc == 0
        assert state["flash"] == 2
        assert any("erase" in c and "--mass" in c for c in calls)

    def test_approtect_signature_triggers_mass_erase(self, monkeypatch, valid_hex):
        """APPROTECT wording is also recognized as a locked signature."""
        calls: list[list[str]] = []
        state = {"flash": 0}

        def fake_run(cmd, **kw):
            calls.append(cmd)
            if "flash" in cmd:
                state["flash"] += 1
                if state["flash"] == 1:
                    return _result(1, ("Error: APPROTECT is enabled.",))
                return _result(0)
            return _result(0)

        monkeypatch.setattr(subprocess, "Popen", fake_run)

        rc = flash_mod.flash_hex(_UID, valid_hex, target_mcu=_MCU)

        assert rc == 0
        assert any("erase" in c and "--mass" in c for c in calls)

    def test_malformed_hex_never_reaches_erase_mass(self, monkeypatch, tmp_path):
        """The headline regression: a malformed hex must never appear
        anywhere near an ``erase --mass`` invocation -- the hex-validation
        pre-flight stops this before any subprocess runs at all; this
        asserts it directly at the flash_hex level, independent of how
        the guard is implemented internally."""
        bad_path = tmp_path / "bad.hex"
        bad_path.write_text("not a valid hex file\n")

        calls: list[list[str]] = []

        def fake_run(cmd, **kw):
            calls.append(cmd)
            return _result(0)

        monkeypatch.setattr(subprocess, "Popen", fake_run)

        rc = flash_mod.flash_hex(_UID, str(bad_path), target_mcu=_MCU)

        assert rc != 0
        assert not any("erase" in c and "--mass" in c for c in calls)
        assert calls == []

    def test_bad_target_mcu_style_failure_never_erases(self, monkeypatch, valid_hex):
        """An unrecognized pyocd error (e.g. an unknown --target-mcu)
        fails outright -- it is never treated as 'assume locked'."""
        calls: list[list[str]] = []
        state = {"flash": 0}

        def fake_run(cmd, **kw):
            calls.append(cmd)
            if "flash" in cmd:
                state["flash"] += 1
                return _result(1, ("Target type nrf99999 is not recognized.",))
            return _result(0)

        monkeypatch.setattr(subprocess, "Popen", fake_run)

        rc = flash_mod.flash_hex(_UID, valid_hex, target_mcu="nrf99999")

        assert rc == 1
        assert state["flash"] == 1
        assert not any("erase" in c for c in calls)


class TestBlankBoardMessage:
    """An erase-then-failed-reflash must say, explicitly and unmissably,
    that the board is now blank -- through ``log``, not only local
    stderr -- naming the board.
    """

    def _fake_locked_then_still_failing(self):
        """flash always fails with a locked signature; erase succeeds;
        the retried flash still fails -- the exact erase-then-failed-
        reflash scenario this class covers."""
        state = {"flash": 0}

        def fake_run(cmd, **kw):
            if "flash" in cmd:
                state["flash"] += 1
                return _result(7, _LOCKED_SIGNATURE_LINES)
            return _result(0)  # erase succeeds

        return fake_run

    def test_blank_board_message_names_the_board_via_log(self, monkeypatch, valid_hex):
        messages: list[str] = []
        monkeypatch.setattr(
            subprocess, "Popen", self._fake_locked_then_still_failing()
        )

        rc = flash_mod.flash_hex(
            _UID, valid_hex, target_mcu=_MCU,
            log=messages.append, board_name="gutov-main",
        )

        assert rc == 7                                    # unchanged rc contract
        assert any(
            "gutov-main" in m and "no firmware" in m.lower() for m in messages
        )

    def test_blank_board_message_falls_back_to_uid_without_board_name(
        self, monkeypatch, capsys, valid_hex
    ):
        monkeypatch.setattr(
            subprocess, "Popen", self._fake_locked_then_still_failing()
        )

        rc = flash_mod.flash_hex(_UID, valid_hex, target_mcu=_MCU)

        assert rc == 7
        err = capsys.readouterr().err
        assert _UID in err
        assert "no firmware" in err.lower()

    def test_blank_board_message_reaches_log_not_only_stderr(
        self, monkeypatch, capsys, valid_hex
    ):
        """The message must be routed through a supplied ``log`` --
        stderr must stay clean when a caller supplies one, exactly like
        every other flash_hex message (see TestLogRouting)."""
        messages: list[str] = []
        monkeypatch.setattr(
            subprocess, "Popen", self._fake_locked_then_still_failing()
        )

        rc = flash_mod.flash_hex(
            _UID, valid_hex, target_mcu=_MCU,
            log=messages.append, board_name="gutov-main",
        )

        assert rc == 7
        assert capsys.readouterr().err == ""
        assert any("no firmware" in m.lower() for m in messages)

    def test_erase_failure_makes_no_blank_board_claim(self, monkeypatch, valid_hex):
        """If the mass erase itself fails, the firmware is generally
        still intact -- no blank-board claim should ever be made."""
        messages: list[str] = []

        def fake_run(cmd, **kw):
            if "flash" in cmd:
                return _result(1, _LOCKED_SIGNATURE_LINES)
            elif "erase" in cmd:
                return _result(5)
            return _result(0)

        monkeypatch.setattr(subprocess, "Popen", fake_run)

        rc = flash_mod.flash_hex(
            _UID, valid_hex, target_mcu=_MCU,
            log=messages.append, board_name="gutov-main",
        )

        assert rc == 5
        assert not any("no firmware" in m.lower() for m in messages)
        assert any("mass erase failed" in m.lower() for m in messages)


class TestHexValidation:
    """A bad hex file must never reach pyocd at all."""

    def test_malformed_hex_rejected_with_zero_subprocess_calls(
        self, monkeypatch, tmp_path
    ):
        bad_path = tmp_path / "bad.hex"
        bad_path.write_text("this is not a valid hex file\n")

        calls: list[list[str]] = []

        def fake_run(cmd, **kw):
            calls.append(cmd)
            return _result(0)

        monkeypatch.setattr(subprocess, "Popen", fake_run)

        messages: list[str] = []
        rc = flash_mod.flash_hex(
            _UID, str(bad_path), target_mcu=_MCU, log=messages.append
        )

        assert rc != 0
        assert calls == []
        assert any("hex" in m.lower() for m in messages)

    def test_missing_hex_rejected_with_zero_subprocess_calls(
        self, monkeypatch, tmp_path
    ):
        missing_path = tmp_path / "does-not-exist.hex"

        calls: list[list[str]] = []

        def fake_run(cmd, **kw):
            calls.append(cmd)
            return _result(0)

        monkeypatch.setattr(subprocess, "Popen", fake_run)

        messages: list[str] = []
        rc = flash_mod.flash_hex(
            _UID, str(missing_path), target_mcu=_MCU, log=messages.append
        )

        assert rc != 0
        assert calls == []
        assert any("hex" in m.lower() for m in messages)


def test_default_mcu_reused_from_registry_flash():
    """DEFAULT_MCU must be the same object registry.flash defines --
    ticket 005's acceptance criteria: reuse it, don't redefine a second
    copy."""
    from mbtools.registry.flash import DEFAULT_MCU as registry_default_mcu

    assert flash_mod.DEFAULT_MCU is registry_default_mcu
