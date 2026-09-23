"""Tests for mbtools.registry.usbwatch — the PortWatcher interface and its
one implementation, PollingPortWatcher (ticket 003).

No test here touches real USB hardware: PollingPortWatcher.scan() is
exercised either via its comports_fn injection point or by monkeypatching
serial.tools.list_ports.comports, never against a real bus.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable

import pytest

from mbtools.common import DAPLINK_VID_PID, PortInfo
from mbtools.registry import usbwatch
from mbtools.registry.usbwatch import PollingPortWatcher, PortWatcher
from mbtools.testing.fakes import FakeUSBSource

MATCHING = PortInfo(uid="abc123", port="/dev/ttyACM0", vid=0x0D28, pid=0x0204)
MATCHING2 = PortInfo(uid="def456", port="/dev/ttyACM1", vid=0x0D28, pid=0x0204)
NON_MATCHING = PortInfo(uid="deadbeef", port="/dev/ttyUSB0", vid=0x1234, pid=0x5678)


@dataclass
class _RawComportEntry:
    """Stands in for one ``serial.tools.list_ports.comports()`` result --
    a ``ListPortInfo``-shaped object exposing ``.vid``, ``.pid``,
    ``.serial_number``, ``.device`` (rather than PortInfo's ``uid``/``port``
    names), so tests can script exactly what PollingPortWatcher actually
    reads off a raw comports() entry."""

    device: str
    vid: int | None
    pid: int | None
    serial_number: str | None


def _raw(info: PortInfo) -> _RawComportEntry:
    """Build the raw comports()-shaped entry a given PortInfo would have
    come from -- used to script PollingPortWatcher's injected comports_fn
    from the same PortInfo fixtures FakeUSBSource scripts against."""
    return _RawComportEntry(
        device=info.port, vid=info.vid, pid=info.pid, serial_number=info.uid
    )


# ---------------------------------------------------------------------------
# PortWatcher interface
# ---------------------------------------------------------------------------


def test_polling_port_watcher_satisfies_port_watcher_protocol():
    assert isinstance(PollingPortWatcher(comports_fn=list), PortWatcher)


def test_fake_usb_source_satisfies_port_watcher_protocol():
    assert isinstance(FakeUSBSource(), PortWatcher)


# ---------------------------------------------------------------------------
# PollingPortWatcher.scan() -- comports_fn injection
# ---------------------------------------------------------------------------


class TestPollingPortWatcherInjected:
    def test_empty_comports_returns_empty_dict(self):
        watcher = PollingPortWatcher(comports_fn=lambda: [])
        result = watcher.scan()
        assert result == {}

    def test_one_matching_device(self):
        watcher = PollingPortWatcher(comports_fn=lambda: [_raw(MATCHING)])
        assert watcher.scan() == {"abc123": MATCHING}

    def test_one_non_matching_device(self):
        watcher = PollingPortWatcher(comports_fn=lambda: [_raw(NON_MATCHING)])
        assert watcher.scan() == {}

    def test_mixed_matching_and_non_matching(self):
        watcher = PollingPortWatcher(
            comports_fn=lambda: [_raw(MATCHING), _raw(NON_MATCHING), _raw(MATCHING2)]
        )
        result = watcher.scan()
        assert result == {"abc123": MATCHING, "def456": MATCHING2}

    def test_entry_with_no_serial_number_is_skipped_not_raised(self):
        odd = _RawComportEntry(
            device="/dev/ttyACM9", vid=0x0D28, pid=0x0204, serial_number=None
        )
        watcher = PollingPortWatcher(comports_fn=lambda: [odd])
        assert watcher.scan() == {}

    def test_rescans_live_each_call(self):
        """scan() is a live snapshot, not cached -- a watcher instance
        reused across calls reflects whatever comports_fn returns *now*."""
        snapshots = [[], [_raw(MATCHING)], []]
        calls = iter(snapshots)
        watcher = PollingPortWatcher(comports_fn=lambda: next(calls))
        assert watcher.scan() == {}
        assert watcher.scan() == {"abc123": MATCHING}
        assert watcher.scan() == {}


# ---------------------------------------------------------------------------
# PollingPortWatcher.scan() -- monkeypatched serial.tools.list_ports.comports
# ---------------------------------------------------------------------------


class TestPollingPortWatcherMonkeypatched:
    def test_empty_comports(self, monkeypatch):
        monkeypatch.setattr(usbwatch._list_ports, "comports", lambda: [])
        watcher = PollingPortWatcher()
        assert watcher.scan() == {}

    def test_one_matching_device(self, monkeypatch):
        monkeypatch.setattr(
            usbwatch._list_ports, "comports", lambda: [_raw(MATCHING)]
        )
        watcher = PollingPortWatcher()
        assert watcher.scan() == {"abc123": MATCHING}

    def test_one_non_matching_device(self, monkeypatch):
        monkeypatch.setattr(
            usbwatch._list_ports, "comports", lambda: [_raw(NON_MATCHING)]
        )
        watcher = PollingPortWatcher()
        assert watcher.scan() == {}

    def test_mixed_devices(self, monkeypatch):
        monkeypatch.setattr(
            usbwatch._list_ports,
            "comports",
            lambda: [_raw(MATCHING), _raw(NON_MATCHING)],
        )
        watcher = PollingPortWatcher()
        assert watcher.scan() == {"abc123": MATCHING}


def test_vid_pid_filter_matches_shared_constant():
    assert DAPLINK_VID_PID == (0x0D28, 0x0204)


# ---------------------------------------------------------------------------
# Interface parity: PollingPortWatcher vs. FakeUSBSource
# ---------------------------------------------------------------------------
#
# Same scripted snapshots, run through both implementations, must produce
# identical results -- proving FakeUSBSource is a faithful stand-in for
# PollingPortWatcher wherever a caller depends only on the PortWatcher
# interface.

PARITY_CASES = [
    pytest.param([], {}, id="empty"),
    pytest.param([MATCHING], {"abc123": MATCHING}, id="one-matching"),
    pytest.param([NON_MATCHING], {}, id="one-non-matching"),
    pytest.param(
        [MATCHING, NON_MATCHING, MATCHING2],
        {"abc123": MATCHING, "def456": MATCHING2},
        id="mixed",
    ),
]


@pytest.mark.parametrize("snapshot, expected", PARITY_CASES)
def test_polling_and_fake_agree(
    snapshot: Iterable[PortInfo], expected: dict[str, PortInfo]
):
    polling = PollingPortWatcher(comports_fn=lambda: [_raw(p) for p in snapshot])
    fake = FakeUSBSource([snapshot])

    polling_result = polling.scan()
    fake_result = fake.scan()

    assert polling_result == expected
    assert fake_result == expected
    assert polling_result == fake_result
