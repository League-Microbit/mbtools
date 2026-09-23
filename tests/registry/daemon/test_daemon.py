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
)
from mbtools.testing.fakes import FakeSerial, FakeUSBSource

VID, PID_ = DAPLINK_VID_PID
UID = "9900" + "0000" + "11112222" + "3333444455556666" + "77778888" + "6e052820"
UID2 = "9900" + "0000" + "11112222" + "aaaabbbbccccdddd" + "77778888" + "6e052820"
ANNOUNCEMENT = "device NEZHA2 robot vevov 1198504156"


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
    tests can assert "probed exactly N times" directly."""

    def __init__(self, announcements: list[str | None] = ()) -> None:
        self._queue: deque[str | None] = deque(announcements)
        self.calls = 0

    def __call__(self, **kwargs: object) -> FakeSerial:
        self.calls += 1
        announcement = self._queue.popleft() if self._queue else None
        return FakeSerial(announcement=announcement, **kwargs)


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
