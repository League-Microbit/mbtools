"""Tests for mbtools.registry.locks -- the exclusive lock manager,
kind-tagged, holder identity generalized to local (PID-tied) or remote
(session-tied) (ticket 005, generalized by ticket 002).

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
    HolderRef,
    LockHeldError,
    LockManager,
    LockStatus,
)

UID = "uid-1"
UID2 = "uid-2"
PID = 1001
PID2 = 1002


def _local(pid: int) -> HolderRef:
    """The same construction api.py uses for a real local connection
    (ticket 002, sprint.md Decision 2) -- ``ref`` mirrors ``pid`` as a
    string."""
    return HolderRef(origin="local", ref=str(pid), pid=pid)


HOLDER = _local(PID)
HOLDER2 = _local(PID2)


# ---------------------------------------------------------------------------
# acquire / status -- happy path
# ---------------------------------------------------------------------------


def test_acquire_grants_lock_on_unlocked_device():
    manager = LockManager()

    granted = manager.acquire(UID, KIND_SERIAL, HOLDER)

    assert granted is True
    assert manager.status(UID) == LockStatus(kind=KIND_SERIAL, holder=HOLDER)


def test_status_none_for_unlocked_device():
    manager = LockManager()
    assert manager.status(UID) is None


# ---------------------------------------------------------------------------
# acquire -- conflict
# ---------------------------------------------------------------------------


def test_acquire_when_already_locked_raises_with_holder_info():
    manager = LockManager()
    manager.acquire(UID, KIND_SERIAL, HOLDER)

    with pytest.raises(LockHeldError) as exc_info:
        manager.acquire(UID, KIND_FLASH, HOLDER2)

    err = exc_info.value
    assert err.uid == UID
    assert err.holder == LockStatus(kind=KIND_SERIAL, holder=HOLDER)


def test_failed_acquire_does_not_mutate_state():
    manager = LockManager()
    manager.acquire(UID, KIND_SERIAL, HOLDER)

    with pytest.raises(LockHeldError):
        manager.acquire(UID, KIND_FLASH, HOLDER2)

    # Original holder is untouched -- not overwritten, not cleared.
    assert manager.status(UID) == LockStatus(kind=KIND_SERIAL, holder=HOLDER)


# ---------------------------------------------------------------------------
# release
# ---------------------------------------------------------------------------


def test_release_by_current_holder_unlocks_device():
    manager = LockManager()
    manager.acquire(UID, KIND_SERIAL, HOLDER)

    released = manager.release(UID, HOLDER)

    assert released is True
    assert manager.status(UID) is None


def test_release_with_wrong_holder_is_noop():
    manager = LockManager()
    manager.acquire(UID, KIND_SERIAL, HOLDER)

    released = manager.release(UID, HOLDER2)

    assert released is False
    assert manager.status(UID) == LockStatus(kind=KIND_SERIAL, holder=HOLDER)


def test_release_of_unlocked_device_is_noop():
    manager = LockManager()
    assert manager.release(UID, HOLDER) is False


# ---------------------------------------------------------------------------
# idempotent unlocked state
# ---------------------------------------------------------------------------


def test_never_locked_and_released_devices_are_indistinguishable():
    never_locked = LockManager()

    released = LockManager()
    released.acquire(UID, KIND_SERIAL, HOLDER)
    released.release(UID, HOLDER)

    assert never_locked.status(UID) is None
    assert released.status(UID) is None
    # Both are lockable identically afterwards.
    assert never_locked.acquire(UID, KIND_DEBUG, HOLDER2) is True
    assert released.acquire(UID, KIND_DEBUG, HOLDER2) is True


# ---------------------------------------------------------------------------
# sweep
# ---------------------------------------------------------------------------


def test_sweep_releases_only_dead_holders():
    manager = LockManager()
    manager.acquire(UID, KIND_SERIAL, HOLDER)
    manager.acquire(UID2, KIND_DEBUG, HOLDER2)

    def is_alive(holder: HolderRef) -> bool:
        return holder == HOLDER2  # HOLDER is dead, HOLDER2 is alive

    released_uids = manager.sweep(is_alive)

    assert released_uids == [UID]
    assert manager.status(UID) is None
    assert manager.status(UID2) == LockStatus(kind=KIND_DEBUG, holder=HOLDER2)


def test_sweep_with_all_holders_alive_releases_nothing():
    manager = LockManager()
    manager.acquire(UID, KIND_SERIAL, HOLDER)

    released_uids = manager.sweep(lambda holder: True)

    assert released_uids == []
    assert manager.status(UID) == LockStatus(kind=KIND_SERIAL, holder=HOLDER)


# ---------------------------------------------------------------------------
# HolderRef generalization -- local and remote (ticket 002)
# ---------------------------------------------------------------------------


def test_remote_holder_acquire_release_sweep_conflict_reporting():
    """Proves the generalization is sound ahead of ticket 006 actually
    wiring a real remote caller: a HolderRef(origin="remote", ...)
    behaves identically to a local one for acquire/release/sweep/
    conflict-reporting -- no real network involved, LockManager
    constructed and driven directly.
    """
    manager = LockManager()
    remote_holder = HolderRef(
        origin="remote", ref="session-abc", host="loki"
    )

    # acquire
    assert manager.acquire(UID, KIND_SERIAL, remote_holder) is True
    assert manager.status(UID) == LockStatus(kind=KIND_SERIAL, holder=remote_holder)
    assert manager.status(UID).pid is None  # remote holder has no pid

    # conflict-reporting: a second acquire on the same uid is refused,
    # and the refusal carries the full remote holder identity.
    with pytest.raises(LockHeldError) as exc_info:
        manager.acquire(UID, KIND_FLASH, HOLDER)
    assert exc_info.value.holder.holder == remote_holder

    # release: only the exact remote holder can release it.
    assert manager.release(UID, HOLDER) is False  # wrong holder -- no-op
    assert manager.release(UID, remote_holder) is True
    assert manager.status(UID) is None

    # sweep: an is_alive callable dispatching on origin works the same
    # way for a remote holder as for a local one.
    assert manager.acquire(UID, KIND_DEBUG, remote_holder) is True

    def is_alive(holder: HolderRef) -> bool:
        return holder.origin != "remote"  # remote holders are "dead" here

    released_uids = manager.sweep(is_alive)
    assert released_uids == [UID]
    assert manager.status(UID) is None


def test_local_and_remote_holder_conflict_on_same_uid_is_refused():
    """Decision 2's single-table exclusivity property: a local and a
    remote holder can never both hold a lock on the same uid -- proves
    the double-lock bug the generalization exists to prevent is closed.
    """
    manager = LockManager()
    remote_holder = HolderRef(origin="remote", ref="session-xyz", host="hodr")

    assert manager.acquire(UID, KIND_SERIAL, HOLDER) is True

    with pytest.raises(LockHeldError) as exc_info:
        manager.acquire(UID, KIND_SERIAL, remote_holder)
    assert exc_info.value.holder.holder == HOLDER

    # And the reverse direction.
    manager2 = LockManager()
    assert manager2.acquire(UID, KIND_SERIAL, remote_holder) is True
    with pytest.raises(LockHeldError) as exc_info2:
        manager2.acquire(UID, KIND_SERIAL, HOLDER)
    assert exc_info2.value.holder.holder == remote_holder


# ---------------------------------------------------------------------------
# flash-release callback
# ---------------------------------------------------------------------------


def test_release_fires_flash_callback_for_flash_kind():
    fired = []
    manager = LockManager(flash_release_callback=fired.append)
    manager.acquire(UID, KIND_FLASH, HOLDER)

    manager.release(UID, HOLDER)

    assert fired == [UID]


def test_release_does_not_fire_flash_callback_for_non_flash_kind():
    fired = []
    manager = LockManager(flash_release_callback=fired.append)
    manager.acquire(UID, KIND_SERIAL, HOLDER)

    manager.release(UID, HOLDER)

    assert fired == []


def test_noop_release_does_not_fire_flash_callback():
    fired = []
    manager = LockManager(flash_release_callback=fired.append)
    manager.acquire(UID, KIND_FLASH, HOLDER)

    manager.release(UID, HOLDER2)  # wrong holder -- no-op

    assert fired == []


def test_sweep_fires_flash_callback_for_dead_flash_holder():
    fired = []
    manager = LockManager(flash_release_callback=fired.append)
    manager.acquire(UID, KIND_FLASH, HOLDER)

    manager.sweep(lambda holder: False)

    assert fired == [UID]


def test_sweep_does_not_fire_flash_callback_for_dead_non_flash_holder():
    fired = []
    manager = LockManager(flash_release_callback=fired.append)
    manager.acquire(UID, KIND_SERIAL, HOLDER)

    manager.sweep(lambda holder: False)

    assert fired == []


def test_flash_callback_fires_exactly_once_per_release():
    calls = []
    manager = LockManager(flash_release_callback=calls.append)
    manager.acquire(UID, KIND_FLASH, HOLDER)

    manager.release(UID, HOLDER)
    # Already unlocked -- a second release is a no-op and must not refire.
    manager.release(UID, HOLDER)

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
        holder = _local(pid)
        assert manager.acquire(UID, KIND_SERIAL, holder) is True
        assert _real_is_pid_alive(pid) is True

        proc.kill()
        proc.wait()  # reap it -- os.kill(pid, 0) still finds a zombie

        released_uids = manager.sweep(lambda h: _real_is_pid_alive(h.pid))

        assert released_uids == [UID]
        assert manager.status(UID) is None
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait()
