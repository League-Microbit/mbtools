"""register_board: wire contract, list shaping, events, errors, lifetime
(nezsim sprint 015 ticket 001)."""

from __future__ import annotations

import socket
import threading

import pytest

from mbtools.common import (
    CODE_INVALID_REQUEST,
    CODE_NAME_IN_USE,
    CODE_NAME_RESERVED,
)
from mbtools.registry.remote_api import RemoteAPIServer
from mbtools.registry.store import STATE_CONNECTED

from virtual_helpers import JsonClient, SIM_SERIAL, register_request, wait_until

pytestmark = pytest.mark.requires_af_unix


def _watch(srv):
    """Open a watch connection and return (client, events list)."""
    c = JsonClient(srv.socket_path)
    assert c.request({"op": "watch"}) == {"ok": True}
    events: list[dict] = []

    def reader():
        try:
            while True:
                events.append(c.recv())
        except Exception:
            pass

    threading.Thread(target=reader, daemon=True).start()
    return c, events


def test_register_acks_and_row_is_an_ordinary_local_device(make_server, store):
    srv = make_server()
    c = JsonClient(srv.socket_path)

    resp = c.request(register_request("tinybot"))

    assert resp["ok"] is True
    assert resp["uid"] == "sim-tinybot"
    dev = resp["device"]
    assert dev["uid"] == "sim-tinybot"
    assert dev["host"] is None
    assert dev["port"] == "virtual:tinybot"
    assert dev["vid_pid"] == "virtual"
    assert dev["state"] == STATE_CONNECTED
    assert dev["role"] == "NEZHA2"
    assert dev["common_name"] == "robot"
    assert dev["device_name"] == "tinybot"
    assert dev["serial_payload"] == SIM_SERIAL
    assert dev["simulated"] is True
    assert dev["endpoint"] is None
    c.close()


def test_registered_board_in_list_find_with_simulated_flag(make_server):
    srv = make_server()
    c = JsonClient(srv.socket_path)
    c.request(register_request("tinybot"))
    other = JsonClient(srv.socket_path)

    listed = other.request({"op": "list"})["devices"]
    [dev] = [d for d in listed if d["uid"] == "sim-tinybot"]
    assert dev["host"] is None and dev["simulated"] is True

    for token in ("tinybot", "TinyBot", "sim-tinybot"):
        found = other.request({"op": "find", "uid": token})
        assert found["ok"] is True and found["device"]["simulated"] is True
    other.close()
    c.close()


def test_hardware_rows_report_simulated_false(make_server, store):
    store.upsert_attached("aa" * 24, "/dev/ttyACM0", "0d28:0204")
    srv = make_server()
    c = JsonClient(srv.socket_path)
    [dev] = c.request({"op": "list"})["devices"]
    assert dev["simulated"] is False
    c.close()


def test_register_emits_attach_then_identity_with_simulated(make_server):
    srv = make_server()
    w, events = _watch(srv)
    c = JsonClient(srv.socket_path)

    c.request(register_request("tinybot"))

    wait_until(lambda: len(events) >= 2)
    assert [e["type"] for e in events[:2]] == ["attach", "identity"]
    assert events[0]["uid"] == "sim-tinybot"
    assert events[0]["simulated"] is True
    assert events[1]["simulated"] is True
    assert events[1]["device_name"] == "tinybot"
    assert events[1]["role"] == "NEZHA2"
    assert events[1]["serial_payload"] == SIM_SERIAL
    w.close()
    c.close()


def test_close_detaches_removes_row_and_emits_detach(make_server, store):
    srv = make_server()
    w, events = _watch(srv)
    c = JsonClient(srv.socket_path)
    c.request(register_request("tinybot"))
    wait_until(lambda: len(events) >= 2)

    c.close()

    wait_until(lambda: any(e["type"] == "detach" for e in events))
    detach = [e for e in events if e["type"] == "detach"][0]
    assert detach["uid"] == "sim-tinybot"
    assert detach["simulated"] is True
    wait_until(lambda: store.get("sim-tinybot") is None)
    w2 = JsonClient(srv.socket_path)
    assert w2.request({"op": "list"})["devices"] == []
    w2.close()
    w.close()


def test_abrupt_death_without_clean_close_also_detaches(make_server, store):
    srv = make_server()
    c = JsonClient(srv.socket_path)
    c.request(register_request("tinybot"))
    assert store.get("sim-tinybot") is not None

    # RST-style death: no shutdown(), no FIN handshake, just reset the fd.
    c.sock.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, b"\x01\x00\x00\x00\x00\x00\x00\x00")
    import os

    os.close(c.sock.detach())  # makefile() refs keep a plain close() from closing the fd

    wait_until(lambda: store.get("sim-tinybot") is None)


def test_lock_held_on_a_board_is_released_when_it_detaches(make_server, locks):
    srv = make_server()
    reg = JsonClient(srv.socket_path)
    reg.request(register_request("tinybot"))
    user = JsonClient(srv.socket_path)
    assert user.request({"op": "lock", "uid": "sim-tinybot", "kind": "serial"})["ok"]
    assert locks.status("sim-tinybot") is not None

    reg.close()

    wait_until(lambda: locks.status("sim-tinybot") is None)
    user.close()


@pytest.mark.parametrize("name", ["tovez", "TOVEZ", "gopiv", "zeguz", " tovez ".strip()])
def test_real_robot_names_are_reserved_and_register_nothing(make_server, store, name):
    srv = make_server()
    c = JsonClient(srv.socket_path)

    resp = c.request(register_request(name))

    assert resp["ok"] is False
    assert resp["code"] == CODE_NAME_RESERVED
    assert store.list_devices() == []
    assert make_server.events == []
    c.close()


def test_name_in_use_while_live_then_reusable_after_detach(make_server, store):
    srv = make_server()
    first = JsonClient(srv.socket_path)
    assert first.request(register_request("tinybot"))["ok"]
    second = JsonClient(srv.socket_path)

    for name in ("tinybot", "TinyBot"):
        resp = second.request(register_request(name))
        assert resp["ok"] is False and resp["code"] == CODE_NAME_IN_USE
    assert [r.uid for r in store.list_devices()] == ["sim-tinybot"]

    first.close()
    wait_until(lambda: store.get("sim-tinybot") is None)
    assert second.request(register_request("tinybot"))["ok"] is True
    second.close()


@pytest.mark.parametrize(
    "over",
    [
        {"name": ""},
        {"name": "a"},
        {"name": "has space"},
        {"name": "bad@host"},
        {"name": 7},
        {"endpoint": "nocolon"},
        {"endpoint": "host:0"},
        {"endpoint": "host:99999"},
        {"endpoint": ":123"},
        {"endpoint": 5},
        {"role": "bad role"},
        {"serial": "12ab"},
        {"serial": ""},
        {"simulated": False},
        {"simulated": "yes"},
    ],
)
def test_invalid_fields_are_invalid_request(make_server, store, over):
    srv = make_server()
    c = JsonClient(srv.socket_path)

    resp = c.request(register_request(**over))

    assert resp["ok"] is False
    assert resp["code"] == CODE_INVALID_REQUEST
    assert store.list_devices() == []
    c.close()


def test_missing_serial_is_invalid(make_server):
    srv = make_server()
    c = JsonClient(srv.socket_path)
    req = register_request()
    del req["serial"]
    assert c.request(req)["code"] == CODE_INVALID_REQUEST
    c.close()


def test_register_board_is_unknown_op_on_the_remote_tcp_port(store, locks):
    remote = RemoteAPIServer(
        host="127.0.0.1", port=0, store=store, locks=locks, sweep_interval_s=100.0
    )
    remote.start()
    try:
        s = socket.create_connection(("127.0.0.1", remote.port), timeout=5)
        f = s.makefile("rw", encoding="utf-8", newline="\n")
        import json

        f.write(json.dumps(register_request()) + "\n")
        f.flush()
        resp = json.loads(f.readline())
        assert resp["ok"] is False
        assert resp["code"] == CODE_INVALID_REQUEST
        assert "unknown op" in resp["error"]
        assert store.list_devices() == []
        s.close()
    finally:
        remote.stop()


def test_assembly_wires_the_event_callback_into_the_api_server(
    socket_dir, store
):
    """``assemble_daemon_and_api`` hands the same event callback to the
    local API server that it gives the Daemon, so registrations reach the
    bus (and peering) exactly like hardware attach/detach."""
    from mbtools.registry.cli import assemble_daemon_and_api
    from mbtools.testing.fakes import FakeUSBSource

    seen = []
    daemon, api = assemble_daemon_and_api(
        store=store,
        usbwatch=FakeUSBSource([[]]),
        socket_path=f"{socket_dir}/api.sock",
        event_callback=lambda t, r: seen.append((t, r.uid)),
    )
    api.start()
    try:
        c = JsonClient(api.socket_path)
        assert c.request(register_request("tinybot"))["ok"]
        assert seen == [("attach", "sim-tinybot"), ("identity", "sim-tinybot")]
        daemon.run_once()  # a USB poll in the same assembly leaves it alone
        assert store.get("sim-tinybot").state == STATE_CONNECTED
        c.close()
        wait_until(lambda: seen[-1] == ("detach", "sim-tinybot"))
    finally:
        api.stop()
