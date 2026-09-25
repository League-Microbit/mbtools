"""Integration-style tests for sprint 007 ticket 002's own acceptance
criterion: "a test using two Daemon/Store pairs sharing one fake claims
directory proves a uid claimed by one Daemon never appears in the other's
list_devices()" and SUC-005's "a crashed/killed claiming process's claim
is available to the other instance on its next attempt, with no manual
cleanup."

Kept as its own file (rather than folded entirely into test_daemon.py,
which already has one baseline version of the "only one claims" scenario)
per sprint.md's own Test Strategy naming this an "integration-style test"
distinct from the rest of that module's fake-claim-outcome unit tests --
every test here uses the real :mod:`mbtools.registry.claims` module
against a real ``tmp_path``-backed directory (real ``flock``), never a
fake claim outcome, and never a real board/probe.
"""

from __future__ import annotations

import itertools

from mbtools.common import DAPLINK_VID_PID, PortInfo
from mbtools.registry import claims
from mbtools.registry.daemon import Daemon
from mbtools.registry.store import STATE_CONNECTED, STATE_DISCONNECTED, Store
from mbtools.testing.fakes import (
    FakeSerial,
    FakeUSBSource,
    unavailable_chip_identity_session_factory,
)

VID, PID_ = DAPLINK_VID_PID
UID = "9900" + "0000" + "11112222" + "3333444455556666" + "77778888" + "6e052820"
ANNOUNCEMENT = "device NEZHA2 robot vevov 1198504156"


def _port_info(uid: str = UID, port: str = "/dev/ttyACM0") -> PortInfo:
    return PortInfo(uid=uid, port=port, vid=VID, pid=PID_)


def _store_clock(start: float = 1000.0, step: float = 1.0):
    counter = itertools.count()
    return lambda: start + step * next(counter)


class _ProbeScript:
    def __init__(self, announcements):
        self._queue = list(announcements)
        self.calls = 0

    def __call__(self, **kwargs):
        self.calls += 1
        announcement = self._queue.pop(0) if self._queue else None
        return FakeSerial(announcement=announcement, **kwargs)


def _make_daemon(usbwatch, store, claims_dir, port: str = "/dev/ttyACM0"):
    return Daemon(
        usbwatch=usbwatch,
        store=store,
        serial_factory=_ProbeScript([ANNOUNCEMENT]),
        probe_timeout_s=0.05,
        settle_s=0,
        claim_fn=lambda uid: claims.try_claim(uid, claims_dir=claims_dir),
        # pyocd is a real, installed dependency of this project -- never
        # let a test that happens to reach the SWD fallback path touch a
        # real session (see mbtools.testing.fakes's own docstring).
        chip_identity_session_factory=unavailable_chip_identity_session_factory,
    )


def test_uid_claimed_by_one_daemon_never_appears_in_the_others_list(tmp_path):
    claims_dir = tmp_path / "claims"
    store_a = Store(tmp_path / "a" / "devices.db", now_fn=_store_clock())
    store_b = Store(tmp_path / "b" / "devices.db", now_fn=_store_clock())

    # Same physical board, same uid, seen by both instances' own USB scan
    # -- exactly SUC-005's "both instances' daemons see the board in their
    # own USB scan" precondition.
    daemon_a = _make_daemon(store=store_a, usbwatch=FakeUSBSource([[_port_info()]]), claims_dir=claims_dir)
    daemon_b = _make_daemon(store=store_b, usbwatch=FakeUSBSource([[_port_info()]]), claims_dir=claims_dir)

    daemon_a.run_once()
    daemon_b.run_once()

    devices_a = store_a.list_devices()
    devices_b = store_b.list_devices()

    a_lists_it = any(d.uid == UID for d in devices_a)
    b_lists_it = any(d.uid == UID for d in devices_b)
    assert a_lists_it != b_lists_it  # exactly one -- never both, never neither

    # Confirm it stays that way across further cycles, not just the first.
    for _ in range(3):
        daemon_a.run_once()
        daemon_b.run_once()
    devices_a = store_a.list_devices()
    devices_b = store_b.list_devices()
    assert (any(d.uid == UID for d in devices_a)) == a_lists_it
    assert (any(d.uid == UID for d in devices_b)) == b_lists_it


def test_losing_daemon_claims_it_after_the_winner_detaches(tmp_path):
    claims_dir = tmp_path / "claims"
    store_a = Store(tmp_path / "a" / "devices.db", now_fn=_store_clock())
    store_b = Store(tmp_path / "b" / "devices.db", now_fn=_store_clock())

    usbwatch_a = FakeUSBSource([[_port_info()], [_port_info()], []])  # detaches on cycle 3
    usbwatch_b = FakeUSBSource([[_port_info()]])  # repeats -- keeps trying

    daemon_a = _make_daemon(store=store_a, usbwatch=usbwatch_a, claims_dir=claims_dir)
    daemon_b = _make_daemon(store=store_b, usbwatch=usbwatch_b, claims_dir=claims_dir)

    daemon_a.run_once()  # a claims it (goes first)
    daemon_b.run_once()  # b is denied

    assert store_a.get(UID) is not None
    assert store_b.get(UID) is None

    daemon_a.run_once()  # a still holds it
    daemon_b.run_once()  # b still denied
    assert store_b.get(UID) is None

    daemon_a.run_once()  # a's board detaches -- releases the claim
    assert store_a.get(UID).state == STATE_DISCONNECTED

    daemon_b.run_once()  # b's next attempt now succeeds
    assert store_b.get(UID) is not None
    assert store_b.get(UID).state == STATE_CONNECTED
