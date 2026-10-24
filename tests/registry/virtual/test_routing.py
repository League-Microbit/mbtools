"""stream / flash routing through the registry to a registered board's
endpoint (nezsim sprint 015 ticket 001)."""

from __future__ import annotations

import socket

import pytest

from mbtools.common import CODE_INTERNAL_ERROR, CODE_NOT_LOCKED

from virtual_helpers import JsonClient, SIM_SERIAL, register_request, wait_until

pytestmark = pytest.mark.requires_af_unix

UID = "sim-tinybot"


@pytest.fixture
def board(make_server, endpoints):
    """A registry with 'tinybot' registered, served by a fake endpoint."""

    def _go(**endpoint_kw):
        ep = endpoints(**endpoint_kw)
        srv = make_server()
        reg = JsonClient(srv.socket_path)
        assert reg.request(register_request("tinybot", ep.endpoint))["ok"]
        return srv, ep, reg

    return _go


def test_stream_requires_a_lock_first(board):
    srv, ep, reg = board()
    c = JsonClient(srv.socket_path)
    resp = c.request({"op": "stream", "uid": UID})
    assert resp["ok"] is False and resp["code"] == CODE_NOT_LOCKED
    assert ep.requests == []  # never dialed
    c.close()


def test_stream_splices_bytes_both_ways_until_close(board, locks):
    srv, ep, reg = board(greeting=b"HELLO")
    c = JsonClient(srv.socket_path)
    assert c.request({"op": "lock", "uid": UID, "kind": "serial"})["ok"]

    assert c.request({"op": "stream", "uid": UID}) == {"ok": True}

    # The endpoint was dialed with lock then stream.
    assert [r["op"] for r in ep.requests] == ["lock", "stream"]
    assert ep.requests[0]["kind"] == "serial"
    # endpoint -> client (raw, unparsed), then client -> endpoint -> client
    assert c.raw(5) == b"HELLO"
    c.sock.sendall(b"\x00\x01ping\xff")
    assert c.raw(7) == b"\x00\x01PING\xff"
    wait_until(lambda: bytes(ep.stream_bytes) == b"\x00\x01ping\xff")

    c.close()  # the client side ends the stream
    assert ep.stream_closed.wait(5.0)
    wait_until(lambda: locks.status(UID) is None)
    reg.close()


def test_stream_ends_and_lock_releases_when_the_endpoint_closes(board, locks):
    srv, ep, reg = board()
    c = JsonClient(srv.socket_path)
    c.request({"op": "lock", "uid": UID, "kind": "serial"})
    c.request({"op": "stream", "uid": UID})
    assert c.raw(5) == b"HELLO"

    ep.close()  # endpoint hangs up on everyone

    assert c.eof()  # registry closed our side
    wait_until(lambda: locks.status(UID) is None)
    reg.close()


def test_stream_relays_an_endpoint_refusal_as_the_response(board, locks):
    refusal = {"ok": False, "code": "invalid_request", "error": "no wire attached"}
    srv, ep, reg = board(stream_ack=refusal)
    c = JsonClient(srv.socket_path)
    c.request({"op": "lock", "uid": UID, "kind": "serial"})

    resp = c.request({"op": "stream", "uid": UID})

    assert resp == refusal
    # The connection is still in JSON mode and still holds its lock.
    assert c.request({"op": "unlock", "uid": UID})["released"] is True
    c.close()
    reg.close()


def test_stream_relays_an_endpoint_lock_refusal(board):
    refusal = {"ok": False, "code": "locked", "error": "device is locked (flash)"}
    srv, ep, reg = board(lock_ack=refusal)
    c = JsonClient(srv.socket_path)
    c.request({"op": "lock", "uid": UID, "kind": "serial"})
    assert c.request({"op": "stream", "uid": UID}) == refusal
    assert [r["op"] for r in ep.requests] == ["lock"]
    c.close()
    reg.close()


def test_stream_to_an_unreachable_endpoint_is_an_error_not_a_hang(make_server):
    dead = socket.socket()
    dead.bind(("127.0.0.1", 0))
    port = dead.getsockname()[1]
    dead.close()  # nothing listens here now
    srv = make_server()
    reg = JsonClient(srv.socket_path)
    assert reg.request(register_request("tinybot", f"127.0.0.1:{port}"))["ok"]
    c = JsonClient(srv.socket_path)
    c.request({"op": "lock", "uid": UID, "kind": "serial"})

    resp = c.request({"op": "stream", "uid": UID})

    assert resp["ok"] is False and resp["code"] == CODE_INTERNAL_ERROR
    c.close()
    reg.close()


def test_detach_while_streaming_ends_the_stream(board, locks):
    srv, ep, reg = board()
    c = JsonClient(srv.socket_path)
    c.request({"op": "lock", "uid": UID, "kind": "serial"})
    c.request({"op": "stream", "uid": UID})
    assert c.raw(5) == b"HELLO"

    reg.close()  # the simulator goes away

    assert c.eof()
    wait_until(lambda: locks.status(UID) is None)


# -- flash -----------------------------------------------------------------


@pytest.fixture
def hexfile(tmp_path):
    p = tmp_path / "prog.hex"
    # a minimal valid Intel hex: one data record + EOF
    p.write_text(":0400000001020304F2\n:00000001FF\n")
    return p


def _flash(c, hexfile):
    """Send flash and collect (log lines, terminal result)."""
    c.send({"op": "flash", "uid": UID, "hex_path": str(hexfile)})
    logs = []
    while True:
        msg = c.recv()
        if msg.get("type") == "log":
            logs.append(msg["line"])
        else:
            return logs, msg


def test_flash_forwards_hex_relays_logs_and_result_and_releases_lock(
    board, locks, store, hexfile
):
    srv, ep, reg = board()
    c = JsonClient(srv.socket_path)
    assert c.request({"op": "lock", "uid": UID, "kind": "flash"})["ok"]

    logs, result = _flash(c, hexfile)

    assert logs == ["erasing...", "programming..."]
    assert result == {"type": "result", "ok": True, "success": True, "exit_code": 0, "error": None}
    assert [r["op"] for r in ep.requests] == ["lock", "send_hex", "flash"]
    assert ep.requests[0]["kind"] == "flash"
    assert ep.requests[2]["hex_path"] == "/staged/1.hex"
    assert ep.received_hex == [hexfile.read_bytes()]
    assert locks.status(UID) is None
    assert store.get(UID).flash_count == 1
    c.close()
    reg.close()


def test_flash_failure_result_is_relayed_and_lock_still_released(
    board, locks, store, hexfile
):
    failed = {"type": "result", "ok": False, "success": False, "exit_code": 1,
              "code": "flash_failed", "error": "boom"}
    srv, ep, reg = board(flash_result=failed)
    c = JsonClient(srv.socket_path)
    c.request({"op": "lock", "uid": UID, "kind": "flash"})

    logs, result = _flash(c, hexfile)

    assert result == failed
    assert locks.status(UID) is None
    assert store.get(UID).flash_count == 1  # an attempted flash counts
    c.close()
    reg.close()


def test_flash_endpoint_lock_refusal_releases_lock_without_counting(
    board, locks, store, hexfile
):
    refusal = {"ok": False, "code": "locked", "error": "busy"}
    srv, ep, reg = board(lock_ack=refusal)
    c = JsonClient(srv.socket_path)
    c.request({"op": "lock", "uid": UID, "kind": "flash"})

    logs, result = _flash(c, hexfile)

    assert result["type"] == "result" and result["ok"] is False
    assert result["code"] == "locked" and result["error"] == "busy"
    assert locks.status(UID) is None
    assert store.get(UID).flash_count == 0
    c.close()
    reg.close()


def test_flash_to_unreachable_endpoint_fails_and_releases_lock(
    make_server, locks, hexfile
):
    dead = socket.socket()
    dead.bind(("127.0.0.1", 0))
    port = dead.getsockname()[1]
    dead.close()
    srv = make_server()
    reg = JsonClient(srv.socket_path)
    reg.request(register_request("tinybot", f"127.0.0.1:{port}"))
    c = JsonClient(srv.socket_path)
    c.request({"op": "lock", "uid": UID, "kind": "flash"})

    logs, result = _flash(c, hexfile)

    assert result["ok"] is False and result["code"] == CODE_INTERNAL_ERROR
    assert locks.status(UID) is None
    c.close()
    reg.close()


def test_flash_with_a_bad_hex_is_invalid_request_and_never_dials(
    board, locks, tmp_path
):
    bad = tmp_path / "bad.hex"
    bad.write_text("this is not intel hex\n")
    srv, ep, reg = board()
    c = JsonClient(srv.socket_path)
    c.request({"op": "lock", "uid": UID, "kind": "flash"})

    logs, result = _flash(c, bad)

    assert result["ok"] is False and result["code"] == "invalid_request"
    assert ep.requests == []
    assert locks.status(UID) is None
    c.close()
    reg.close()


def test_flash_requires_a_flash_lock(board, hexfile):
    srv, ep, reg = board()
    c = JsonClient(srv.socket_path)
    c.request({"op": "lock", "uid": UID, "kind": "serial"})
    logs, result = _flash(c, hexfile)
    assert result["ok"] is False and result["code"] == CODE_NOT_LOCKED
    assert ep.requests == []
    c.close()
    reg.close()


def test_mark_flashed_works_as_for_hardware(board, store):
    srv, ep, reg = board()
    c = JsonClient(srv.socket_path)
    assert c.request({"op": "mark_flashed", "uid": UID})["code"] == CODE_NOT_LOCKED
    c.request({"op": "lock", "uid": UID, "kind": "flash"})
    assert c.request({"op": "mark_flashed", "uid": UID})["ok"] is True
    assert store.get(UID).flash_count == 1
    c.close()
    reg.close()
