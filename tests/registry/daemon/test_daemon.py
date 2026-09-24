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
from mbtools.registry.daemon import Daemon
from mbtools.registry.locks import KIND_FLASH, KIND_SERIAL, HolderRef
from mbtools.registry.store import (
    STATE_ATTACHED_UNPROBED,
    STATE_CONNECTED,
    STATE_CONNECTED_NO_FIRMWARE,
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


def test_flash_reprobe_timeout_marks_no_firmware_without_reopening_port(store):
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

    assert store.get(UID).state == STATE_CONNECTED_NO_FIRMWARE
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

    assert store.get(UID).state == STATE_DISCONNECTED  # not marked no-firmware


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
    to learn about that ``connected_no_firmware`` transition exactly like
    any other completed probe."""
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
    daemon.run_once()  # gives up -> identity event with connected_no_firmware

    assert events == [("identity", UID, STATE_CONNECTED_NO_FIRMWARE)]


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


def test_relay_reattach_reprobe_sends_break_before_hello(store):
    """A uid whose *pre-probe stored role* is a relay gets reset_first=True
    on its reattach-triggered re-probe -- the ordinary "device dropped
    off and came back" path, as opposed to the flash-pending path covered
    separately below."""
    usbwatch = FakeUSBSource([[_port_info()], [], [_port_info()]])
    script = _ProbeScript([RELAY_ANNOUNCEMENT, RELAY_ANNOUNCEMENT])
    daemon = _make_daemon(usbwatch, store, script)

    daemon.run_once()  # attach + probe #1 -- first-ever probe, no stored
    # role yet, so no BREAK on this one.
    assert script.fakes[0].break_calls == []
    assert store.get(UID).role == "RADIOBRIDGE"

    daemon.run_once()  # detach
    daemon.run_once()  # reattach -> probe #2; pre-probe stored role is
    # now RADIOBRIDGE, from probe #1.

    assert script.calls == 2
    assert script.fakes[1].break_calls == [BREAK_DURATION]


def test_relay_flash_pending_reprobe_sends_break_before_hello(store):
    """The flash-triggered re-probe path (device never left the bus, so
    there's no detach/reattach for needs_probe() to key off) must pass
    reset_first the same way as an ordinary reattach: from the uid's
    pre-probe stored role."""
    usbwatch = FakeUSBSource([[_port_info()]])  # uid never leaves the scan
    script = _ProbeScript([RELAY_ANNOUNCEMENT, RELAY_ANNOUNCEMENT])
    daemon = _make_daemon(usbwatch, store, script)

    daemon.run_once()  # attach + probe #1 -- first-ever probe, no BREAK
    assert script.fakes[0].break_calls == []
    assert store.get(UID).role == "RADIOBRIDGE"
    assert store.needs_probe(UID) is False

    daemon.locks.acquire(UID, KIND_FLASH, _local_holder(777))
    daemon.locks.release(UID, _local_holder(777))  # flash_pending[UID] set

    daemon.run_once()  # still attached -> flash-pending re-probe #2

    assert script.calls == 2
    assert script.fakes[1].break_calls == [BREAK_DURATION]


def test_non_relay_reattach_reprobe_never_sends_break(store):
    """An ordinary (non-relay) device's reprobe is unaffected: no BREAK
    sent when the stored role does not contain RELAY/BRIDGE -- covers
    both the very first probe (no prior stored role at all) and a later
    reattach-triggered re-probe (stored role is NEZHA2, not a relay)."""
    usbwatch = FakeUSBSource([[_port_info()], [], [_port_info()]])
    script = _ProbeScript([ANNOUNCEMENT, ANNOUNCEMENT])
    daemon = _make_daemon(usbwatch, store, script)

    daemon.run_once()  # attach + probe #1 -- no prior stored role at all
    assert script.fakes[0].break_calls == []
    assert store.get(UID).role == "NEZHA2"

    daemon.run_once()  # detach
    daemon.run_once()  # reattach -> probe #2; stored role is NEZHA2

    assert script.calls == 2
    assert script.fakes[1].break_calls == []


def test_non_relay_flash_pending_reprobe_never_sends_break(store):
    """Same non-relay "never sends BREAK" guarantee, on the flash-pending
    re-probe path rather than the reattach path."""
    usbwatch = FakeUSBSource([[_port_info()]])
    script = _ProbeScript([ANNOUNCEMENT, ANNOUNCEMENT])
    daemon = _make_daemon(usbwatch, store, script)

    daemon.run_once()  # attach + probe #1
    assert script.fakes[0].break_calls == []

    daemon.locks.acquire(UID, KIND_FLASH, _local_holder(777))
    daemon.locks.release(UID, _local_holder(777))

    daemon.run_once()  # flash-pending re-probe #2

    assert script.calls == 2
    assert script.fakes[1].break_calls == []
