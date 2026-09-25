"""Tests for mbtools.registry.remote_api -- the TCP control-plane server
(ticket 006) that gives a client on another host the same list/find/
lock/unlock/mark_flashed access api.RegistryAPIServer's local Unix
socket gives, scoped to this registry's own (host IS NULL) devices.

Every test here drives a real RemoteAPIServer over a real AF_INET
loopback socket (port=0, an OS-chosen ephemeral port via `bound_port`,
so parallel test runs never collide) -- mirrors
tests/registry/api/test_api.py's own "drive the real server over a real
socket" precedent for the Unix-socket path.
"""

from __future__ import annotations

import json
import shutil
import socket
import tempfile
import threading
import time

import pytest

from mbtools.common import (
    CODE_AMBIGUOUS_NAME,
    CODE_INVALID_REQUEST,
    CODE_LOCKED,
    CODE_NOT_FOUND,
    CODE_NOT_LOCKED,
    CODE_UNAUTHORIZED,
)
from mbtools.registry.api import RegistryAPIServer
from mbtools.registry.eventbus import EventBus
from mbtools.registry.flash import FlashOp
from mbtools.registry.identity import ProbeResult
from mbtools.registry.locks import KIND_FLASH, KIND_SERIAL, HolderRef, LockManager
from mbtools.registry.remote_api import RemoteAPIServer
from mbtools.registry.store import Store

LOCAL_UID = "aa00" + "0000" + "11112222" + "3333444455556666" + "77778888" + "6e052820"
REMOTE_UID = "bb00" + "0000" + "11112222" + "3333444455556666" + "77778888" + "6e052820"
VID_PID = "0d28:0204"
PEER_HOST = "peer1"


# ---------------------------------------------------------------------------
# test doubles / helpers
# ---------------------------------------------------------------------------


class _SpyRunner:
    """Fake pyOCD-subprocess runner -- never shells out. Only needed
    because RegistryAPIServer's constructor requires a FlashOp; no test
    here actually flashes anything."""

    def __call__(self, cmd, log) -> int:
        return 0


class _Client:
    """A minimal newline-delimited-JSON client over a real AF_INET
    loopback socket -- exercises the wire protocol exactly as a real
    remote client (ticket 011's registry.remote_client) would."""

    def __init__(self, port: int, host: str = "127.0.0.1"):
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.sock.connect((host, port))
        self.rfile = self.sock.makefile("r", encoding="utf-8", newline="\n")
        self.wfile = self.sock.makefile("w", encoding="utf-8", newline="\n")

    def send(self, obj: dict) -> None:
        self.wfile.write(json.dumps(obj))
        self.wfile.write("\n")
        self.wfile.flush()

    def recv(self) -> dict:
        line = self.rfile.readline()
        if not line:
            raise ConnectionError("mbregistry remote_api: connection closed")
        return json.loads(line)

    def request(self, obj: dict) -> dict:
        self.send(obj)
        return self.recv()

    def close(self) -> None:
        for f in (self.wfile, self.rfile):
            try:
                f.close()
            except OSError:
                pass
        self.sock.close()


def _wait_until(predicate, timeout=2.0, interval=0.01) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(interval)
    assert predicate(), "condition never became true within timeout"


# ---------------------------------------------------------------------------
# fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def store(tmp_path):
    s = Store(tmp_path / "devices.db")
    s.upsert_attached(LOCAL_UID, "/dev/ttyACM0", VID_PID)
    s.apply_probe_result(
        LOCAL_UID,
        ProbeResult(
            role="robot", common_name="Robot", device_name="alpha", serial="123", raw="DEVICE:alpha"
        ),
    )
    s.upsert_remote_attached(REMOTE_UID, PEER_HOST, "/dev/ttyACM0", VID_PID)
    s.apply_remote_probe(
        REMOTE_UID,
        ProbeResult(
            role="robot", common_name="Robot", device_name="beta", serial="456", raw="DEVICE:beta"
        ),
    )
    yield s
    s.close()


@pytest.fixture
def locks():
    return LockManager()


@pytest.fixture
def remote_server(store, locks):
    servers: list[RemoteAPIServer] = []

    def _make(
        *,
        auth_token=None,
        sweep_interval_s=100.0,
        lock: threading.RLock | None = None,
        eventbus=None,
    ):
        srv = RemoteAPIServer(
            host="127.0.0.1",
            port=0,
            store=store,
            locks=locks,
            auth_token=auth_token,
            sweep_interval_s=sweep_interval_s,
            lock=lock,
            eventbus=eventbus,
        )
        srv.start()
        servers.append(srv)
        return srv

    yield _make
    for srv in servers:
        srv.stop()


@pytest.fixture
def local_server(tmp_path, store, locks):
    """A real Unix-socket RegistryAPIServer sharing this test's own
    store/locks -- used only by the local+remote lock-conflict tests, to
    prove the two servers genuinely share one LockManager table (ticket
    002, Decision 2)."""
    servers: list[RegistryAPIServer] = []
    socket_dir = tempfile.mkdtemp(prefix="mbregistry-remote-api-")

    def _make(*, lock: threading.RLock | None = None):
        flash_op = FlashOp(locks=locks, store=store, runner=_SpyRunner())
        srv = RegistryAPIServer(
            socket_path=f"{socket_dir}/api.sock",
            store=store,
            locks=locks,
            flash_op=flash_op,
            sweep_interval_s=100.0,
            lock=lock,
        )
        srv.start()
        servers.append(srv)
        return srv

    yield _make
    for srv in servers:
        srv.stop()
    shutil.rmtree(socket_dir, ignore_errors=True)


# ---------------------------------------------------------------------------
# list / find -- scoped to this registry's own (host IS NULL) devices
# ---------------------------------------------------------------------------


def test_list_only_includes_this_registrys_own_devices(remote_server):
    srv = remote_server()
    client = _Client(srv.bound_port)

    resp = client.request({"op": "list"})

    assert resp["ok"] is True
    uids = {d["uid"] for d in resp["devices"]}
    assert uids == {LOCAL_UID}  # REMOTE_UID (host="peer1") is excluded
    client.close()


def test_find_local_device_resolves_normally(remote_server):
    srv = remote_server()
    client = _Client(srv.bound_port)

    resp = client.request({"op": "find", "uid": "alpha"})

    assert resp["ok"] is True
    assert resp["device"]["uid"] == LOCAL_UID
    client.close()


def test_find_a_peer_owned_device_is_not_found(remote_server):
    """A remote client resolving a name this registry doesn't own (it
    only has a cached, peer-owned row for it) gets not_found, exactly as
    if the device didn't exist here -- ticket 006 acceptance criterion
    #3; this API never forwards to a third host (Decision 8)."""
    srv = remote_server()
    client = _Client(srv.bound_port)

    resp = client.request({"op": "find", "uid": REMOTE_UID})

    assert resp["ok"] is False
    assert resp["code"] == CODE_NOT_FOUND
    client.close()


def test_find_ambiguous_name_returns_ambiguous_name_code(tmp_path):
    """store.find() raises AmbiguousNameError (ticket 001) when a bare
    name collides across hosts; remote_api (this ticket's first real
    API-layer caller) is where that gets translated to a wire code."""
    s = Store(tmp_path / "ambiguous.db")
    s.upsert_attached(LOCAL_UID, "/dev/ttyACM0", VID_PID)
    s.apply_probe_result(
        LOCAL_UID,
        ProbeResult(role="robot", common_name="Robot", device_name="dup", serial="1", raw="x"),
    )
    s.upsert_remote_attached(REMOTE_UID, PEER_HOST, "/dev/ttyACM0", VID_PID)
    s.apply_remote_probe(
        REMOTE_UID,
        ProbeResult(role="robot", common_name="Robot", device_name="dup", serial="2", raw="y"),
    )
    locks = LockManager()
    srv = RemoteAPIServer(host="127.0.0.1", port=0, store=s, locks=locks, sweep_interval_s=100.0)
    srv.start()
    client = _Client(srv.bound_port)

    resp = client.request({"op": "find", "uid": "dup"})

    assert resp["ok"] is False
    assert resp["code"] == CODE_AMBIGUOUS_NAME
    assert set(resp["hosts"]) == {None, PEER_HOST}
    client.close()
    srv.stop()
    s.close()


# ---------------------------------------------------------------------------
# lock / unlock
# ---------------------------------------------------------------------------


def test_lock_unlock_happy_path(remote_server, locks):
    srv = remote_server()
    client = _Client(srv.bound_port)

    resp = client.request({"op": "lock", "uid": LOCAL_UID, "kind": KIND_SERIAL})
    assert resp == {"ok": True}
    status = locks.status(LOCAL_UID)
    assert status.kind == KIND_SERIAL
    assert status.holder.origin == "remote"
    assert status.holder.host == "127.0.0.1"

    resp = client.request({"op": "unlock", "uid": LOCAL_UID})
    assert resp == {"ok": True, "released": True}
    assert locks.status(LOCAL_UID) is None
    client.close()


def test_lock_a_peer_owned_device_is_not_found(remote_server):
    srv = remote_server()
    client = _Client(srv.bound_port)

    resp = client.request({"op": "lock", "uid": REMOTE_UID, "kind": KIND_SERIAL})

    assert resp["ok"] is False
    assert resp["code"] == CODE_NOT_FOUND
    client.close()


def test_two_remote_sessions_conflict_and_holder_is_session_shaped(remote_server):
    srv = remote_server()
    holder_client = _Client(srv.bound_port)
    assert holder_client.request(
        {"op": "lock", "uid": LOCAL_UID, "kind": KIND_FLASH}
    )["ok"] is True

    contender = _Client(srv.bound_port)
    resp = contender.request({"op": "lock", "uid": LOCAL_UID, "kind": KIND_SERIAL})

    assert resp["ok"] is False
    assert resp["code"] == CODE_LOCKED
    assert resp["holder"]["kind"] == KIND_FLASH
    assert resp["holder"]["pid"] is None
    assert resp["holder"]["origin"] == "remote"
    assert resp["holder"]["host"] == "127.0.0.1"
    holder_client.close()
    contender.close()


@pytest.mark.requires_af_unix
def test_local_holder_beats_remote_contender_with_unchanged_2_key_shape(
    remote_server, local_server
):
    """A local Unix-socket client and a remote TCP client contend for the
    same uid through the one shared LockManager table (ticket 002,
    Decision 2) -- proving lock/unlock genuinely share state, not just
    the same code, and that the local holder's wire shape in a `locked`
    response is unaffected by a remote contender existing at all (ticket
    006 acceptance criteria #1/#8)."""
    shared_lock = threading.RLock()
    unix_srv = local_server(lock=shared_lock)
    tcp_srv = remote_server(lock=shared_lock)

    unix_sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    unix_sock.connect(str(unix_srv.socket_path))
    unix_rfile = unix_sock.makefile("r", encoding="utf-8", newline="\n")
    unix_wfile = unix_sock.makefile("w", encoding="utf-8", newline="\n")

    def unix_request(obj):
        unix_wfile.write(json.dumps(obj))
        unix_wfile.write("\n")
        unix_wfile.flush()
        return json.loads(unix_rfile.readline())

    assert unix_request({"op": "lock", "uid": LOCAL_UID, "kind": KIND_SERIAL})["ok"] is True

    remote_client = _Client(tcp_srv.bound_port)
    resp = remote_client.request({"op": "lock", "uid": LOCAL_UID, "kind": KIND_FLASH})

    assert resp["ok"] is False
    assert resp["code"] == CODE_LOCKED
    # The local holder's wire shape is unchanged -- exactly 2 keys, no
    # origin/host added, even though the contender was remote.
    assert resp["holder"]["kind"] == KIND_SERIAL
    assert isinstance(resp["holder"]["pid"], int)
    assert set(resp["holder"].keys()) == {"kind", "pid"}

    remote_client.close()
    for f in (unix_rfile, unix_wfile):
        f.close()
    unix_sock.close()


# ---------------------------------------------------------------------------
# mark_flashed
# ---------------------------------------------------------------------------


def test_mark_flashed_increments_flash_count(remote_server, locks, store):
    srv = remote_server()
    client = _Client(srv.bound_port)
    client.request({"op": "lock", "uid": LOCAL_UID, "kind": KIND_FLASH})

    resp = client.request({"op": "mark_flashed", "uid": LOCAL_UID})

    assert resp == {"ok": True}
    assert store.find(LOCAL_UID).flash_count == 1
    client.close()


def test_mark_flashed_without_flash_lock_is_refused(remote_server):
    srv = remote_server()
    client = _Client(srv.bound_port)

    resp = client.request({"op": "mark_flashed", "uid": LOCAL_UID})

    assert resp["ok"] is False
    assert resp["code"] == CODE_NOT_LOCKED
    client.close()


# ---------------------------------------------------------------------------
# connection close / periodic sweep release a session's locks
# ---------------------------------------------------------------------------


def test_connection_close_releases_that_sessions_locks(remote_server, locks):
    srv = remote_server()
    client = _Client(srv.bound_port)
    client.request({"op": "lock", "uid": LOCAL_UID, "kind": KIND_SERIAL})
    assert locks.status(LOCAL_UID) is not None

    client.close()  # no explicit unlock

    _wait_until(lambda: locks.status(LOCAL_UID) is None)


def test_periodic_sweep_releases_a_vanished_remote_sessions_lock(remote_server, locks):
    """Simulates a remote session that vanished without an observable
    connection close (a network partition, ticket 006 acceptance
    criterion #5) by acquiring a lock for a HolderRef whose session id
    was never registered in the server's own live-session set -- the
    same "construct a HolderRef directly, prove the liveness dispatch
    releases it" pattern ticket 002's own tests used for local PIDs.
    """
    srv = remote_server(sweep_interval_s=0.05)
    ghost = HolderRef(origin="remote", ref="ghost-session", host="10.0.0.99")
    locks.acquire(LOCAL_UID, KIND_SERIAL, ghost)
    assert "ghost-session" not in srv._live_sessions

    _wait_until(lambda: locks.status(LOCAL_UID) is None, timeout=2.0)


# ---------------------------------------------------------------------------
# TCP keepalive
# ---------------------------------------------------------------------------


def test_configure_keepalive_enables_so_keepalive():
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.bind(("127.0.0.1", 0))
    listener.listen(1)
    client_sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    client_sock.connect(listener.getsockname())
    conn, _addr = listener.accept()
    try:
        RemoteAPIServer._configure_keepalive(conn)
        assert conn.getsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE) != 0
    finally:
        conn.close()
        client_sock.close()
        listener.close()


# ---------------------------------------------------------------------------
# auth token
# ---------------------------------------------------------------------------


def test_op_dispatched_immediately_when_no_auth_token_configured(remote_server):
    srv = remote_server(auth_token=None)
    client = _Client(srv.bound_port)

    resp = client.request({"op": "list"})

    assert resp["ok"] is True
    client.close()


def test_connection_without_a_token_is_rejected_when_auth_token_set(remote_server):
    srv = remote_server(auth_token="s3cret")
    client = _Client(srv.bound_port)

    resp = client.request({"op": "list"})  # no token -- treated as the auth message

    assert resp["ok"] is False
    assert resp["code"] == CODE_UNAUTHORIZED
    client.close()


def test_connection_with_the_wrong_token_is_rejected(remote_server):
    srv = remote_server(auth_token="s3cret")
    client = _Client(srv.bound_port)

    resp = client.request({"token": "wrong"})

    assert resp["ok"] is False
    assert resp["code"] == CODE_UNAUTHORIZED
    client.close()


def test_connection_with_the_right_token_may_then_dispatch_ops(remote_server):
    srv = remote_server(auth_token="s3cret")
    client = _Client(srv.bound_port)

    auth_resp = client.request({"token": "s3cret"})
    assert auth_resp == {"ok": True}

    resp = client.request({"op": "list"})
    assert resp["ok"] is True
    client.close()


def test_no_op_is_dispatched_before_a_bad_token_is_rejected(remote_server, locks):
    """A rejected connection must never have reached op dispatch --
    sneaking an op into the same first line as a (wrong) token must not
    grant it."""
    srv = remote_server(auth_token="s3cret")
    client = _Client(srv.bound_port)

    client.request({"token": "wrong", "op": "lock", "uid": LOCAL_UID, "kind": KIND_SERIAL})

    assert locks.status(LOCAL_UID) is None
    client.close()


# ---------------------------------------------------------------------------
# malformed / unknown requests
# ---------------------------------------------------------------------------


def test_malformed_json_returns_invalid_request(remote_server):
    srv = remote_server()
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.connect(("127.0.0.1", srv.bound_port))
    sock.sendall(b"not json at all\n")
    rfile = sock.makefile("r", encoding="utf-8")
    resp = json.loads(rfile.readline())

    assert resp["ok"] is False
    assert resp["code"] == CODE_INVALID_REQUEST
    sock.close()


def test_unknown_op_returns_invalid_request(remote_server):
    srv = remote_server()
    client = _Client(srv.bound_port)

    resp = client.request({"op": "not_a_real_op", "uid": LOCAL_UID})

    assert resp["ok"] is False
    assert resp["code"] == CODE_INVALID_REQUEST
    client.close()


def test_flash_without_a_staged_hex_path_is_invalid_request(remote_server):
    """`flash`'s own territory (ticket 008) is covered in
    test_remote_flash.py -- this only re-proves `flash` is no longer an
    *unknown* op on this server (see the previous test) while also
    exercising the "hex_path must come from this connection's own
    send_hex" guard without a real pyocd invocation."""
    srv = remote_server()
    client = _Client(srv.bound_port)
    client.request({"op": "lock", "uid": LOCAL_UID, "kind": KIND_FLASH})

    resp = client.request(
        {"op": "flash", "uid": LOCAL_UID, "hex_path": "/tmp/not-staged.hex"}
    )

    assert resp["ok"] is False
    assert resp["code"] == CODE_INVALID_REQUEST
    assert resp["type"] == "result"
    client.close()


# ---------------------------------------------------------------------------
# watch (sprint 008, ticket 001) -- mirrors tests/registry/api/test_api.py's
# own watch tests, over the remote TCP transport instead of the local
# Unix socket, dispatched through the same shared
# _api_base.BaseAPIServer._op_watch/_handle_watch.
# ---------------------------------------------------------------------------


def test_watch_acks_then_streams_published_events_in_order(remote_server):
    bus = EventBus()
    srv = remote_server(eventbus=bus)
    client = _Client(srv.bound_port)

    ack = client.request({"op": "watch"})
    assert ack == {"ok": True}

    event1 = {"type": "attach", "uid": LOCAL_UID, "port": "/dev/ttyACM0", "vid_pid": VID_PID}
    event2 = {"type": "lock_state", "uid": LOCAL_UID, "kind": "serial", "display": "pid 1"}
    bus.publish(event1)
    bus.publish(event2)

    assert client.recv() == event1
    assert client.recv() == event2
    client.close()


def test_watch_fans_out_to_every_connected_watcher(remote_server):
    bus = EventBus()
    srv = remote_server(eventbus=bus)
    watcher_a = _Client(srv.bound_port)
    watcher_b = _Client(srv.bound_port)
    assert watcher_a.request({"op": "watch"}) == {"ok": True}
    assert watcher_b.request({"op": "watch"}) == {"ok": True}

    event = {"type": "detach", "uid": LOCAL_UID}
    bus.publish(event)

    assert watcher_a.recv() == event
    assert watcher_b.recv() == event
    watcher_a.close()
    watcher_b.close()


def test_watch_sees_no_snapshot_only_events_published_after_subscribing(remote_server):
    bus = EventBus()
    srv = remote_server(eventbus=bus)
    bus.publish({"type": "attach", "uid": "before-watch"})

    client = _Client(srv.bound_port)
    assert client.request({"op": "watch"}) == {"ok": True}

    after = {"type": "attach", "uid": "after-watch"}
    bus.publish(after)

    assert client.recv() == after
    client.close()


def test_watch_client_disconnect_cleanly_unsubscribes(remote_server):
    """No leaked queue, no exception on the next publish() -- mirrors
    test_api.py's own equivalent test; see that test's docstring for why
    a publish (not the bare close) is what surfaces cleanup here.

    Unlike a Unix-domain socket, a first write to a TCP socket whose peer
    already closed can succeed silently (it lands in the local send
    buffer before the RST arrives) -- so this keeps publishing (each
    call must not raise) until the second or later write actually
    surfaces the broken connection and the handler thread unsubscribes.
    """
    bus = EventBus()
    srv = remote_server(eventbus=bus)
    client = _Client(srv.bound_port)
    assert client.request({"op": "watch"}) == {"ok": True}

    client.close()

    def _no_subscribers_left():
        with bus._lock:
            if len(bus._subscribers) == 0:
                return True
        bus.publish({"type": "attach", "uid": "after-close"})  # must not raise
        return False

    _wait_until(_no_subscribers_left)


def test_watch_requires_auth_token_when_configured(remote_server, locks):
    """`watch` gets no auth exemption -- the same first-line token
    handshake every other op requires still applies."""
    srv = remote_server(auth_token="s3cret")
    client = _Client(srv.bound_port)

    resp = client.request({"op": "watch"})

    assert resp["ok"] is False
    assert resp["code"] == CODE_UNAUTHORIZED
    client.close()


def test_watch_does_not_block_other_clients_from_being_served(remote_server):
    srv = remote_server()
    watcher = _Client(srv.bound_port)
    assert watcher.request({"op": "watch"}) == {"ok": True}

    other = _Client(srv.bound_port)
    resp = other.request({"op": "list"})
    assert resp["ok"] is True

    watcher.close()
    other.close()
