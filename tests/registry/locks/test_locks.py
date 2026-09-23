"""Tests for mbtools.registry.locks -- the PID-tied exclusive lock
manager, kind-tagged (ticket 005).

Every test here is pure in-memory except the one at the bottom
(test_sweep_releases_lock_of_real_dead_subprocess), which spawns a real
subprocess and uses an os.kill(pid, 0)-based liveness check, per the
ticket's own acceptance criterion: "proving the injectable abstraction
matches real OS behavior, not just its own fakes."
"""

from __future__ import annotations

import os
import subprocess

import pytest

from mbtools.registry.locks import (
    KIND_DEBUG,
    KIND_FLASH,
    KIND_SERIAL,
    LockHeldError,
    LockManager,
    LockStatus,
)

UID = "uid-1"
UID2 = "uid-2"
PID = 1001
PID2 = 1002


# ---------------------------------------------------------------------------
# acquire / status -- happy path
# ---------------------------------------------------------------------------


def test_acquire_grants_lock_on_unlocked_device():
    manager = LockManager()

    granted = manager.acquire(UID, KIND_SERIAL, PID)

    assert granted is True
    assert manager.status(UID) == LockStatus(kind=KIND_SERIAL, pid=PID)


def test_status_none_for_unlocked_device():
    manager = LockManager()
    assert manager.status(UID) is None


# ---------------------------------------------------------------------------
# acquire -- conflict
# ---------------------------------------------------------------------------


def test_acquire_when_already_locked_raises_with_holder_info():
    manager = LockManager()
    manager.acquire(UID, KIND_SERIAL, PID)

    with pytest.raises(LockHeldError) as exc_info:
        manager.acquire(UID, KIND_FLASH, PID2)

    err = exc_info.value
    assert err.uid == UID
    assert err.holder == LockStatus(kind=KIND_SERIAL, pid=PID)


def test_failed_acquire_does_not_mutate_state():
    manager = LockManager()
    manager.acquire(UID, KIND_SERIAL, PID)

    with pytest.raises(LockHeldError):
        manager.acquire(UID, KIND_FLASH, PID2)

    # Original holder is untouched -- not overwritten, not cleared.
    assert manager.status(UID) == LockStatus(kind=KIND_SERIAL, pid=PID)


# ---------------------------------------------------------------------------
# release
# ---------------------------------------------------------------------------


def test_release_by_current_holder_unlocks_device():
    manager = LockManager()
    manager.acquire(UID, KIND_SERIAL, PID)

    released = manager.release(UID, PID)

    assert released is True
    assert manager.status(UID) is None


def test_release_with_wrong_pid_is_noop():
    manager = LockManager()
    manager.acquire(UID, KIND_SERIAL, PID)

    released = manager.release(UID, PID2)

    assert released is False
    assert manager.status(UID) == LockStatus(kind=KIND_SERIAL, pid=PID)


def test_release_of_unlocked_device_is_noop():
    manager = LockManager()
    assert manager.release(UID, PID) is False


# ---------------------------------------------------------------------------
# idempotent unlocked state
# ---------------------------------------------------------------------------


def test_never_locked_and_released_devices_are_indistinguishable():
    never_locked = LockManager()

    released = LockManager()
    released.acquire(UID, KIND_SERIAL, PID)
    released.release(UID, PID)

    assert never_locked.status(UID) is None
    assert released.status(UID) is None
    # Both are lockable identically afterwards.
    assert never_locked.acquire(UID, KIND_DEBUG, PID2) is True
    assert released.acquire(UID, KIND_DEBUG, PID2) is True


# ---------------------------------------------------------------------------
# sweep
# ---------------------------------------------------------------------------


def test_sweep_releases_only_dead_holders():
    manager = LockManager()
    manager.acquire(UID, KIND_SERIAL, PID)
    manager.acquire(UID2, KIND_DEBUG, PID2)

    def is_pid_alive(pid: int) -> bool:
        return pid == PID2  # PID is dead, PID2 is alive

    released_uids = manager.sweep(is_pid_alive)

    assert released_uids == [UID]
    assert manager.status(UID) is None
    assert manager.status(UID2) == LockStatus(kind=KIND_DEBUG, pid=PID2)


def test_sweep_with_all_holders_alive_releases_nothing():
    manager = LockManager()
    manager.acquire(UID, KIND_SERIAL, PID)

    released_uids = manager.sweep(lambda pid: True)

    assert released_uids == []
    assert manager.status(UID) == LockStatus(kind=KIND_SERIAL, pid=PID)


# ---------------------------------------------------------------------------
# flash-release callback
# ---------------------------------------------------------------------------


def test_release_fires_flash_callback_for_flash_kind():
    fired = []
    manager = LockManager(flash_release_callback=fired.append)
    manager.acquire(UID, KIND_FLASH, PID)

    manager.release(UID, PID)

    assert fired == [UID]


def test_release_does_not_fire_flash_callback_for_non_flash_kind():
    fired = []
    manager = LockManager(flash_release_callback=fired.append)
    manager.acquire(UID, KIND_SERIAL, PID)

    manager.release(UID, PID)

    assert fired == []


def test_noop_release_does_not_fire_flash_callback():
    fired = []
    manager = LockManager(flash_release_callback=fired.append)
    manager.acquire(UID, KIND_FLASH, PID)

    manager.release(UID, PID2)  # wrong pid -- no-op

    assert fired == []


def test_sweep_fires_flash_callback_for_dead_flash_holder():
    fired = []
    manager = LockManager(flash_release_callback=fired.append)
    manager.acquire(UID, KIND_FLASH, PID)

    manager.sweep(lambda pid: False)

    assert fired == [UID]


def test_sweep_does_not_fire_flash_callback_for_dead_non_flash_holder():
    fired = []
    manager = LockManager(flash_release_callback=fired.append)
    manager.acquire(UID, KIND_SERIAL, PID)

    manager.sweep(lambda pid: False)

    assert fired == []


def test_flash_callback_fires_exactly_once_per_release():
    calls = []
    manager = LockManager(flash_release_callback=calls.append)
    manager.acquire(UID, KIND_FLASH, PID)

    manager.release(UID, PID)
    # Already unlocked -- a second release is a no-op and must not refire.
    manager.release(UID, PID)

    assert calls == [UID]


# ---------------------------------------------------------------------------
# real-subprocess liveness integration test
# ---------------------------------------------------------------------------


def _real_is_pid_alive(pid: int) -> bool:
    """os.kill(pid, 0)-based liveness check -- the real mechanism a
    production ``is_pid_alive`` (wired up by daemon/api, tickets 006/008)
    would use, as opposed to this test suite's scripted fakes above."""
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        # Process exists, just isn't ours to signal -- still alive.
        return True
    return True


def test_sweep_releases_lock_of_real_dead_subprocess():
    manager = LockManager()
    proc = subprocess.Popen(["sleep", "30"])
    try:
        pid = proc.pid
        assert manager.acquire(UID, KIND_SERIAL, pid) is True
        assert _real_is_pid_alive(pid) is True

        proc.kill()
        proc.wait()  # reap it -- os.kill(pid, 0) still finds a zombie

        released_uids = manager.sweep(_real_is_pid_alive)

        assert released_uids == [UID]
        assert manager.status(UID) is None
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait()
