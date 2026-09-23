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

import intelhex
import pytest

from mbtools.registry.flash import (
    FlashLockNotHeldError,
    FlashOp,
    FlashResult,
    HexValidationError,
)
from mbtools.registry.locks import KIND_FLASH, KIND_SERIAL, LockManager
from mbtools.registry.store import Store

UID = "9900" + "0000" + "11112222" + "3333444455556666" + "77778888" + "6e052820"
PID = 4242
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
    locks.acquire(UID, KIND_FLASH, PID)
    runner = _SpyRunner()
    op = FlashOp(locks=locks, store=store, runner=runner)

    with pytest.raises(HexValidationError):
        op.flash_hex(UID, str(tmp_path / "does-not-exist.hex"), log=lambda line: None)

    assert runner.calls == []
    assert store.get(UID).flash_count == 0


def test_malformed_hex_file_raises_before_pyocd_and_does_not_increment(store, locks, tmp_path):
    locks.acquire(UID, KIND_FLASH, PID)
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
    locks.acquire(UID, KIND_SERIAL, PID)
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
    locks.acquire(UID, KIND_FLASH, PID)
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
    locks.acquire(UID, KIND_FLASH, PID)
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
    locks.acquire(UID, KIND_FLASH, PID)
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
    locks.acquire(UID, KIND_FLASH, PID)
    runner = _SpyRunner(raises=OSError("probe disconnected mid-flash"))
    op = FlashOp(locks=locks, store=store, runner=runner)
    hex_path = _valid_hex_path(tmp_path)

    result = op.flash_hex(UID, hex_path, log=lambda line: None)

    assert result.success is False
    assert result.exit_code is None
    assert "probe disconnected mid-flash" in result.error
    assert store.get(UID).flash_count == 1
