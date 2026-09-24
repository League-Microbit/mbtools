"""Tests for mbtools.registry.api -- the Unix-socket query/control
protocol server and its SO_PEERCRED/LOCAL_PEERPID wiring (ticket 008).

Per sprint.md's Test Strategy ("protocol/dispatch tests run against a
real Unix socket in a tmp_path ... the SO_PEERCRED-specific assertion is
Linux-only and is skipped (not xfailed) on macOS, where the rest of the
protocol test still runs against the injectable is_pid_alive/
close-triggers-release paths"): every test here drives a real
``RegistryAPIServer`` over a real ``AF_UNIX`` socket in ``tmp_path``.
Only the one test asserting the real kernel-verified peer-pid mechanism
is platform-gated; everything else uses an injected ``peer_pid_fn`` so
it runs identically on Linux and macOS.
"""

from __future__ import annotations

import json
import os
import shutil
import socket
import sys
import tempfile
import time

import intelhex
import pytest

from mbtools.common import (
    CODE_INVALID_REQUEST,
    CODE_LOCKED,
    CODE_NOT_FOUND,
    CODE_NOT_LOCKED,
)
from mbtools.registry.api import RegistryAPIServer, default_peer_pid
from mbtools.registry.flash import FlashOp
from mbtools.registry.identity import ProbeResult
from mbtools.registry.locks import KIND_FLASH, KIND_SERIAL, HolderRef, LockManager
from mbtools.registry.store import Store

UID = "9900" + "0000" + "11112222" + "3333444455556666" + "77778888" + "6e052820"
UID2 = "aa11" + "0000" + "11112222" + "3333444455556666" + "77778888" + "6e052820"
# A peer-owned uid (ticket 010) -- never inserted by the `store` fixture
# itself, so it only exists in a test that explicitly calls
# `store.upsert_remote_attached`.
UID_REMOTE = "bb11" + "0000" + "11112222" + "3333444455556666" + "77778888" + "6e052820"
VID_PID = "0d28:0204"
PID_A = 1001
PID_B = 1002


def _local_holder(pid: int) -> HolderRef:
    """Mirrors api.py's own ``_local_holder`` construction (ticket 002)
    for tests that reach directly into ``LockManager`` to set up a
    pre-existing lock, bypassing the wire protocol."""
    return HolderRef(origin="local", ref=str(pid), pid=pid)


# ---------------------------------------------------------------------------
# test doubles / helpers
# ---------------------------------------------------------------------------


class _SpyRunner:
    """A fake pyOCD-subprocess runner -- relays scripted log lines in
    order, then returns a scripted exit code. Never shells out to a real
    pyocd binary. Mirrors tests/registry/flash/test_flash.py's own
    ``_SpyRunner``."""

    def __init__(self, lines=(), exit_code: int = 0):
        self._lines = list(lines)
        self._exit_code = exit_code
        self.calls: list[list[str]] = []

    def __call__(self, cmd, log) -> int:
        self.calls.append(cmd)
        for line in self._lines:
            log(line)
        return self._exit_code


def _valid_hex_path(tmp_path) -> str:
    ih = intelhex.IntelHex()
    ih[0x0000] = 0xFF
    path = tmp_path / "firmware.hex"
    ih.write_hex_file(str(path))
    return str(path)


def _sequential_peer_pid_fn(pids):
    """A ``peer_pid_fn`` that hands out ``pids`` in connection-accept
    order -- the test double for SO_PEERCRED/LOCAL_PEERPID: real client
    processes each get their own kernel-verified pid; this simulates
    several distinct "processes" from one test process's several
    sequential connections.
    """
    it = iter(pids)

    def fn(_sock: socket.socket) -> int:
        return next(it)

    return fn


class _Client:
    """A minimal newline-delimited-JSON client over a real AF_UNIX
    socket -- exercises the wire protocol exactly as a real client
    (ticket 009's CLI, sprint 002's mbdeploy/mbserial) would.
    """

    def __init__(self, socket_path):
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.connect(str(socket_path))
        self.rfile = self.sock.makefile("r", encoding="utf-8", newline="\n")
        self.wfile = self.sock.makefile("w", encoding="utf-8", newline="\n")

    def send(self, obj: dict) -> None:
        self.wfile.write(json.dumps(obj))
        self.wfile.write("\n")
        self.wfile.flush()

    def recv(self) -> dict:
        line = self.rfile.readline()
        if not line:
            raise ConnectionError("mbregistry api: connection closed")
        return json.loads(line)

    def request(self, obj: dict) -> dict:
        self.send(obj)
        return self.recv()

    def flash_request(self, obj: dict):
        """Send a ``flash`` request and collect every streamed log line
        up to (and including) the final ``{"type": "result", ...}``."""
        self.send(obj)
        logs = []
        while True:
            msg = self.recv()
            if msg.get("type") == "log":
                logs.append(msg["line"])
            else:
                return logs, msg

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
    s.upsert_attached(UID, "/dev/ttyACM0", VID_PID)
    s.upsert_attached(UID2, "/dev/ttyACM1", VID_PID)
    s.apply_probe_result(
        UID,
        ProbeResult(role="robot", common_name="Robot", device_name="alpha", serial="123", raw="DEVICE:alpha"),
    )
    yield s
    s.close()


@pytest.fixture
def locks():
    return LockManager()


@pytest.fixture
def socket_dir():
    """A short-path temp dir for the AF_UNIX socket file.

    pytest's own ``tmp_path`` nests under a long, per-test directory name
    that regularly exceeds ``sizeof(sockaddr_un.sun_path)`` (104 bytes on
    macOS, 108 on Linux) once combined with ``api.sock`` -- a real
    constraint any production deployment avoids by using the short
    ``/run/mbregistry/`` default, but tests need their own short path.
    """
    d = tempfile.mkdtemp(prefix="mbregistry-api-")
    yield d
    shutil.rmtree(d, ignore_errors=True)


@pytest.fixture
def make_server(socket_dir, store, locks):
    servers = []

    def _make(*, runner=None, peer_pid_fn=None, is_pid_alive_fn=None, sweep_interval_s=100.0):
        flash_op = FlashOp(locks=locks, store=store, runner=runner if runner is not None else _SpyRunner())
        srv = RegistryAPIServer(
            socket_path=f"{socket_dir}/api.sock",
            store=store,
            locks=locks,
            flash_op=flash_op,
            peer_pid_fn=peer_pid_fn,
            is_pid_alive_fn=is_pid_alive_fn,
            sweep_interval_s=sweep_interval_s,
        )
        srv.start()
        servers.append(srv)
        return srv

    yield _make
    for srv in servers:
        srv.stop()


# ---------------------------------------------------------------------------
# lifecycle -- socket creation
# ---------------------------------------------------------------------------


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX file mode bits only")
def test_start_makes_the_socket_connectable_by_non_owning_users(make_server):
    """Regression test for a bug ticket 010's real-hardware pass found:
    production's ``mbregistry.service`` runs as root (no ``User=`` --
    see ``cli.render_systemd_unit``), and binding an ``AF_UNIX`` socket
    under root's default umask produced mode ``0o755`` -- readable but
    not *writable*. Unix-domain ``connect()`` requires write permission
    on the socket inode, so plain ``mbregistry list`` run as an
    unprivileged user (exactly what this project's own acceptance
    criteria call for -- no ``sudo`` in sight) failed with
    ``PermissionError`` on every one of the four Nolanet nodes, until
    :meth:`RegistryAPIServer.start` was fixed to ``chmod`` the socket to
    ``0o666`` after binding. Asserts the permission bits directly rather
    than re-deriving "can a different uid connect", which isn't
    reproducible in a single-user test process.
    """
    srv = make_server()
    mode = os.stat(srv.socket_path).st_mode
    assert mode & 0o777 == 0o666


def test_stop_joins_connection_handler_threads_before_returning(make_server):
    """Regression test for a flaky ``IndexError`` in
    ``store._row_to_record`` traced to a shutdown-ordering race: ``stop()``
    used to join the accept and sweep threads but never the
    per-connection handler threads ``_accept_loop`` spawns, so a caller
    that closed ``store`` right after ``stop()`` returned (every
    production and test caller does exactly this) could race a
    still-finishing handler thread against the now-closed sqlite
    connection. A client that has already closed its connection before
    ``stop()`` is called must have its handler thread fully joined
    (not merely alive-and-winding-down) by the time ``stop()`` returns.
    """
    srv = make_server()
    client = _Client(srv.socket_path)
    resp = client.request({"op": "list"})
    assert resp["ok"] is True
    client.close()

    srv.stop()

    assert srv._conn_threads, "expected at least one connection-handler thread"
    assert all(not t.is_alive() for t in srv._conn_threads)


# ---------------------------------------------------------------------------
# list / get / find
# ---------------------------------------------------------------------------


def test_list_includes_every_device_with_lock_status_folded_in(make_server, locks):
    srv = make_server(peer_pid_fn=_sequential_peer_pid_fn([PID_A]))
    locks.acquire(UID, KIND_SERIAL, _local_holder(PID_A))
    client = _Client(srv.socket_path)

    resp = client.request({"op": "list"})

    assert resp["ok"] is True
    by_uid = {d["uid"]: d for d in resp["devices"]}
    assert set(by_uid) == {UID, UID2}
    assert by_uid[UID]["lock_kind"] == KIND_SERIAL
    assert by_uid[UID]["lock_pid"] == PID_A
    assert by_uid[UID2]["lock_kind"] is None
    assert by_uid[UID2]["lock_pid"] is None
    client.close()


def test_find_by_uid_short_uid_and_device_name(make_server):
    srv = make_server()
    client = _Client(srv.socket_path)
    short_uid = UID[16:24]

    for token in (UID, short_uid, "alpha", "ALPHA"):
        resp = client.request({"op": "find", "uid": token})
        assert resp["ok"] is True, token
        assert resp["device"]["uid"] == UID, token

    client.close()


def test_find_unknown_device_returns_not_found(make_server):
    srv = make_server()
    client = _Client(srv.socket_path)

    resp = client.request({"op": "find", "uid": "does-not-exist"})

    assert resp == {
        "ok": False,
        "code": CODE_NOT_FOUND,
        "error": "no such device: 'does-not-exist'",
    }
    client.close()


# ---------------------------------------------------------------------------
# HOST column feed, remote lock-state cache, peer endpoint (ticket 010)
# ---------------------------------------------------------------------------


def test_list_local_row_has_no_host_no_endpoint_and_reachable_true(make_server):
    srv = make_server()
    client = _Client(srv.socket_path)

    resp = client.request({"op": "list"})

    assert resp["ok"] is True
    by_uid = {d["uid"]: d for d in resp["devices"]}
    assert by_uid[UID]["host"] is None
    assert by_uid[UID]["endpoint"] is None
    assert by_uid[UID]["peer_reachable"] is True
    client.close()


def test_find_local_device_endpoint_is_absent_or_null(make_server):
    srv = make_server()
    client = _Client(srv.socket_path)

    resp = client.request({"op": "find", "uid": UID})

    assert resp["ok"] is True
    assert resp["device"].get("endpoint") is None
    client.close()


def test_find_remote_owned_device_includes_endpoint_from_peer_table(make_server, store):
    store.record_peer_seen("loki", "loki:8900")
    store.upsert_remote_attached(UID_REMOTE, "loki", "/dev/ttyACM9", VID_PID)
    srv = make_server()
    client = _Client(srv.socket_path)

    resp = client.request({"op": "find", "uid": UID_REMOTE})

    assert resp["ok"] is True
    assert resp["device"]["host"] == "loki"
    assert resp["device"]["endpoint"] == "loki:8900"
    assert resp["device"]["peer_reachable"] is True
    client.close()


def test_list_remote_owned_row_never_makes_a_live_lock_call(make_server, store):
    """Ticket 010 acceptance criterion: a remote-owned row's lock_kind/
    lock_pid come from no live LockManager call -- they stay None
    regardless of the store's cached remote_lock_kind/remote_lock_display,
    which land in their own, separate fields instead.
    """
    store.record_peer_seen("loki", "loki:8900")
    store.upsert_remote_attached(UID_REMOTE, "loki", "/dev/ttyACM9", VID_PID)
    store.apply_remote_lock_state(UID_REMOTE, KIND_SERIAL, "pid 555")
    srv = make_server()
    client = _Client(srv.socket_path)

    resp = client.request({"op": "list"})

    by_uid = {d["uid"]: d for d in resp["devices"]}
    remote = by_uid[UID_REMOTE]
    assert remote["lock_kind"] is None
    assert remote["lock_pid"] is None
    assert remote["remote_lock_kind"] == KIND_SERIAL
    assert remote["remote_lock_display"] == "pid 555"
    client.close()


def test_list_remote_owned_row_with_no_matching_peer_row_reports_unreachable(
    make_server, store
):
    # A `host` value with no matching `peer` row (shouldn't happen in
    # steady state, but a race could leave one transiently) is treated
    # as unreachable rather than assumed reachable -- reachability can't
    # be confirmed either way, so the conservative default wins.
    store.upsert_remote_attached(UID_REMOTE, "ghost-host", "/dev/ttyACM9", VID_PID)
    srv = make_server()
    client = _Client(srv.socket_path)

    resp = client.request({"op": "list"})

    by_uid = {d["uid"]: d for d in resp["devices"]}
    assert by_uid[UID_REMOTE]["peer_reachable"] is False
    assert by_uid[UID_REMOTE]["endpoint"] is None
    client.close()


def test_list_remote_owned_row_unreachable_when_peer_marked_unreachable(
    make_server, store
):
    store.record_peer_seen("loki", "loki:8900")
    store.mark_peer_unreachable("loki")
    store.upsert_remote_attached(UID_REMOTE, "loki", "/dev/ttyACM9", VID_PID)
    srv = make_server()
    client = _Client(srv.socket_path)

    resp = client.request({"op": "list"})

    by_uid = {d["uid"]: d for d in resp["devices"]}
    assert by_uid[UID_REMOTE]["peer_reachable"] is False
    assert by_uid[UID_REMOTE]["endpoint"] == "loki:8900"
    client.close()


# ---------------------------------------------------------------------------
# lock / unlock
# ---------------------------------------------------------------------------


def test_lock_happy_path(make_server, locks):
    srv = make_server(peer_pid_fn=_sequential_peer_pid_fn([PID_A]))
    client = _Client(srv.socket_path)

    resp = client.request({"op": "lock", "uid": UID, "kind": KIND_SERIAL})

    assert resp == {"ok": True}
    assert locks.status(UID).pid == PID_A
    assert locks.status(UID).kind == KIND_SERIAL
    client.close()


def test_lock_already_locked_returns_holder_kind_and_pid(make_server):
    srv = make_server(peer_pid_fn=_sequential_peer_pid_fn([PID_A, PID_B]))
    holder = _Client(srv.socket_path)
    assert holder.request({"op": "lock", "uid": UID, "kind": KIND_FLASH})["ok"] is True

    contender = _Client(srv.socket_path)
    resp = contender.request({"op": "lock", "uid": UID, "kind": KIND_SERIAL})

    assert resp["ok"] is False
    assert resp["code"] == CODE_LOCKED
    assert resp["holder"] == {"kind": KIND_FLASH, "pid": PID_A}
    holder.close()
    contender.close()


def test_lock_unknown_device_is_not_found(make_server):
    srv = make_server()
    client = _Client(srv.socket_path)

    resp = client.request({"op": "lock", "uid": "nope", "kind": KIND_SERIAL})

    assert resp["ok"] is False
    assert resp["code"] == CODE_NOT_FOUND
    client.close()


def test_lock_unknown_kind_is_invalid_request(make_server):
    srv = make_server()
    client = _Client(srv.socket_path)

    resp = client.request({"op": "lock", "uid": UID, "kind": "nonsense"})

    assert resp["ok"] is False
    assert resp["code"] == CODE_INVALID_REQUEST
    client.close()


def test_unlock_releases_own_lock(make_server, locks):
    srv = make_server(peer_pid_fn=_sequential_peer_pid_fn([PID_A]))
    client = _Client(srv.socket_path)
    client.request({"op": "lock", "uid": UID, "kind": KIND_SERIAL})

    resp = client.request({"op": "unlock", "uid": UID})

    assert resp == {"ok": True, "released": True}
    assert locks.status(UID) is None
    client.close()


def test_unlock_by_a_different_connection_cannot_steal_release(make_server, locks):
    srv = make_server(peer_pid_fn=_sequential_peer_pid_fn([PID_A, PID_B]))
    owner = _Client(srv.socket_path)
    owner.request({"op": "lock", "uid": UID, "kind": KIND_SERIAL})

    other = _Client(srv.socket_path)
    resp = other.request({"op": "unlock", "uid": UID})

    # Not an error -- a no-op, per LockManager.release's own contract.
    assert resp == {"ok": True, "released": False}
    assert locks.status(UID).pid == PID_A
    owner.close()
    other.close()


# ---------------------------------------------------------------------------
# connection close releases exactly that connection's locks
# ---------------------------------------------------------------------------


def test_connection_close_releases_only_that_connections_locks(make_server, locks):
    srv = make_server(peer_pid_fn=_sequential_peer_pid_fn([PID_A, PID_B]))
    conn_a = _Client(srv.socket_path)
    conn_a.request({"op": "lock", "uid": UID, "kind": KIND_SERIAL})
    conn_b = _Client(srv.socket_path)
    conn_b.request({"op": "lock", "uid": UID2, "kind": KIND_SERIAL})

    conn_a.close()  # no explicit unlock

    _wait_until(lambda: locks.status(UID) is None)
    # conn_b's own lock is untouched by conn_a's close.
    assert locks.status(UID2) is not None
    assert locks.status(UID2).pid == PID_B
    conn_b.close()


# ---------------------------------------------------------------------------
# flash
# ---------------------------------------------------------------------------


def test_flash_without_flash_lock_is_refused(make_server, tmp_path):
    srv = make_server(peer_pid_fn=_sequential_peer_pid_fn([PID_A]))
    client = _Client(srv.socket_path)
    hex_path = _valid_hex_path(tmp_path)

    logs, result = client.flash_request({"op": "flash", "uid": UID, "hex_path": hex_path})

    assert logs == []
    assert result["type"] == "result"
    assert result["ok"] is False
    assert result["code"] == CODE_NOT_LOCKED
    client.close()


def test_flash_requires_the_lock_be_held_by_this_connections_own_pid(make_server, locks, tmp_path):
    srv = make_server(peer_pid_fn=_sequential_peer_pid_fn([PID_B]))
    locks.acquire(UID, KIND_FLASH, _local_holder(PID_A))  # some other connection holds it
    client = _Client(srv.socket_path)  # gets PID_B, not PID_A
    hex_path = _valid_hex_path(tmp_path)

    logs, result = client.flash_request({"op": "flash", "uid": UID, "hex_path": hex_path})

    assert result["ok"] is False
    assert result["code"] == CODE_NOT_LOCKED
    assert locks.status(UID).pid == PID_A  # untouched
    client.close()


def test_flash_streams_log_lines_in_order_and_releases_lock_on_completion(make_server, locks, tmp_path):
    lines = ["erasing...", "programming...", "verifying..."]
    srv = make_server(peer_pid_fn=_sequential_peer_pid_fn([PID_A]), runner=_SpyRunner(lines=lines, exit_code=0))
    client = _Client(srv.socket_path)
    client.request({"op": "lock", "uid": UID, "kind": KIND_FLASH})
    hex_path = _valid_hex_path(tmp_path)

    logs, result = client.flash_request({"op": "flash", "uid": UID, "hex_path": hex_path})

    assert logs == lines  # relayed in order, as they arrived
    assert result == {
        "type": "result",
        "ok": True,
        "success": True,
        "exit_code": 0,
        "error": None,
    }
    # The lock was released as part of the flash op completing -- this is
    # daemon's re-probe trigger; flash_hex itself never releases it (see
    # flash.py), so the api layer must.
    assert locks.status(UID) is None
    client.close()


def test_failed_flash_still_releases_lock(make_server, locks, tmp_path):
    srv = make_server(peer_pid_fn=_sequential_peer_pid_fn([PID_A]), runner=_SpyRunner(exit_code=1))
    client = _Client(srv.socket_path)
    client.request({"op": "lock", "uid": UID, "kind": KIND_FLASH})
    hex_path = _valid_hex_path(tmp_path)

    _logs, result = client.flash_request({"op": "flash", "uid": UID, "hex_path": hex_path})

    assert result["ok"] is False
    assert result["success"] is False
    assert result["exit_code"] == 1
    assert locks.status(UID) is None
    client.close()


def test_flash_hex_validation_error_is_reported_and_releases_lock(make_server, locks, tmp_path):
    srv = make_server(peer_pid_fn=_sequential_peer_pid_fn([PID_A]))
    client = _Client(srv.socket_path)
    client.request({"op": "lock", "uid": UID, "kind": KIND_FLASH})
    missing_hex = str(tmp_path / "does-not-exist.hex")

    logs, result = client.flash_request({"op": "flash", "uid": UID, "hex_path": missing_hex})

    assert logs == []
    assert result["ok"] is False
    assert result["code"] == CODE_INVALID_REQUEST
    assert locks.status(UID) is None
    client.close()


def test_flash_release_triggers_flash_release_callback(tmp_path, socket_dir, store):
    fired = []
    locks = LockManager(flash_release_callback=fired.append)
    flash_op = FlashOp(locks=locks, store=store, runner=_SpyRunner(exit_code=0))
    srv = RegistryAPIServer(
        socket_path=f"{socket_dir}/api.sock",
        store=store,
        locks=locks,
        flash_op=flash_op,
        peer_pid_fn=_sequential_peer_pid_fn([PID_A]),
        sweep_interval_s=100.0,
    )
    srv.start()
    client = _Client(srv.socket_path)
    client.request({"op": "lock", "uid": UID, "kind": KIND_FLASH})
    hex_path = _valid_hex_path(tmp_path)

    client.flash_request({"op": "flash", "uid": UID, "hex_path": hex_path})

    assert fired == [UID]
    client.close()
    srv.stop()


# ---------------------------------------------------------------------------
# mark_flashed
# ---------------------------------------------------------------------------


def test_mark_flashed_without_flash_lock_is_refused(make_server, store):
    srv = make_server(peer_pid_fn=_sequential_peer_pid_fn([PID_A]))
    client = _Client(srv.socket_path)

    resp = client.request({"op": "mark_flashed", "uid": UID})

    assert resp["ok"] is False
    assert resp["code"] == CODE_NOT_LOCKED
    assert store.find(UID).flash_count == 0
    client.close()


def test_mark_flashed_held_by_a_different_connection_is_refused(make_server, locks, store):
    srv = make_server(peer_pid_fn=_sequential_peer_pid_fn([PID_B]))
    locks.acquire(UID, KIND_FLASH, _local_holder(PID_A))  # some other connection holds it
    client = _Client(srv.socket_path)  # gets PID_B, not PID_A

    resp = client.request({"op": "mark_flashed", "uid": UID})

    assert resp["ok"] is False
    assert resp["code"] == CODE_NOT_LOCKED
    assert locks.status(UID).pid == PID_A  # untouched
    assert store.find(UID).flash_count == 0
    client.close()


def test_mark_flashed_increments_flash_count_and_does_not_touch_the_lock(
    make_server, locks, store
):
    srv = make_server(peer_pid_fn=_sequential_peer_pid_fn([PID_A]))
    client = _Client(srv.socket_path)
    client.request({"op": "lock", "uid": UID, "kind": KIND_FLASH})

    resp = client.request({"op": "mark_flashed", "uid": UID})

    assert resp == {"ok": True}
    assert store.find(UID).flash_count == 1
    # the lock is untouched -- mark_flashed does not release it, unlike flash.
    assert locks.status(UID).kind == KIND_FLASH
    assert locks.status(UID).pid == PID_A
    client.close()


def test_mark_flashed_does_not_invoke_pyocd(make_server):
    runner = _SpyRunner()
    srv = make_server(peer_pid_fn=_sequential_peer_pid_fn([PID_A]), runner=runner)
    client = _Client(srv.socket_path)
    client.request({"op": "lock", "uid": UID, "kind": KIND_FLASH})

    client.request({"op": "mark_flashed", "uid": UID})

    assert runner.calls == []
    client.close()


def test_mark_flashed_does_not_trigger_flash_release_callback(tmp_path, socket_dir, store):
    fired = []
    locks = LockManager(flash_release_callback=fired.append)
    flash_op = FlashOp(locks=locks, store=store, runner=_SpyRunner(exit_code=0))
    srv = RegistryAPIServer(
        socket_path=f"{socket_dir}/api.sock",
        store=store,
        locks=locks,
        flash_op=flash_op,
        peer_pid_fn=_sequential_peer_pid_fn([PID_A]),
        sweep_interval_s=100.0,
    )
    srv.start()
    client = _Client(srv.socket_path)
    client.request({"op": "lock", "uid": UID, "kind": KIND_FLASH})

    client.request({"op": "mark_flashed", "uid": UID})

    # mark_flashed never releases the lock, so the re-probe hook -- which
    # fires on any flash-kind release, per LockManager's own contract --
    # does not fire either.
    assert fired == []
    client.close()
    srv.stop()


def test_mark_flashed_unknown_uid_returns_not_found(make_server):
    srv = make_server(peer_pid_fn=_sequential_peer_pid_fn([PID_A]))
    client = _Client(srv.socket_path)

    resp = client.request({"op": "mark_flashed", "uid": "does-not-exist"})

    assert resp["ok"] is False
    assert resp["code"] == CODE_NOT_FOUND
    client.close()


# ---------------------------------------------------------------------------
# malformed requests
# ---------------------------------------------------------------------------


def test_malformed_json_returns_invalid_request(make_server):
    srv = make_server()
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    sock.connect(str(srv.socket_path))
    sock.sendall(b"not json at all\n")
    rfile = sock.makefile("r", encoding="utf-8")
    resp = json.loads(rfile.readline())

    assert resp["ok"] is False
    assert resp["code"] == CODE_INVALID_REQUEST
    sock.close()


def test_unknown_op_returns_invalid_request(make_server):
    srv = make_server()
    client = _Client(srv.socket_path)

    resp = client.request({"op": "reboot"})

    assert resp["ok"] is False
    assert resp["code"] == CODE_INVALID_REQUEST
    client.close()


# ---------------------------------------------------------------------------
# periodic liveness sweep (wired here, per ticket 008 -- not by daemon)
# ---------------------------------------------------------------------------


def test_periodic_sweep_releases_lock_of_a_dead_holder_without_closing_the_connection(make_server, locks):
    dead_pids = {PID_A}
    srv = make_server(
        peer_pid_fn=_sequential_peer_pid_fn([PID_A]),
        is_pid_alive_fn=lambda pid: pid not in dead_pids,
        sweep_interval_s=0.05,
    )
    client = _Client(srv.socket_path)
    client.request({"op": "lock", "uid": UID, "kind": KIND_SERIAL})
    assert locks.status(UID) is not None

    # The connection stays open the whole time -- only the periodic sweep,
    # not a connection close, should release this lock.
    _wait_until(lambda: locks.status(UID) is None, timeout=2.0)
    client.close()


# ---------------------------------------------------------------------------
# SO_PEERCRED / LOCAL_PEERPID -- real kernel-verified peer pid
# ---------------------------------------------------------------------------


@pytest.mark.skipif(sys.platform != "linux", reason="SO_PEERCRED is Linux-only")
def test_default_peer_pid_uses_so_peercred_on_linux(make_server, locks):
    srv = make_server(peer_pid_fn=None)  # real default_peer_pid
    client = _Client(srv.socket_path)

    resp = client.request({"op": "lock", "uid": UID, "kind": KIND_SERIAL})

    assert resp == {"ok": True}
    # The peer pid the kernel reported is this very test process's pid --
    # never a value the client could have supplied itself.
    assert locks.status(UID).pid == os.getpid()
    client.close()


@pytest.mark.skipif(sys.platform != "darwin", reason="LOCAL_PEERPID is macOS-only")
def test_default_peer_pid_uses_local_peerpid_on_darwin(make_server, locks):
    srv = make_server(peer_pid_fn=None)  # real default_peer_pid
    client = _Client(srv.socket_path)

    resp = client.request({"op": "lock", "uid": UID, "kind": KIND_SERIAL})

    assert resp == {"ok": True}
    assert locks.status(UID).pid == os.getpid()
    client.close()


def test_default_peer_pid_raises_on_unsupported_platform(monkeypatch):
    import mbtools.registry.api as api_module

    monkeypatch.setattr(api_module.sys, "platform", "win32")
    dummy = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        with pytest.raises(OSError):
            default_peer_pid(dummy)
    finally:
        dummy.close()
