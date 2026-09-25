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
import sys

import pytest

from mbtools.registry.locks import (
    KIND_DEBUG,
    KIND_FLASH,
    KIND_SERIAL,
    HolderRef,
    LockHeldError,
    LockManager,
    LockStatus,
    format_lock_suffix,
)

UID = "uid-1"
UID2 = "uid-2"
PID = 1001
PID2 = 1002

#: A fixed clock (sprint 008, ticket 002) for tests that assert
#: ``LockStatus.since``/a lock-display callback's ``since`` argument by
#: value -- injected via ``LockManager(now_fn=...)`` so those assertions
#: don't depend on real wall-clock time.
FIXED_NOW = 1_700_000_000.0


def _fixed_now() -> float:
    return FIXED_NOW


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
    manager = LockManager(now_fn=_fixed_now)

    granted = manager.acquire(UID, KIND_SERIAL, HOLDER)

    assert granted is True
    assert manager.status(UID) == LockStatus(
        kind=KIND_SERIAL, holder=HOLDER, label=None, since=FIXED_NOW
    )


def test_status_none_for_unlocked_device():
    manager = LockManager()
    assert manager.status(UID) is None


# ---------------------------------------------------------------------------
# acquire -- conflict
# ---------------------------------------------------------------------------


def test_acquire_when_already_locked_raises_with_holder_info():
    manager = LockManager(now_fn=_fixed_now)
    manager.acquire(UID, KIND_SERIAL, HOLDER)

    with pytest.raises(LockHeldError) as exc_info:
        manager.acquire(UID, KIND_FLASH, HOLDER2)

    err = exc_info.value
    assert err.uid == UID
    assert err.holder == LockStatus(kind=KIND_SERIAL, holder=HOLDER, label=None, since=FIXED_NOW)


def test_failed_acquire_does_not_mutate_state():
    manager = LockManager(now_fn=_fixed_now)
    manager.acquire(UID, KIND_SERIAL, HOLDER)

    with pytest.raises(LockHeldError):
        manager.acquire(UID, KIND_FLASH, HOLDER2)

    # Original holder is untouched -- not overwritten, not cleared.
    assert manager.status(UID) == LockStatus(
        kind=KIND_SERIAL, holder=HOLDER, label=None, since=FIXED_NOW
    )


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
    manager = LockManager(now_fn=_fixed_now)
    manager.acquire(UID, KIND_SERIAL, HOLDER)

    released = manager.release(UID, HOLDER2)

    assert released is False
    assert manager.status(UID) == LockStatus(
        kind=KIND_SERIAL, holder=HOLDER, label=None, since=FIXED_NOW
    )


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
    manager = LockManager(now_fn=_fixed_now)
    manager.acquire(UID, KIND_SERIAL, HOLDER)
    manager.acquire(UID2, KIND_DEBUG, HOLDER2)

    def is_alive(holder: HolderRef) -> bool:
        return holder == HOLDER2  # HOLDER is dead, HOLDER2 is alive

    released_uids = manager.sweep(is_alive)

    assert released_uids == [UID]
    assert manager.status(UID) is None
    assert manager.status(UID2) == LockStatus(
        kind=KIND_DEBUG, holder=HOLDER2, label=None, since=FIXED_NOW
    )


def test_sweep_with_all_holders_alive_releases_nothing():
    manager = LockManager(now_fn=_fixed_now)
    manager.acquire(UID, KIND_SERIAL, HOLDER)

    released_uids = manager.sweep(lambda holder: True)

    assert released_uids == []
    assert manager.status(UID) == LockStatus(
        kind=KIND_SERIAL, holder=HOLDER, label=None, since=FIXED_NOW
    )


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
    manager = LockManager(now_fn=_fixed_now)
    remote_holder = HolderRef(
        origin="remote", ref="session-abc", host="loki"
    )

    # acquire
    assert manager.acquire(UID, KIND_SERIAL, remote_holder) is True
    assert manager.status(UID) == LockStatus(
        kind=KIND_SERIAL, holder=remote_holder, label=None, since=FIXED_NOW
    )
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
# lock-display callback (ticket 005) -- fires on every acquire/release,
# any kind, carrying (uid, kind, display, label, since) (sprint 008,
# ticket 002 widened the tuple from (uid, kind, display) to carry the new
# per-acquisition label/since fields alongside it) -- never the raw
# HolderRef.
# ---------------------------------------------------------------------------


def test_display_callback_fires_on_acquire_with_local_holder_display():
    calls = []
    manager = LockManager(
        lock_display_callback=lambda *args: calls.append(args), now_fn=_fixed_now
    )

    manager.acquire(UID, KIND_SERIAL, HOLDER)

    assert calls == [(UID, KIND_SERIAL, f"pid {PID}", None, FIXED_NOW)]


def test_display_callback_fires_on_acquire_with_remote_holder_display():
    calls = []
    manager = LockManager(
        lock_display_callback=lambda *args: calls.append(args), now_fn=_fixed_now
    )
    remote_holder = HolderRef(origin="remote", ref="session-abc", host="loki")

    manager.acquire(UID, KIND_FLASH, remote_holder)

    assert calls == [(UID, KIND_FLASH, "session session-abc on loki", None, FIXED_NOW)]


def test_display_callback_carries_the_supplied_label():
    """Sprint 008, ticket 002: an optional ``label`` passed to
    ``acquire`` rides through to the display callback's fourth
    argument, alongside the unchanged ``display`` text (never folded
    into it -- that's ``registry.render``/``registry.peering``'s own
    job, not this module's)."""
    calls = []
    manager = LockManager(
        lock_display_callback=lambda *args: calls.append(args), now_fn=_fixed_now
    )

    manager.acquire(UID, KIND_SERIAL, HOLDER, label="alice-laptop")

    assert calls == [(UID, KIND_SERIAL, f"pid {PID}", "alice-laptop", FIXED_NOW)]


def test_display_callback_never_receives_the_raw_holder_ref():
    calls = []
    manager = LockManager(lock_display_callback=lambda *args: calls.append(args))

    manager.acquire(UID, KIND_SERIAL, HOLDER)

    _uid, _kind, display, _label, _since = calls[0]
    assert isinstance(display, str)
    assert not isinstance(display, HolderRef)


def test_display_callback_fires_on_release_with_none_none():
    calls = []
    manager = LockManager(lock_display_callback=lambda *args: calls.append(args))
    manager.acquire(UID, KIND_SERIAL, HOLDER)
    calls.clear()

    manager.release(UID, HOLDER)

    assert calls == [(UID, None, None, None, None)]


def test_display_callback_fires_for_every_kind_not_only_flash():
    """Unlike ``flash_release_callback``, this hook is kind-agnostic --
    it must fire for a serial-kind release too."""
    calls = []
    manager = LockManager(lock_display_callback=lambda *args: calls.append(args))
    manager.acquire(UID, KIND_SERIAL, HOLDER)
    calls.clear()

    manager.release(UID, HOLDER)

    assert calls == [(UID, None, None, None, None)]


def test_display_callback_not_fired_on_failed_acquire():
    calls = []
    manager = LockManager(lock_display_callback=lambda *args: calls.append(args))
    manager.acquire(UID, KIND_SERIAL, HOLDER)
    calls.clear()

    with pytest.raises(LockHeldError):
        manager.acquire(UID, KIND_SERIAL, HOLDER2)

    assert calls == []


def test_display_callback_not_fired_on_noop_release():
    calls = []
    manager = LockManager(lock_display_callback=lambda *args: calls.append(args))
    manager.acquire(UID, KIND_SERIAL, HOLDER)
    calls.clear()

    manager.release(UID, HOLDER2)  # wrong holder -- no-op

    assert calls == []


def test_display_callback_fires_on_sweep_release():
    calls = []
    manager = LockManager(lock_display_callback=lambda *args: calls.append(args))
    manager.acquire(UID, KIND_SERIAL, HOLDER)
    calls.clear()

    manager.sweep(lambda holder: False)  # everyone dead

    assert calls == [(UID, None, None, None, None)]


def test_both_callbacks_fire_independently_for_flash_release():
    flash_calls = []
    display_calls = []
    manager = LockManager(
        flash_release_callback=flash_calls.append,
        lock_display_callback=lambda *args: display_calls.append(args),
    )
    manager.acquire(UID, KIND_FLASH, HOLDER)
    display_calls.clear()  # only the release call is under test here

    manager.release(UID, HOLDER)

    assert flash_calls == [UID]
    assert display_calls == [(UID, None, None, None, None)]


def test_no_display_callback_registered_is_a_silent_no_op():
    """Every test above this section (and every pre-ticket-005 test in
    this file) constructs a manager with no ``lock_display_callback`` at
    all -- this test just makes the "unaffected by default" contract
    explicit rather than implicit."""
    manager = LockManager()

    manager.acquire(UID, KIND_SERIAL, HOLDER)
    manager.release(UID, HOLDER)  # would raise if release assumed a callback exists

    assert manager.status(UID) is None


# ---------------------------------------------------------------------------
# label/since (sprint 008, ticket 002) -- per-acquisition, not per-holder
# ---------------------------------------------------------------------------


def test_acquire_without_label_leaves_label_none():
    manager = LockManager(now_fn=_fixed_now)

    manager.acquire(UID, KIND_SERIAL, HOLDER)

    status = manager.status(UID)
    assert status.label is None
    assert status.since == FIXED_NOW


def test_acquire_with_label_sets_label_and_since():
    manager = LockManager(now_fn=_fixed_now)

    manager.acquire(UID, KIND_SERIAL, HOLDER, label="alice-laptop")

    status = manager.status(UID)
    assert status.label == "alice-laptop"
    assert status.since == FIXED_NOW


def test_since_is_stamped_fresh_per_acquisition_not_cached_on_holder():
    """Proves ``since`` lives on ``LockStatus`` (per-acquisition), not on
    ``HolderRef`` (per-connection, reused across every lock that
    connection acquires) -- sprint.md's Design Rationale Decision 1's own
    reason for the placement. The *same* ``HolderRef`` acquiring two
    different uids at two different times gets two independent ``since``
    values, not one shared value fixed at first use."""
    clock = iter([100.0, 200.0])
    manager = LockManager(now_fn=lambda: next(clock))

    manager.acquire(UID, KIND_SERIAL, HOLDER)
    manager.acquire(UID2, KIND_DEBUG, HOLDER)  # same holder, later acquisition

    assert manager.status(UID).since == 100.0
    assert manager.status(UID2).since == 200.0

    # And releasing + re-acquiring the same uid with the same holder
    # stamps a fresh since, not the original one.
    manager.release(UID, HOLDER)
    clock2 = iter([300.0])
    manager2 = LockManager(now_fn=lambda: next(clock2))
    manager2.acquire(UID, KIND_SERIAL, HOLDER)
    assert manager2.status(UID).since == 300.0


def test_label_never_participates_in_release_holder_matching():
    """AC: LockManager.release's holder-equality check is unaffected --
    label/since never participate in matching who may release a lock.
    Proven by acquiring with a label, then releasing with the identical
    HolderRef (no label concept on HolderRef at all) -- it still
    succeeds, since release matches on HolderRef, never on LockStatus."""
    manager = LockManager(now_fn=_fixed_now)
    manager.acquire(UID, KIND_SERIAL, HOLDER, label="alice-laptop")

    assert manager.release(UID, HOLDER) is True
    assert manager.status(UID) is None


# ---------------------------------------------------------------------------
# format_lock_suffix (sprint 008, ticket 002) -- the shared display
# formatter registry.render/registry.peering both use.
# ---------------------------------------------------------------------------


def test_format_lock_suffix_empty_when_since_is_none():
    assert format_lock_suffix("alice-laptop", None, now=FIXED_NOW) == ""
    assert format_lock_suffix(None, None, now=FIXED_NOW) == ""


def test_format_lock_suffix_with_label_and_since():
    assert format_lock_suffix("alice-laptop", FIXED_NOW - 725, now=FIXED_NOW) == (
        " (alice-laptop, 12m)"
    )


def test_format_lock_suffix_with_since_only():
    assert format_lock_suffix(None, FIXED_NOW - 725, now=FIXED_NOW) == " (12m)"


def test_format_lock_suffix_seconds_and_hours():
    assert format_lock_suffix(None, FIXED_NOW - 30, now=FIXED_NOW) == " (30s)"
    assert format_lock_suffix(None, FIXED_NOW - 7200, now=FIXED_NOW) == " (2h)"


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


@pytest.mark.skipif(
    sys.platform == "win32",
    reason=(
        "needs two POSIX-only facilities together: the 'sleep' binary "
        "run as a subprocess (no such command on Windows by default) "
        "and os.kill(pid, 0)-as-a-liveness-probe semantics (Python's "
        "os.kill on Windows does not support signal 0 as a liveness "
        "check -- see registry.api_windows.default_is_pid_alive_windows's "
        "own docstring); that Windows analogue is exercised separately "
        "in tests/registry/api_windows/"
    ),
)
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
