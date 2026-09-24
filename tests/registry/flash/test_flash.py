"""Tests for mbtools.registry.flash -- the registry's minimal pyOCD flash
operation (ticket 007).

Per sprint.md's Test Strategy ("the pyOCD invocation is behind an
injectable callable ... so flash tests never shell out to a real
pyocd"), every test here injects a fake ``runner`` -- none shells out to a
real ``pyocd`` binary or touches a real probe. ``locks``/``store`` are
real :class:`~mbtools.registry.locks.LockManager`/
:class:`~mbtools.registry.store.Store` instances (a real SQLite file in
``tmp_path``), matching the rest of this sprint's "fakes only at the true
I/O boundary" convention.
"""

from __future__ import annotations

import os
import subprocess
import threading
import time

import intelhex
import pytest

from mbtools.registry.flash import (
    DEFAULT_NO_PROGRESS_TIMEOUT_S,
    FlashLockNotHeldError,
    FlashOp,
    FlashResult,
    HexValidationError,
    check_device_permission,
    looks_permission_denied,
    run_streamed_with_watchdog,
)
from mbtools.registry.locks import KIND_FLASH, KIND_SERIAL, HolderRef, LockManager
from mbtools.registry.store import Store

UID = "9900" + "0000" + "11112222" + "3333444455556666" + "77778888" + "6e052820"
PID = 4242
HOLDER = HolderRef(origin="local", ref=str(PID), pid=PID)
VID_PID = "0d28:0204"


@pytest.fixture
def store(tmp_path):
    s = Store(tmp_path / "devices.db")
    s.upsert_attached(UID, "/dev/ttyACM0", VID_PID)
    return s


@pytest.fixture
def locks():
    return LockManager()


def _valid_hex_path(tmp_path) -> str:
    """A hex file that parses cleanly with intelhex."""
    ih = intelhex.IntelHex()
    ih[0x0000] = 0xFF
    path = tmp_path / "firmware.hex"
    ih.write_hex_file(str(path))
    return str(path)


class _SpyRunner:
    """A fake pyOCD-subprocess runner: records the argv it was called
    with, relays a scripted sequence of log lines through ``log`` in
    order, then either returns a scripted exit code or raises a scripted
    exception. Never touches a real subprocess."""

    def __init__(self, lines: list[str] = (), exit_code: int = 0, raises: Exception | None = None):
        self._lines = list(lines)
        self._exit_code = exit_code
        self._raises = raises
        self.calls: list[list[str]] = []
        self.relayed: list[str] = []

    def __call__(self, cmd: list[str], log) -> int:
        self.calls.append(cmd)
        for line in self._lines:
            log(line)
            self.relayed.append(line)
        if self._raises is not None:
            raise self._raises
        return self._exit_code


# ---------------------------------------------------------------------------
# hex validation short-circuits before pyocd
# ---------------------------------------------------------------------------


def test_missing_hex_file_raises_before_pyocd_and_does_not_increment(store, locks, tmp_path):
    locks.acquire(UID, KIND_FLASH, HOLDER)
    runner = _SpyRunner()
    op = FlashOp(locks=locks, store=store, runner=runner)

    with pytest.raises(HexValidationError):
        op.flash_hex(UID, str(tmp_path / "does-not-exist.hex"), log=lambda line: None)

    assert runner.calls == []
    assert store.get(UID).flash_count == 0


def test_malformed_hex_file_raises_before_pyocd_and_does_not_increment(store, locks, tmp_path):
    locks.acquire(UID, KIND_FLASH, HOLDER)
    bad_hex = tmp_path / "bad.hex"
    bad_hex.write_text("this is not a valid intel hex file\n")
    runner = _SpyRunner()
    op = FlashOp(locks=locks, store=store, runner=runner)

    with pytest.raises(HexValidationError):
        op.flash_hex(UID, str(bad_hex), log=lambda line: None)

    assert runner.calls == []
    assert store.get(UID).flash_count == 0


# ---------------------------------------------------------------------------
# lock contract
# ---------------------------------------------------------------------------


def test_flash_without_any_lock_is_refused(store, locks, tmp_path):
    runner = _SpyRunner()
    op = FlashOp(locks=locks, store=store, runner=runner)
    hex_path = _valid_hex_path(tmp_path)

    with pytest.raises(FlashLockNotHeldError):
        op.flash_hex(UID, hex_path, log=lambda line: None)

    assert runner.calls == []
    assert store.get(UID).flash_count == 0


def test_flash_with_wrong_kind_lock_is_refused(store, locks, tmp_path):
    locks.acquire(UID, KIND_SERIAL, HOLDER)
    runner = _SpyRunner()
    op = FlashOp(locks=locks, store=store, runner=runner)
    hex_path = _valid_hex_path(tmp_path)

    with pytest.raises(FlashLockNotHeldError):
        op.flash_hex(UID, hex_path, log=lambda line: None)

    assert runner.calls == []
    assert store.get(UID).flash_count == 0


# ---------------------------------------------------------------------------
# successful flash
# ---------------------------------------------------------------------------


def test_successful_flash_increments_count_and_relays_log_in_order(store, locks, tmp_path):
    locks.acquire(UID, KIND_FLASH, HOLDER)
    lines = ["erasing...", "programming...", "verifying..."]
    runner = _SpyRunner(lines=lines, exit_code=0)
    op = FlashOp(locks=locks, store=store, runner=runner)
    hex_path = _valid_hex_path(tmp_path)
    seen: list[str] = []

    result = op.flash_hex(UID, hex_path, log=seen.append)

    assert result == FlashResult(success=True, exit_code=0)
    assert seen == lines  # relayed in order, as the runner produced them
    assert store.get(UID).flash_count == 1
    # pyocd flash invoked exactly once, with the expected shape.
    assert len(runner.calls) == 1
    cmd = runner.calls[0]
    assert cmd[-3:] == ["--uid", UID, hex_path]
    assert "flash" in cmd


def test_flash_hex_never_releases_the_lock(store, locks, tmp_path):
    locks.acquire(UID, KIND_FLASH, HOLDER)
    runner = _SpyRunner(exit_code=0)
    op = FlashOp(locks=locks, store=store, runner=runner)
    hex_path = _valid_hex_path(tmp_path)

    op.flash_hex(UID, hex_path, log=lambda line: None)

    # Releasing the lock is the caller's job (per the module docstring's
    # "Contract with locks and store") -- flash_hex must not do it itself.
    assert locks.status(UID) is not None


# ---------------------------------------------------------------------------
# failed flash -- non-zero exit
# ---------------------------------------------------------------------------


def test_nonzero_exit_still_increments_count_and_reports_failure(store, locks, tmp_path):
    locks.acquire(UID, KIND_FLASH, HOLDER)
    runner = _SpyRunner(lines=["some error output"], exit_code=1)
    op = FlashOp(locks=locks, store=store, runner=runner)
    hex_path = _valid_hex_path(tmp_path)

    result = op.flash_hex(UID, hex_path, log=lambda line: None)

    assert result.success is False
    assert result.exit_code == 1
    assert result.error is not None
    assert store.get(UID).flash_count == 1


# ---------------------------------------------------------------------------
# failed flash -- injected runner raises
# ---------------------------------------------------------------------------


def test_runner_raising_still_increments_count_and_reports_failure(store, locks, tmp_path):
    locks.acquire(UID, KIND_FLASH, HOLDER)
    runner = _SpyRunner(raises=OSError("probe disconnected mid-flash"))
    op = FlashOp(locks=locks, store=store, runner=runner)
    hex_path = _valid_hex_path(tmp_path)

    result = op.flash_hex(UID, hex_path, log=lambda line: None)

    assert result.success is False
    assert result.exit_code is None
    assert "probe disconnected mid-flash" in result.error
    assert store.get(UID).flash_count == 1


# ---------------------------------------------------------------------------
# ticket 009: permission pre-check, resolved via the store's own port
# ---------------------------------------------------------------------------


class TestPermissionPrecheck:
    """``FlashOp.flash_hex`` resolves ``uid``'s port itself, via the same
    ``store`` this class already holds (no new constructor parameter
    needed) -- see the method's own docstring, step 3."""

    def test_no_read_write_access_fails_before_pyocd_and_does_not_increment(
        self, store, locks, tmp_path
    ):
        if os.name != "posix":
            pytest.skip("os.access permission bits are POSIX-specific")
        if hasattr(os, "geteuid") and os.geteuid() == 0:
            pytest.skip("root bypasses permission bits -- can't simulate denial")

        device_path = tmp_path / "fake-cmsis-dap-device"
        device_path.write_bytes(b"")
        device_path.chmod(0o000)
        try:
            store.upsert_attached(UID, str(device_path), VID_PID)
            locks.acquire(UID, KIND_FLASH, HOLDER)
            runner = _SpyRunner()
            op = FlashOp(locks=locks, store=store, runner=runner)
            hex_path = _valid_hex_path(tmp_path)
            messages: list[str] = []

            result = op.flash_hex(UID, hex_path, log=messages.append)

            assert result.success is False
            assert result.exit_code is None
            assert "permission" in result.error.lower()
            assert runner.calls == []  # pyocd's runner never invoked
            # A pre-check failure never ran pyocd, so it's not an
            # "attempted" flash -- same reasoning as HexValidationError.
            assert store.get(UID).flash_count == 0
            assert any("permission" in m.lower() for m in messages)
        finally:
            device_path.chmod(0o644)

    def test_accessible_port_does_not_block_the_flash(self, store, locks, tmp_path):
        device_path = tmp_path / "fake-cmsis-dap-device"
        device_path.write_bytes(b"")
        store.upsert_attached(UID, str(device_path), VID_PID)
        locks.acquire(UID, KIND_FLASH, HOLDER)
        runner = _SpyRunner(exit_code=0)
        op = FlashOp(locks=locks, store=store, runner=runner)
        hex_path = _valid_hex_path(tmp_path)

        result = op.flash_hex(UID, hex_path, log=lambda line: None)

        assert result.success is True
        assert len(runner.calls) == 1
        assert store.get(UID).flash_count == 1

    def test_missing_or_unresolved_port_is_skipped_not_a_failure(
        self, store, locks, tmp_path
    ):
        """The ``store`` fixture's default port (``/dev/ttyACM0``) does
        not exist in the test environment -- the pre-check must defer to
        pyocd rather than misreport a nonexistent path as a permission
        failure. This is also the regression guard for every
        pre-ticket-009 test above, which all rely on exactly this."""
        locks.acquire(UID, KIND_FLASH, HOLDER)
        runner = _SpyRunner(exit_code=0)
        op = FlashOp(locks=locks, store=store, runner=runner)
        hex_path = _valid_hex_path(tmp_path)

        result = op.flash_hex(UID, hex_path, log=lambda line: None)

        assert result.success is True
        assert len(runner.calls) == 1


# ---------------------------------------------------------------------------
# ticket 009: the shared low-level helpers directly -- check_device_
# permission / looks_permission_denied / run_streamed_with_watchdog.
# ``flashlogic.py``'s own test file (via ``deploy.flash.flash_hex``)
# covers these end-to-end; this file covers them at their own module
# boundary, since they're now public API here, not private to flashlogic.
# ---------------------------------------------------------------------------


class TestCheckDevicePermission:
    def test_none_port_is_skipped(self):
        assert check_device_permission(None) is None

    def test_empty_port_is_skipped(self):
        assert check_device_permission("") is None

    def test_nonexistent_port_is_skipped(self, tmp_path):
        assert check_device_permission(str(tmp_path / "nope")) is None

    def test_accessible_port_passes(self, tmp_path):
        path = tmp_path / "device"
        path.write_bytes(b"")
        assert check_device_permission(str(path)) is None

    def test_inaccessible_port_returns_actionable_message(self, tmp_path):
        if os.name != "posix":
            pytest.skip("os.access permission bits are POSIX-specific")
        if hasattr(os, "geteuid") and os.geteuid() == 0:
            pytest.skip("root bypasses permission bits -- can't simulate denial")

        path = tmp_path / "device"
        path.write_bytes(b"")
        path.chmod(0o000)
        try:
            message = check_device_permission(str(path))
            assert message is not None
            assert str(path) in message
            assert "install-service" in message  # points at the ticket 008 fix
        finally:
            path.chmod(0o644)


class TestLooksPermissionDenied:
    @pytest.mark.parametrize(
        "text",
        [
            "[Errno 13] Access denied (insufficient permissions)",
            "This can probably be remedied with a udev rule.",
            "Unable to open device: permission denied",
        ],
    )
    def test_real_pyocd_wording_matches(self, text):
        assert looks_permission_denied(text) is True

    @pytest.mark.parametrize(
        "text",
        [
            "Timeout reading from probe.",
            "Erasing sector 4...",
            "",
        ],
    )
    def test_unrelated_output_does_not_match(self, text):
        assert looks_permission_denied(text) is False


class TestRunStreamedWithWatchdog:
    """Direct tests of the shared helper both ``flashlogic._run_streamed``
    and ``flash._default_runner`` now delegate to."""

    def test_relays_lines_and_returns_exit_code(self, monkeypatch):
        class _Fake:
            def __init__(self):
                self.stdout = iter(["one\n", "two\n"])

            def wait(self):
                return 0

        monkeypatch.setattr(subprocess, "Popen", lambda cmd, **kw: _Fake())
        messages: list[str] = []

        rc, output = run_streamed_with_watchdog(["pyocd"], messages.append)

        assert rc == 0
        assert messages == ["one", "two"]
        assert output == "one\ntwo"

    def test_no_progress_timeout_kills_and_returns_nonzero(self, monkeypatch):
        class _Hanging:
            def __init__(self):
                self._stop = threading.Event()
                self.killed = False
                self.stdout = self

            def __iter__(self):
                return self

            def __next__(self):
                self._stop.wait()
                raise StopIteration

            def kill(self):
                self.killed = True
                self._stop.set()

            def wait(self):
                return 0  # even a platform quirk returning 0 must not read as success

        proc = _Hanging()
        monkeypatch.setattr(subprocess, "Popen", lambda cmd, **kw: proc)
        messages: list[str] = []

        started = time.monotonic()
        rc, output = run_streamed_with_watchdog(
            ["pyocd"], messages.append, no_progress_timeout=0.2
        )
        elapsed = time.monotonic() - started

        assert rc != 0  # forced non-zero despite the fake's own rc == 0
        assert proc.killed
        assert elapsed < 5.0
        assert output == ""
        assert any("no output" in m.lower() for m in messages)

    def test_default_timeout_constant_is_conservative(self):
        # A cheap guard against an accidental one-character typo (e.g.
        # "6.0" instead of "60.0") turning the production default into
        # something that would false-positive on a real flash.
        assert DEFAULT_NO_PROGRESS_TIMEOUT_S >= 30.0
