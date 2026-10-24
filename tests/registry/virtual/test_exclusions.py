"""The exclusion points that keep a registered virtual board out of the
hardware-only machinery (nezsim sprint 015 ticket 001). Each is a place
where, without a guard, a simulator would be corrupted or leaked:

1. ``Daemon.run_once``'s previously-attached diff (a USB scan that sees no
   board would mark the simulator disconnected) -- plus the flash-release
   hook, whose pending re-probe would expire and mark it known-blank.
2. Peering: snapshot payload, daemon events and lock events never reach peers.
3. The remote TCP API: list hides it, find/lock/stream say not_found.
4. The registry-start sweep removes stale virtual rows.
"""

from __future__ import annotations

import json

import pytest

from mbtools.common import CODE_NOT_FOUND, DAPLINK_VID_PID, PortInfo
from mbtools.registry.daemon import Daemon
from mbtools.registry.locks import KIND_FLASH, HolderRef
from mbtools.registry.peering import PeerDiscovery, _snapshot_payload
from mbtools.registry.remote_api import RemoteAPIServer
from mbtools.registry.store import STATE_CONNECTED, Store, format_vid_pid
from mbtools.registry.virtual import is_virtual
from mbtools.testing.fakes import FakeUSBSource

from virtual_helpers import JsonClient, register_request

VID, PID_ = DAPLINK_VID_PID
HW_UID = "9900" + "0000" + "11112222" + "3333444455556666" + "77778888" + "6e052820"


def _virtual_row(store: Store, name="tinybot"):
    return store.upsert_virtual(f"sim-{name}", name, "NEZHA2", "1198504156")


# -- 1. daemon ---------------------------------------------------------------


def test_usb_poll_cycle_does_not_detach_a_registered_board(store):
    rec = _virtual_row(store)
    assert is_virtual(rec)
    events = []
    daemon = Daemon(
        usbwatch=FakeUSBSource([[]]),  # a scan seeing no boards at all
        store=store,
        event_callback=lambda t, r: events.append((t, r.uid)),
    )

    daemon.run_once()
    daemon.run_once()

    after = store.get("sim-tinybot")
    assert after is not None and after.state == STATE_CONNECTED
    assert events == []  # no bogus detach


def test_poll_still_detaches_real_hardware_alongside_a_virtual_row(store):
    """Control: the filter excludes only virtual rows."""
    _virtual_row(store)
    store.upsert_attached(HW_UID, "/dev/ttyACM0", format_vid_pid(VID, PID_))
    events = []
    daemon = Daemon(
        usbwatch=FakeUSBSource([[]]),  # hardware is no longer on the bus
        store=store,
        event_callback=lambda t, r: events.append((t, r.uid)),
    )
    daemon.run_once()
    assert ("detach", HW_UID) in events
    assert ("detach", "sim-tinybot") not in events
    assert store.get("sim-tinybot").state == STATE_CONNECTED


def test_flash_release_on_a_virtual_board_never_marks_it_known_blank(store):
    _virtual_row(store)
    t = [0.0]
    daemon = Daemon(
        usbwatch=FakeUSBSource([[]]),
        store=store,
        flash_reprobe_timeout_s=1.0,
        now_fn=lambda: t[0],
    )
    holder = HolderRef(origin="local", ref="1", pid=1)
    daemon.locks.acquire("sim-tinybot", KIND_FLASH, holder)
    daemon.locks.release("sim-tinybot", holder)  # fires the flash-release hook
    t[0] = 100.0  # well past the re-probe deadline

    daemon.run_once()

    assert store.get("sim-tinybot").state == STATE_CONNECTED


# -- 2. peering --------------------------------------------------------------


class _CapturePub:
    def __init__(self):
        self.sent = []

    def send(self, message):
        self.sent.append(json.loads(message))


def _peering(store):
    pd = PeerDiscovery(store=store, host="testhost", advertise_address="127.0.0.1")
    pd._pub_socket = _CapturePub()
    return pd


def test_peer_snapshot_never_includes_a_registered_board(store):
    _virtual_row(store)
    assert _snapshot_payload(store) == []
    assert store.snapshot_local_devices() == []

    # control: a hardware row IS advertised
    store.upsert_attached(HW_UID, "/dev/ttyACM0", "0d28:0204")
    assert [d["uid"] for d in _snapshot_payload(store)] == [HW_UID]


def test_peer_events_never_include_a_registered_board(store):
    rec = _virtual_row(store)
    pd = _peering(store)

    for etype in ("attach", "identity", "detach"):
        pd.publish_daemon_event(etype, rec)
    pd.publish_lock_event("sim-tinybot", "serial", "pid 1", None, None)

    assert pd._pub_socket.sent == []

    # control: hardware events do publish
    hw = store.upsert_attached(HW_UID, "/dev/ttyACM0", "0d28:0204")
    pd.publish_daemon_event("attach", hw)
    pd.publish_lock_event(HW_UID, "serial", "pid 1", None, None)
    assert [m["type"] for m in pd._pub_socket.sent] == ["attach", "lock_state"]


@pytest.mark.requires_af_unix
def test_registering_over_the_api_publishes_nothing_to_peers(make_server, store, endpoints):
    pd = _peering(store)
    srv = make_server(event_callback=pd.publish_daemon_event)
    c = JsonClient(srv.socket_path)
    assert c.request(register_request("tinybot"))["ok"]
    c.close()
    from virtual_helpers import wait_until

    wait_until(lambda: store.get("sim-tinybot") is None)
    assert pd._pub_socket.sent == []


# -- 3. remote TCP API -------------------------------------------------------


def test_remote_api_hides_registered_boards(store, locks):
    import socket

    _virtual_row(store)
    store.upsert_attached(HW_UID, "/dev/ttyACM0", "0d28:0204")
    remote = RemoteAPIServer(
        host="127.0.0.1", port=0, store=store, locks=locks, sweep_interval_s=100.0
    )
    remote.start()
    try:
        s = socket.create_connection(("127.0.0.1", remote.port), timeout=5)
        f = s.makefile("rw", encoding="utf-8", newline="\n")

        def ask(obj):
            f.write(json.dumps(obj) + "\n")
            f.flush()
            return json.loads(f.readline())

        uids = [d["uid"] for d in ask({"op": "list"})["devices"]]
        assert uids == [HW_UID]
        for op in ({"op": "find", "uid": "sim-tinybot"}, {"op": "find", "uid": "tinybot"},
                   {"op": "lock", "uid": "sim-tinybot", "kind": "serial"}):
            resp = ask(op)
            assert resp["ok"] is False and resp["code"] == CODE_NOT_FOUND
        s.close()
    finally:
        remote.stop()


# -- 4. startup sweep --------------------------------------------------------


@pytest.mark.requires_af_unix
def test_registry_start_sweeps_stale_virtual_rows_but_not_hardware(
    socket_dir, store, locks
):
    from virtual_helpers import make_server_factory

    _virtual_row(store, "ghost")
    store.upsert_attached(HW_UID, "/dev/ttyACM0", "0d28:0204")
    make, servers = make_server_factory(socket_dir, store, locks)
    try:
        make()
        assert store.get("sim-ghost") is None
        assert store.get(HW_UID) is not None
    finally:
        for s in servers:
            s.stop()


def test_store_sweep_virtual_returns_removed_uids(store):
    _virtual_row(store, "one")
    _virtual_row(store, "two")
    assert sorted(store.sweep_virtual()) == ["sim-one", "sim-two"]
    assert store.list_devices() == []
