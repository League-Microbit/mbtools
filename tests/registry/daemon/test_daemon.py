"""Tests for mbtools.registry.daemon -- the attach/detach -> probe -> store
pipeline and the re-probe rules (ticket 006).

Per sprint.md's Test Strategy, every test here runs against
``mbtools.testing.fakes.FakeUSBSource``/``FakeSerial`` (ticket 001) plus
real ``Store``/``LockManager`` instances (tickets 004/005) -- no real USB,
no real serial port, no real subprocess.
"""

from __future__ import annotations

import itertools
from collections import deque

import pytest

from mbtools.common import DAPLINK_VID_PID, PortInfo
from mbtools.registry import claims
from mbtools.registry.daemon import Daemon
from mbtools.registry.locks import KIND_FLASH, KIND_SERIAL, HolderRef
from mbtools.registry.store import (
    STATE_ATTACHED_NO_ANNOUNCE,
    STATE_ATTACHED_UNPROBED,
    STATE_CONNECTED,
    STATE_DISCONNECTED,
    Store,
    format_vid_pid,
)
from mbtools.serial.connect import BREAK_DURATION
from mbtools.testing.fakes import FakeSerial, FakeUSBSource

VID, PID_ = DAPLINK_VID_PID
UID = "9900" + "0000" + "11112222" + "3333444455556666" + "77778888" + "6e052820"
UID2 = "9900" + "0000" + "11112222" + "aaaabbbbccccdddd" + "77778888" + "6e052820"
ANNOUNCEMENT = "device NEZHA2 robot vevov 1198504156"
RELAY_ANNOUNCEMENT = "DEVICE:RADIOBRIDGE:relay:getez:1779042496"


def _port_info(uid: str = UID, port: str = "/dev/ttyACM0") -> PortInfo:
    return PortInfo(uid=uid, port=port, vid=VID, pid=PID_)


def _local_holder(pid: int) -> HolderRef:
    """Mirrors api.py's own ``_local_holder`` construction (ticket 002)
    for tests that acquire/release directly against ``daemon.locks``,
    bypassing the wire protocol."""
    return HolderRef(origin="local", ref=str(pid), pid=pid)


def _store_clock(start: float = 1000.0, step: float = 1.0):
    """Deterministic, monotonically-increasing now_fn for Store -- mirrors
    test_store.py's own helper so store timestamps never depend on real
    wall-clock time."""
    counter = itertools.count()
    return lambda: start + step * next(counter)


class _Clock:
    """A manually-advanced clock for Daemon's own now_fn -- lets a test
    jump straight past a flash-reprobe deadline without sleeping or
    running dozens of cycles."""

    def __init__(self, start: float = 0.0) -> None:
        self.t = start

    def __call__(self) -> float:
        return self.t

    def advance(self, dt: float) -> None:
        self.t += dt


class _ProbeScript:
    """A serial_factory that hands out a fresh FakeSerial per probe() call,
    scripted with the next queued announcement (or silence once the queue
    is empty) -- and counts how many times a port was actually opened, so
    tests can assert "probed exactly N times" directly. Every FakeSerial
    handed out is also kept in :attr:`fakes` (ticket 001, sprint 005), so
    a test can inspect e.g. ``fakes[1].break_calls`` to check whether a
    particular probe call asserted BREAK."""

    def __init__(self, announcements: list[str | None] = ()) -> None:
        self._queue: deque[str | None] = deque(announcements)
        self.calls = 0
        self.fakes: list[FakeSerial] = []

    def __call__(self, **kwargs: object) -> FakeSerial:
        self.calls += 1
        announcement = self._queue.popleft() if self._queue else None
        ser = FakeSerial(announcement=announcement, **kwargs)
        self.fakes.append(ser)
        return ser


@pytest.fixture
def store(tmp_path):
    return Store(tmp_path / "devices.db", now_fn=_store_clock())


def _make_daemon(usbwatch, store, script, **kwargs):
    return Daemon(
        usbwatch=usbwatch,
        store=store,
        serial_factory=script,
        probe_timeout_s=0.05,
        settle_s=0,
        **kwargs,
    )


# ---------------------------------------------------------------------------
# full attach -> probe -> connected cycle
# ---------------------------------------------------------------------------


def test_attach_probe_connected_cycle(store):
    usbwatch = FakeUSBSource([[_port_info()]])
    script = _ProbeScript([ANNOUNCEMENT])
    daemon = _make_daemon(usbwatch, store, script)

    daemon.run_once()

    assert script.calls == 1
    record = store.get(UID)
    assert record is not None
    assert record.state == STATE_CONNECTED
    assert record.device_name == "vevov"
    assert record.role == "NEZHA2"


# ---------------------------------------------------------------------------
# no-op re-scan -- never reprobed while attached and unflashed
# ---------------------------------------------------------------------------


def test_no_reprobe_while_attached_across_multiple_cycles(store):
    usbwatch = FakeUSBSource([[_port_info()]])  # repeats once exhausted
    script = _ProbeScript([ANNOUNCEMENT])
    daemon = _make_daemon(usbwatch, store, script)

    daemon.run_once()
    daemon.run_once()
    daemon.run_once()

    assert script.calls == 1
    assert store.get(UID).state == STATE_CONNECTED


# ---------------------------------------------------------------------------
# detach releases lock and marks disconnected
# ---------------------------------------------------------------------------


def test_detach_releases_lock_and_marks_disconnected(store):
    usbwatch = FakeUSBSource([[_port_info()], []])
    script = _ProbeScript([ANNOUNCEMENT])
    daemon = _make_daemon(usbwatch, store, script)

    daemon.run_once()  # attach + probe
    assert daemon.locks.acquire(UID, KIND_SERIAL, _local_holder(555)) is True

    daemon.run_once()  # detach

    assert daemon.locks.status(UID) is None
    assert store.get(UID).state == STATE_DISCONNECTED


def test_detach_of_unlocked_device_just_marks_disconnected(store):
    usbwatch = FakeUSBSource([[_port_info()], []])
    script = _ProbeScript([ANNOUNCEMENT])
    daemon = _make_daemon(usbwatch, store, script)

    daemon.run_once()
    daemon.run_once()

    assert store.get(UID).state == STATE_DISCONNECTED


# ---------------------------------------------------------------------------
# reattach re-probes
# ---------------------------------------------------------------------------


def test_reattach_after_detach_is_reprobed(store):
    usbwatch = FakeUSBSource([[_port_info()], [], [_port_info()]])
    script = _ProbeScript([ANNOUNCEMENT, ANNOUNCEMENT])
    daemon = _make_daemon(usbwatch, store, script)

    daemon.run_once()  # attach + probe #1
    daemon.run_once()  # detach
    daemon.run_once()  # reattach -> probe #2

    assert script.calls == 2
    assert store.get(UID).state == STATE_CONNECTED


# ---------------------------------------------------------------------------
# flash-triggered re-probe, exactly once
# ---------------------------------------------------------------------------


def test_flash_lock_release_triggers_exactly_one_reprobe_on_reenumeration(store):
    usbwatch = FakeUSBSource([[_port_info()], [], [_port_info()]])
    script = _ProbeScript([ANNOUNCEMENT, ANNOUNCEMENT])
    daemon = _make_daemon(usbwatch, store, script)

    daemon.run_once()  # attach + probe #1
    assert script.calls == 1

    assert daemon.locks.acquire(UID, KIND_FLASH, _local_holder(777)) is True
    assert daemon.locks.release(UID, _local_holder(777)) is True  # fires flash-release hook

    daemon.run_once()  # sees the drop (flash-induced reboot)
    assert store.get(UID).state == STATE_DISCONNECTED
    assert script.calls == 1  # not probed while absent

    daemon.run_once()  # sees it re-enumerate -> exactly one re-probe

    assert script.calls == 2
    assert store.get(UID).state == STATE_CONNECTED


def test_flash_reprobe_without_intervening_detach_still_reprobes(store):
    """A flash that never drops the DAPLink interface off the bus at all
    (no detach/reattach cycle for upsert_attached's own reattach-reset
    rule to catch) must still be re-probed -- this is exactly why
    flash-pending tracking exists independently of the store's own
    reattach-based needs_probe() reset."""
    usbwatch = FakeUSBSource([[_port_info()]])  # uid never leaves the scan
    script = _ProbeScript([ANNOUNCEMENT, ANNOUNCEMENT])
    daemon = _make_daemon(usbwatch, store, script)

    daemon.run_once()  # attach + probe #1
    assert script.calls == 1
    assert store.needs_probe(UID) is False

    daemon.locks.acquire(UID, KIND_FLASH, _local_holder(777))
    daemon.locks.release(UID, _local_holder(777))

    daemon.run_once()  # still attached the whole time -> re-probed anyway

    assert script.calls == 2


# ---------------------------------------------------------------------------
# flash-triggered re-probe timeout
# ---------------------------------------------------------------------------


def test_flash_reprobe_timeout_marks_no_announce_without_reopening_port(store):
    """Sprint 007, ticket 001: this give-up path still calls the plain
    ``Store.apply_probe_result(uid, None)`` (ticket 001's own scoping --
    see daemon.py's ``run_once`` docstring -- leaves routing this specific
    call site to ``Store.apply_known_blank`` for ticket 003, which owns
    the full flash-triggered-known-blank wiring), so it now lands on
    ``STATE_ATTACHED_NO_ANNOUNCE`` rather than the old
    ``STATE_CONNECTED_NO_FIRMWARE``."""
    usbwatch = FakeUSBSource([[_port_info()], []])  # never comes back
    script = _ProbeScript([ANNOUNCEMENT])
    clock = _Clock(start=0.0)
    daemon = _make_daemon(
        usbwatch, store, script, flash_reprobe_timeout_s=5.0, now_fn=clock
    )

    daemon.run_once()  # attach + probe #1
    assert script.calls == 1

    daemon.locks.acquire(UID, KIND_FLASH, _local_holder(777))
    daemon.locks.release(UID, _local_holder(777))  # flash_pending[UID] = 0 + 5.0

    daemon.run_once()  # detach seen; deadline not reached yet (t=0 < 5)
    assert store.get(UID).state == STATE_DISCONNECTED

    clock.advance(10.0)  # now well past the deadline
    daemon.run_once()  # still absent -> gives up

    assert store.get(UID).state == STATE_ATTACHED_NO_ANNOUNCE
    assert script.calls == 1  # never reopened a port for a device not there


def test_flash_reprobe_before_deadline_does_not_time_out(store):
    usbwatch = FakeUSBSource([[_port_info()], []])
    script = _ProbeScript([ANNOUNCEMENT])
    clock = _Clock(start=0.0)
    daemon = _make_daemon(
        usbwatch, store, script, flash_reprobe_timeout_s=5.0, now_fn=clock
    )

    daemon.run_once()
    daemon.locks.acquire(UID, KIND_FLASH, _local_holder(777))
    daemon.locks.release(UID, _local_holder(777))

    clock.advance(1.0)
    daemon.run_once()  # detach; well within the 5s window

    assert store.get(UID).state == STATE_DISCONNECTED  # not marked no-announce


# ---------------------------------------------------------------------------
# a locked device is never probed
# ---------------------------------------------------------------------------


def test_locked_device_is_never_probed_until_unlocked(store):
    usbwatch = FakeUSBSource([[_port_info()]])
    script = _ProbeScript([ANNOUNCEMENT])
    daemon = _make_daemon(usbwatch, store, script)

    assert daemon.locks.acquire(UID, KIND_SERIAL, _local_holder(999)) is True

    daemon.run_once()
    daemon.run_once()

    assert script.calls == 0
    assert store.get(UID).state == STATE_ATTACHED_UNPROBED

    assert daemon.locks.release(UID, _local_holder(999)) is True
    daemon.run_once()

    assert script.calls == 1
    assert store.get(UID).state == STATE_CONNECTED


# ---------------------------------------------------------------------------
# run() loop
# ---------------------------------------------------------------------------


def test_run_loop_performs_one_cycle_per_interval_until_stopped(store):
    usbwatch = FakeUSBSource([[_port_info()]])
    script = _ProbeScript([ANNOUNCEMENT])
    daemon = _make_daemon(usbwatch, store, script)

    def stop() -> bool:
        return usbwatch.call_count >= 3

    daemon.run(interval_s=0, stop=stop)

    assert usbwatch.call_count == 3
    assert script.calls == 1  # only probed once, the rest were no-ops


# ---------------------------------------------------------------------------
# two devices -- one locked, one free, in the same cycle
# ---------------------------------------------------------------------------


def test_locked_device_does_not_block_probing_other_devices(store):
    usbwatch = FakeUSBSource([[_port_info(UID, "/dev/ttyACM0"), _port_info(UID2, "/dev/ttyACM1")]])
    script = _ProbeScript([ANNOUNCEMENT])
    daemon = _make_daemon(usbwatch, store, script)

    daemon.locks.acquire(UID, KIND_SERIAL, _local_holder(999))

    daemon.run_once()

    assert script.calls == 1
    assert store.get(UID).state == STATE_ATTACHED_UNPROBED
    assert store.get(UID2).state == STATE_CONNECTED


# ---------------------------------------------------------------------------
# event_callback hook (ticket 005) -- fires on attach/detach/identity,
# never called when not registered (every test above leaves it unset).
# ---------------------------------------------------------------------------


def test_event_callback_fires_attach_then_identity_in_one_cycle(store):
    usbwatch = FakeUSBSource([[_port_info()]])
    script = _ProbeScript([ANNOUNCEMENT])
    events: list[tuple[str, str, str]] = []

    def on_event(event_type, record):
        events.append((event_type, record.uid, record.state))

    daemon = _make_daemon(usbwatch, store, script, event_callback=on_event)

    daemon.run_once()  # attach + probe, in the same cycle

    assert events == [
        ("attach", UID, STATE_ATTACHED_UNPROBED),
        ("identity", UID, STATE_CONNECTED),
    ]


def test_event_callback_fires_detach(store):
    usbwatch = FakeUSBSource([[_port_info()], []])
    script = _ProbeScript([ANNOUNCEMENT])
    events: list[tuple[str, str]] = []
    daemon = _make_daemon(
        usbwatch, store, script, event_callback=lambda t, r: events.append((t, r.uid))
    )

    daemon.run_once()  # attach + probe
    events.clear()
    daemon.run_once()  # detach

    assert events == [("detach", UID)]


def test_event_callback_fires_identity_on_flash_reprobe_timeout_giveup(store):
    """The flash-reprobe-timeout give-up path also calls
    ``store.apply_probe_result`` (with a ``None`` result) -- a peer needs
    to learn about that ``attached_no_announce`` transition exactly like
    any other completed probe. (Sprint 007, ticket 001: this give-up path
    is not yet routed to ``Store.apply_known_blank`` -- see ticket 001's
    own scoping note in daemon.py's ``run_once`` docstring -- so it still
    lands on the didn't-announce state, not known-blank.)"""
    usbwatch = FakeUSBSource([[_port_info()], []])  # never comes back
    script = _ProbeScript([ANNOUNCEMENT])
    clock = _Clock(start=0.0)
    events: list[tuple[str, str, str]] = []
    daemon = _make_daemon(
        usbwatch,
        store,
        script,
        flash_reprobe_timeout_s=5.0,
        now_fn=clock,
        event_callback=lambda t, r: events.append((t, r.uid, r.state)),
    )

    daemon.run_once()  # attach + probe #1
    daemon.locks.acquire(UID, KIND_FLASH, _local_holder(777))
    daemon.locks.release(UID, _local_holder(777))
    daemon.run_once()  # detach seen
    events.clear()

    clock.advance(10.0)
    daemon.run_once()  # gives up -> identity event with attached_no_announce

    assert events == [("identity", UID, STATE_ATTACHED_NO_ANNOUNCE)]


def test_no_event_callback_registered_is_a_silent_no_op(store):
    """Every test above this section constructs a daemon with no
    ``event_callback`` at all -- this test just makes the "unaffected by
    default" contract explicit rather than implicit."""
    usbwatch = FakeUSBSource([[_port_info()], []])
    script = _ProbeScript([ANNOUNCEMENT])
    daemon = _make_daemon(usbwatch, store, script)  # no event_callback

    daemon.run_once()
    daemon.run_once()  # detach -- would raise if daemon assumed a callback exists

    assert store.get(UID).state == STATE_DISCONNECTED


# ---------------------------------------------------------------------------
# `previously_attached` is scoped to locally-owned rows (sprint 005
# ticket 011) -- a uid this store merely mirrors from a peer must not
# block this host from claiming it when it's physically attached here,
# and must never be spuriously "detached" by a scan that was never
# looking at it in the first place. Found via this ticket's own hardware
# verification on torture: a board physically attached the whole time
# stayed stuck as host=hodr because its store row was kept alive
# (non-disconnected) by hodr's own ongoing peering events, so it never
# satisfied the old, host-agnostic "not in previously_attached" check.
# ---------------------------------------------------------------------------


def test_physically_attached_uid_reclaims_local_ownership_from_stale_remote_row(store):
    # Simulate what registry.peering would have written: this store
    # mirrors uid as owned by "hodr", still "connected" (not disconnected)
    # -- exactly the torture/f92f913d shape found on hardware.
    store.upsert_remote_attached(UID, "hodr", "/dev/ttyACM1", format_vid_pid(VID, PID_))
    store.apply_remote_probe(UID, None)  # anything non-disconnected; state irrelevant here
    assert store.get(UID).state != STATE_DISCONNECTED
    assert store.get(UID).host == "hodr"

    # This host's own usbwatch now (still) sees the uid physically present.
    usbwatch = FakeUSBSource([[_port_info(port="/dev/ttyACM1")]])
    script = _ProbeScript([ANNOUNCEMENT])
    daemon = _make_daemon(usbwatch, store, script)

    daemon.run_once()

    record = store.get(UID)
    assert record.host is None
    assert record.port == "/dev/ttyACM1"


def test_remote_owned_row_not_physically_present_is_never_marked_disconnected(store):
    # A uid this store only knows about via peering (never physically
    # attached to this host) must not be diffed into "detached" just
    # because it's absent from this host's own usbwatch scan.
    store.upsert_remote_attached(UID2, "hodr", "/dev/ttyACM0", format_vid_pid(VID, PID_))
    assert store.get(UID2).state != STATE_DISCONNECTED

    usbwatch = FakeUSBSource([[]])  # nothing physically attached here
    script = _ProbeScript([])
    events: list[tuple[str, str]] = []
    daemon = _make_daemon(
        usbwatch, store, script, event_callback=lambda t, r: events.append((t, r.uid))
    )

    daemon.run_once()

    # Neither marked disconnected locally nor announced as detached --
    # this host never owned it and never touched it.
    record = store.get(UID2)
    assert record.host == "hodr"
    assert record.state != STATE_DISCONNECTED
    assert events == []


# ---------------------------------------------------------------------------
# reset_first: BREAK before HELLO on a relay's re-probe (ticket 001,
# sprint 005) -- the daemon.py half of the fix for a relay left in the
# data plane being misidentified by a re-probe
# (docs/acceptance/004-hardware.md, ticket 011's togov/gitev finding).
# ---------------------------------------------------------------------------


def test_first_ever_probe_resets_the_board_before_hello(store):
    """A brand-new (or wiped) registry has no stored role, and a relay left
    in its data plane would forward HELLO over radio and hand back a
    robot's reply. So even the very first probe resets the board first."""
    usbwatch = FakeUSBSource([[_port_info()]])
    script = _ProbeScript([RELAY_ANNOUNCEMENT])
    daemon = _make_daemon(usbwatch, store, script)

    daemon.run_once()  # attach + first-ever probe

    assert script.fakes[0].break_calls == [BREAK_DURATION]
    assert store.get(UID).role == "RADIOBRIDGE"


@pytest.mark.parametrize("announcement", [RELAY_ANNOUNCEMENT, ANNOUNCEMENT])
def test_reattach_reprobe_resets_relays_and_robots_alike(store, announcement):
    usbwatch = FakeUSBSource([[_port_info()], [], [_port_info()]])
    script = _ProbeScript([announcement, announcement])
    daemon = _make_daemon(usbwatch, store, script)

    daemon.run_once()  # attach + probe #1
    daemon.run_once()  # detach
    daemon.run_once()  # reattach -> probe #2

    assert script.calls == 2
    assert script.fakes[0].break_calls == [BREAK_DURATION]
    assert script.fakes[1].break_calls == [BREAK_DURATION]


@pytest.mark.parametrize("announcement", [RELAY_ANNOUNCEMENT, ANNOUNCEMENT])
def test_flash_pending_reprobe_resets_the_board(store, announcement):
    usbwatch = FakeUSBSource([[_port_info()]])  # uid never leaves the scan
    script = _ProbeScript([announcement, announcement])
    daemon = _make_daemon(usbwatch, store, script)

    daemon.run_once()  # attach + probe #1
    daemon.locks.acquire(UID, KIND_FLASH, _local_holder(777))
    daemon.locks.release(UID, _local_holder(777))  # flash_pending[UID] set
    daemon.run_once()  # still attached -> flash-pending re-probe #2

    assert script.calls == 2
    assert script.fakes[1].break_calls == [BREAK_DURATION]


# ---------------------------------------------------------------------------
# cross-instance claim gating (sprint 007, ticket 002) -- every test above
# this section relies on Daemon's own bare claim_fn default (a filesystem-
# free no-op that always grants the claim, per daemon.py's "Cross-instance
# claim" docstring note), so none of them are affected by this section at
# all. These tests inject a fake claim_fn instead, matching the ticket's
# own "tests can fake claim outcomes... when more convenient than tmp_path"
# testing note -- no real filesystem touched here. See
# test_daemon_claims_integration.py for the real-claims.try_claim,
# tmp_path-backed, two-Daemon/Store-pair version of this same guarantee.
# ---------------------------------------------------------------------------


class _FakeClaimHandle:
    def __init__(self, on_release=None):
        self._on_release = on_release
        self.released = False

    def release(self) -> None:
        self.released = True
        if self._on_release is not None:
            self._on_release()


def test_denied_claim_never_upserts_the_uid(store):
    usbwatch = FakeUSBSource([[_port_info()]])  # repeats once exhausted
    script = _ProbeScript([ANNOUNCEMENT])
    calls: list[str] = []

    def deny(uid: str):
        calls.append(uid)
        return None

    daemon = _make_daemon(usbwatch, store, script, claim_fn=deny)

    daemon.run_once()

    assert store.get(UID) is None
    assert script.calls == 0
    assert calls == [UID]


def test_denied_claim_is_retried_on_a_later_cycle_with_no_extra_bookkeeping(store):
    usbwatch = FakeUSBSource([[_port_info()]])  # repeats
    script = _ProbeScript([ANNOUNCEMENT])
    calls: list[str] = []

    def deny(uid: str):
        calls.append(uid)
        return None

    daemon = _make_daemon(usbwatch, store, script, claim_fn=deny)

    daemon.run_once()
    daemon.run_once()
    daemon.run_once()

    assert store.get(UID) is None
    assert calls == [UID, UID, UID]  # tried again every cycle, unconditionally


def test_claim_recovers_once_it_becomes_available_on_a_later_cycle(store):
    usbwatch = FakeUSBSource([[_port_info()]])  # repeats
    script = _ProbeScript([ANNOUNCEMENT])
    outcomes = iter([None, None, _FakeClaimHandle()])

    daemon = _make_daemon(
        usbwatch, store, script, claim_fn=lambda uid: next(outcomes)
    )

    daemon.run_once()  # denied
    daemon.run_once()  # denied
    assert store.get(UID) is None

    daemon.run_once()  # granted this cycle -- upserted and probed in the same cycle

    assert store.get(UID) is not None
    assert script.calls == 1
    assert store.get(UID).state == STATE_CONNECTED


def test_granted_claim_is_released_on_detach(store):
    usbwatch = FakeUSBSource([[_port_info()], []])
    script = _ProbeScript([ANNOUNCEMENT])
    handle = _FakeClaimHandle()
    daemon = _make_daemon(usbwatch, store, script, claim_fn=lambda uid: handle)

    daemon.run_once()  # attach -- claim granted
    assert handle.released is False

    daemon.run_once()  # detach

    assert handle.released is True
    assert store.get(UID).state == STATE_DISCONNECTED


def test_granted_claim_is_never_released_while_still_attached(store):
    usbwatch = FakeUSBSource([[_port_info()]])  # repeats -- never detaches
    script = _ProbeScript([ANNOUNCEMENT])
    handle = _FakeClaimHandle()
    daemon = _make_daemon(usbwatch, store, script, claim_fn=lambda uid: handle)

    daemon.run_once()
    daemon.run_once()
    daemon.run_once()

    assert handle.released is False


def test_claim_fn_is_called_at_most_once_per_attach_not_once_per_cycle(store):
    usbwatch = FakeUSBSource([[_port_info()]])  # repeats -- stays attached
    script = _ProbeScript([ANNOUNCEMENT])
    calls: list[str] = []

    def grant(uid: str):
        calls.append(uid)
        return _FakeClaimHandle()

    daemon = _make_daemon(usbwatch, store, script, claim_fn=grant)

    daemon.run_once()
    daemon.run_once()
    daemon.run_once()

    assert calls == [UID]  # only re-attempted on a genuinely new attach


def test_default_claim_fn_grants_every_uid_with_no_filesystem_access(store):
    """Daemon's own bare default (no claim_fn given at all) -- the same
    default every pre-ticket-002 test above this section already relies
    on implicitly. Made explicit here as its own test."""
    usbwatch = FakeUSBSource([[_port_info()]])
    script = _ProbeScript([ANNOUNCEMENT])
    daemon = _make_daemon(usbwatch, store, script)  # no claim_fn

    daemon.run_once()

    assert store.get(UID) is not None
    assert store.get(UID).state == STATE_CONNECTED


# ---------------------------------------------------------------------------
# real cross-instance contention against a shared, real (tmp_path-backed)
# claims directory -- proves Daemon actually wires up
# mbtools.registry.claims.try_claim correctly when a real claim_fn is
# injected, not just a fake. Two independent Daemon/Store pairs, one
# shared claims directory, one shared USB scan result -- exactly the
# ticket's own "an unclaimed uid is never upsert_attached'd" acceptance
# criterion, end to end.
# ---------------------------------------------------------------------------


def test_two_daemons_sharing_a_real_claims_dir_only_one_claims_the_uid(tmp_path):
    claims_dir = tmp_path / "claims"
    store_a = Store(tmp_path / "a" / "devices.db", now_fn=_store_clock())
    store_b = Store(tmp_path / "b" / "devices.db", now_fn=_store_clock())

    usbwatch_a = FakeUSBSource([[_port_info()]])  # repeats
    usbwatch_b = FakeUSBSource([[_port_info()]])  # repeats -- same uid

    daemon_a = Daemon(
        usbwatch=usbwatch_a,
        store=store_a,
        serial_factory=_ProbeScript([ANNOUNCEMENT]),
        probe_timeout_s=0.05,
        settle_s=0,
        claim_fn=lambda uid: claims.try_claim(uid, claims_dir=claims_dir),
    )
    daemon_b = Daemon(
        usbwatch=usbwatch_b,
        store=store_b,
        serial_factory=_ProbeScript([ANNOUNCEMENT]),
        probe_timeout_s=0.05,
        settle_s=0,
        claim_fn=lambda uid: claims.try_claim(uid, claims_dir=claims_dir),
    )

    daemon_a.run_once()
    daemon_b.run_once()

    a_has_it = store_a.get(UID) is not None
    b_has_it = store_b.get(UID) is not None
    assert a_has_it != b_has_it  # exactly one of the two claimed it

    # The loser keeps retrying every cycle and still never gets it while
    # the winner holds the claim.
    daemon_a.run_once()
    daemon_b.run_once()
    assert (store_a.get(UID) is not None) == a_has_it
    assert (store_b.get(UID) is not None) == b_has_it
