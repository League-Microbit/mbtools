"""Tests for ticket 009's own additions to ``registry.cli``: assembling
``registry.peering``/``registry.remote_api`` alongside the existing
``daemon``/``api`` pairing (:func:`mbtools.registry.cli.assemble_registry`),
the new ``mbregistry run`` flags (``--peer``, ``--remote-port``,
``--peer-pub-port``, ``--peer-snapshot-port``, ``--auth-token``) and their
flag > env var > default precedence, and an in-process two-``mbregistry``
convergence test exercising the actual ``--peer`` code path in
``cmd_run``.

Per this ticket's own Testing note, mDNS itself is kept out of every test
here (a fake ``zeroconf`` module, mirroring ``tests/registry/peering``'s
own convention) -- real UDP multicast in a sandboxed test runner is not
something this suite wants to depend on. The ZeroMQ half (peering's PUB/
REP event bus) and the TCP remote API both use real loopback sockets,
matching ``tests/registry/peering/test_peering_eventbus.py``'s own
"only meaningfully proven against real sockets" reasoning.
"""

from __future__ import annotations

import os
import shutil
import tempfile
import threading
import time

import pytest

from mbtools.common import DAPLINK_VID_PID, EXIT_OK, PortInfo
from mbtools.registry.cli import (
    DEFAULT_PUB_PORT,
    DEFAULT_REMOTE_PORT,
    DEFAULT_SNAPSHOT_PORT,
    _parse_peer_spec,
    _resolve_int,
    _resolve_token,
    assemble_registry,
    build_parser,
    main,
)
from mbtools.registry.store import STATE_CONNECTED, Store
from mbtools.testing.fakes import FakeSerial, FakeUSBSource

VID, PID_ = DAPLINK_VID_PID


def _uid(tag: str) -> str:
    unique = (tag * 4)[:16]
    return "9900" + "0000" + "11112222" + unique + "77778888" + "6e052820"


def _port_info(uid: str, port: str = "/dev/ttyACM0") -> PortInfo:
    return PortInfo(uid=uid, port=port, vid=VID, pid=PID_)


def _wait_until(predicate, timeout=8.0, interval=0.02):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(interval)
    assert predicate(), "condition never became true within timeout"


# ---------------------------------------------------------------------------
# fake zeroconf -- no real mDNS socket opened (mirrors
# tests/registry/peering/test_peering_eventbus.py's own fake of the same
# shape, duplicated here per this project's "one fixture set per test
# module" convention).
# ---------------------------------------------------------------------------


class _FakeServiceInfo:
    def __init__(self, type_, name, *, addresses, port, properties, server):
        self.type_ = type_
        self.name = name
        self.addresses = addresses
        self.port = port
        self.properties = properties
        self.server = server


class _FakeZeroconf:
    def register_service(self, info, allow_name_change=False):
        pass

    def unregister_service(self, info):
        pass

    def close(self):
        pass


class _FakeServiceBrowser:
    def __init__(self, zc, type_, listener=None):
        pass

    def cancel(self):
        pass


class _FakeZeroconfNamespace:
    ServiceInfo = staticmethod(_FakeServiceInfo)
    ServiceBrowser = staticmethod(_FakeServiceBrowser)

    def __init__(self):
        self.instance = _FakeZeroconf()

    def Zeroconf(self):
        return self.instance


@pytest.fixture
def socket_dir():
    d = tempfile.mkdtemp(prefix="mbregistry-cli-run-peering-")
    yield d
    shutil.rmtree(d, ignore_errors=True)


# ---------------------------------------------------------------------------
# pure unit tests: --peer spec parsing, flag > env var > default
# precedence for the new port/token flags -- the same style
# tests/registry/cli/test_cli_list.py's own --socket precedence tests use.
# ---------------------------------------------------------------------------


def test_parse_peer_spec_with_port():
    assert _parse_peer_spec("loki:7440") == ("loki", 7440)


def test_parse_peer_spec_without_port_defaults_to_default_remote_port():
    assert _parse_peer_spec("loki") == ("loki", DEFAULT_REMOTE_PORT)


def test_parse_peer_spec_rejects_empty_host():
    with pytest.raises(ValueError):
        _parse_peer_spec(":7440")


def test_parse_peer_spec_rejects_non_numeric_port():
    with pytest.raises(ValueError):
        _parse_peer_spec("loki:notaport")


def test_resolve_int_flag_beats_env_beats_default(monkeypatch):
    monkeypatch.setenv("MBREGISTRY_REMOTE_PORT", "9999")
    assert _resolve_int(1234, "MBREGISTRY_REMOTE_PORT", 7440) == 1234


def test_resolve_int_env_beats_default(monkeypatch):
    monkeypatch.setenv("MBREGISTRY_REMOTE_PORT", "9999")
    assert _resolve_int(None, "MBREGISTRY_REMOTE_PORT", 7440) == 9999


def test_resolve_int_default_when_neither_given(monkeypatch):
    monkeypatch.delenv("MBREGISTRY_REMOTE_PORT", raising=False)
    assert _resolve_int(None, "MBREGISTRY_REMOTE_PORT", 7440) == 7440


def test_resolve_token_flag_beats_env(monkeypatch):
    monkeypatch.setenv("MBREGISTRY_TOKEN", "from-env")
    assert _resolve_token("from-flag", "MBREGISTRY_TOKEN") == "from-flag"


def test_resolve_token_env_when_no_flag(monkeypatch):
    monkeypatch.setenv("MBREGISTRY_TOKEN", "from-env")
    assert _resolve_token(None, "MBREGISTRY_TOKEN") == "from-env"


def test_resolve_token_none_when_neither_given(monkeypatch):
    monkeypatch.delenv("MBREGISTRY_TOKEN", raising=False)
    assert _resolve_token(None, "MBREGISTRY_TOKEN") is None


def test_build_parser_run_accepts_all_new_flags():
    parser = build_parser()
    args = parser.parse_args(
        [
            "run",
            "--peer",
            "loki:7440",
            "--peer",
            "magni",
            "--remote-port",
            "8000",
            "--peer-pub-port",
            "8002",
            "--peer-snapshot-port",
            "8003",
            "--auth-token",
            "s3cret",
        ]
    )
    assert args.peer == ["loki:7440", "magni"]
    assert args.remote_port == 8000
    assert args.peer_pub_port == 8002
    assert args.peer_snapshot_port == 8003
    assert args.auth_token == "s3cret"


def test_build_parser_run_new_flags_default_to_none():
    parser = build_parser()
    args = parser.parse_args(["run"])
    assert args.peer is None
    assert args.remote_port is None
    assert args.peer_pub_port is None
    assert args.peer_snapshot_port is None
    assert args.auth_token is None


# ---------------------------------------------------------------------------
# assemble_registry: shared lock spans all four objects, remote_api/
# peering actually start and stop cleanly.
# ---------------------------------------------------------------------------


def test_assemble_registry_shares_one_lock_across_all_four(tmp_path, socket_dir):
    uid = _uid("shared11")
    store = Store(tmp_path / "devices.db")
    usbwatch = FakeUSBSource([[_port_info(uid)]])

    daemon, api, remote_api, peering = assemble_registry(
        store=store,
        usbwatch=usbwatch,
        socket_path=f"{socket_dir}/api.sock",
        remote_port=0,
        peer_pub_port=17702,
        peer_snapshot_port=17703,
        zeroconf=_FakeZeroconfNamespace(),
    )
    try:
        # Daemon/api/remote_api all reference the exact same RLock
        # instance -- the acceptance criterion this ticket adds on top
        # of ticket 008's daemon/api-only sharing.
        assert daemon._lock is api._lock
        assert daemon._lock is remote_api._lock
        assert daemon._lock is peering._lock

        api.start()
        remote_api.start()
        peering.start()
        assert remote_api.bound_port != 0
    finally:
        peering.stop()
        remote_api.stop()
        api.stop()
        store.close()


def test_assemble_registry_lock_display_callback_reaches_peering(tmp_path, socket_dir):
    """A lock acquired through ``daemon.locks`` (the exact instance
    ``assemble_registry`` hands to every server) publishes onto
    ``peering``'s own event bus -- proves ``Daemon``'s new
    ``lock_display_callback`` forwarding (this ticket's own extension of
    ``daemon.py``) is actually wired end to end by ``assemble_registry``,
    not just accepted and dropped.
    """
    import zmq

    from mbtools.registry.locks import KIND_SERIAL, HolderRef

    uid = _uid("lockdisp")
    store = Store(tmp_path / "devices.db")
    usbwatch = FakeUSBSource([[_port_info(uid)]])

    daemon, api, remote_api, peering = assemble_registry(
        store=store,
        usbwatch=usbwatch,
        socket_path=f"{socket_dir}/api.sock",
        remote_port=0,
        peer_pub_port=17712,
        peer_snapshot_port=17713,
        zeroconf=_FakeZeroconfNamespace(),
    )

    ctx = zmq.Context()
    sub = ctx.socket(zmq.SUB)
    sub.setsockopt(zmq.SUBSCRIBE, b"")
    sub.setsockopt(zmq.RCVTIMEO, 3000)

    try:
        peering.start()
        sub.connect("tcp://127.0.0.1:17712")
        time.sleep(0.2)  # let the SUB subscription propagate

        daemon.locks.acquire(uid, KIND_SERIAL, HolderRef(origin="local", ref="1234", pid=1234))

        message = sub.recv()
        assert b"lock_state" in message
        assert uid.encode() in message
    finally:
        sub.close(linger=0)
        ctx.term()
        peering.stop()
        store.close()


# ---------------------------------------------------------------------------
# end-to-end: two in-process mbregistry pipelines, connected the same way
# cmd_run's own --peer handling connects them, converge to a shared view.
# This is ticket 009's own "mbregistry run --peer HOST:PORT ... converges
# to a shared device view" acceptance test, at the assemble_registry
# level (one layer below full argv parsing) so mDNS stays fake -- see
# module docstring.
# ---------------------------------------------------------------------------


def test_two_pipelines_connected_via_peer_flag_equivalent_converge(tmp_path, socket_dir, capsys):
    uid_a = _uid("peera111")
    uid_b = _uid("peerb222")

    store_a = Store(tmp_path / "a.db")
    store_b = Store(tmp_path / "b.db")

    daemon_a, api_a, remote_a, peering_a = assemble_registry(
        store=store_a,
        usbwatch=FakeUSBSource([[_port_info(uid_a)]]),
        socket_path=f"{socket_dir}/a.sock",
        serial_factory=lambda **kw: FakeSerial(
            announcement="device NEZHA2 robot alpha1 111", **kw
        ),
        settle_s=0,
        probe_timeout_s=0.05,
        remote_port=17720,
        peer_pub_port=17722,
        peer_snapshot_port=17723,
        zeroconf=_FakeZeroconfNamespace(),
    )
    daemon_b, api_b, remote_b, peering_b = assemble_registry(
        store=store_b,
        usbwatch=FakeUSBSource([[_port_info(uid_b)]]),
        socket_path=f"{socket_dir}/b.sock",
        serial_factory=lambda **kw: FakeSerial(
            announcement="device NEZHA2 robot beta1 222", **kw
        ),
        settle_s=0,
        probe_timeout_s=0.05,
        remote_port=17730,
        peer_pub_port=17732,
        peer_snapshot_port=17733,
        zeroconf=_FakeZeroconfNamespace(),
    )

    stop_a = threading.Event()
    stop_b = threading.Event()
    thread_a = threading.Thread(
        target=daemon_a.run, kwargs={"interval_s": 0.01, "stop": stop_a.is_set}
    )
    thread_b = threading.Thread(
        target=daemon_b.run, kwargs={"interval_s": 0.01, "stop": stop_b.is_set}
    )

    try:
        api_a.start()
        remote_a.start()
        peering_a.start()
        api_b.start()
        remote_b.start()
        peering_b.start()
        thread_a.start()
        thread_b.start()

        _wait_until(
            lambda: (rec := store_a.get(uid_a)) is not None and rec.state == STATE_CONNECTED
        )
        _wait_until(
            lambda: (rec := store_b.get(uid_b)) is not None and rec.state == STATE_CONNECTED
        )

        # This is exactly what cmd_run's own --peer loop does: connect_peer
        # with remote_port given, so the peer is recorded before the link
        # connects (ticket 009's own fix -- see
        # tests/registry/peering/test_peering_eventbus.py's dedicated
        # coverage of connect_peer's remote_port behavior).
        peering_b.connect_peer("host-a", "127.0.0.1", 17722, 17723, remote_port=17720)

        _wait_until(lambda: store_b.get(uid_a) is not None)
        assert store_b.get(uid_a).host == "host-a"
        assert store_b.get_peer("host-a").reachable is True

        # mbregistry list on host b (a client of its local api socket,
        # per spec section 3.5) shows both hosts' devices, HOST-tagged --
        # sprint.md's SUC-001 postcondition.
        import json as _json

        with pytest.raises(SystemExit) as excinfo:
            main(["list", "--socket", f"{socket_dir}/b.sock", "--json"])
        assert excinfo.value.code == EXIT_OK
        listed = _json.loads(capsys.readouterr().out)
        uids_listed = {d["uid"] for d in listed["devices"]}
        assert uid_a in uids_listed
        assert uid_b in uids_listed
    finally:
        stop_a.set()
        stop_b.set()
        thread_a.join(timeout=5.0)
        thread_b.join(timeout=5.0)
        assert not thread_a.is_alive()
        assert not thread_b.is_alive()
        peering_a.stop()
        peering_b.stop()
        remote_a.stop()
        remote_b.stop()
        api_a.stop()
        api_b.stop()
        store_a.close()
        store_b.close()


def test_assemble_registry_name_set_callback_reaches_peering(tmp_path, socket_dir):
    """Same proof as ``test_assemble_registry_lock_display_callback_
    reaches_peering`` above, for sprint 004 ticket 005's
    ``name_set_callback``/``name_clear_callback`` wiring: a
    ``names_set`` op through ``api``'s own dispatch (``BaseAPIServer.
    _op_names_set``) publishes onto ``peering``'s event bus -- proves
    ``assemble_registry`` actually wires ``PeerDiscovery.publish_name_set``
    into ``RegistryAPIServer``, not just accepts and drops it. Ticket
    002 left this callback unwired to any call site; this ticket's own
    ``registry.cli`` assembly (module docstring's "name-registry
    replication wiring" note) is where it lands.
    """
    import zmq

    from mbtools.registry.store import Store as _Store

    store = _Store(tmp_path / "devices.db")
    usbwatch = FakeUSBSource([[]])

    daemon, api, remote_api, peering = assemble_registry(
        store=store,
        usbwatch=usbwatch,
        socket_path=f"{socket_dir}/api.sock",
        remote_port=0,
        peer_pub_port=17714,
        peer_snapshot_port=17715,
        zeroconf=_FakeZeroconfNamespace(),
    )

    ctx = zmq.Context()
    sub = ctx.socket(zmq.SUB)
    sub.setsockopt(zmq.SUBSCRIBE, b"")
    sub.setsockopt(zmq.RCVTIMEO, 3000)

    try:
        peering.start()
        sub.connect("tcp://127.0.0.1:17714")
        time.sleep(0.2)  # let the SUB subscription propagate

        resp = api._op_names_set({"name": "tovez", "channel": 20, "group": 30})
        assert resp["ok"] is True

        message = sub.recv()
        assert b"name_set" in message
        assert b"tovez" in message
    finally:
        sub.close(linger=0)
        ctx.term()
        peering.stop()
        store.close()
