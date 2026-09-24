"""Tests for ``mbserial``'s remote-transport branch (ticket 013) --
connecting to a peer-owned device by name.

Mirrors ``tests/deploy/test_deploy_cli_remote.py``'s own fixture split
(ticket 012): a *local* registry (a real ``RegistryAPIServer`` over a real
``AF_UNIX`` socket) seeded with one remote-owned (``host`` set) device,
and an *owning* registry (a real ``RemoteAPIServer`` over a real loopback
TCP socket, with ``FakeSerial`` injected as the port) that actually owns
that same uid. ``mbserial``'s own resolve step talks to the local
registry; the lock/stream talk directly to the owning one, per
sprint.md's Decision 8.
"""

from __future__ import annotations

import io
import shutil
import socket
import sys
import tempfile
import time

import pytest

from mbtools.common import EXIT_LOCKED, EXIT_NO_DAEMON, EXIT_OK
from mbtools.registry.api import RegistryAPIServer
from mbtools.registry.flash import FlashOp
from mbtools.registry.identity import ProbeResult
from mbtools.registry.locks import KIND_SERIAL, LockManager
from mbtools.registry.remote_api import RemoteAPIServer
from mbtools.registry.remote_client import RemoteRegistryClient
from mbtools.registry.store import Store
from mbtools.serial import cli as cli_mod
from mbtools.serial import connect as connect_mod
from mbtools.testing.fakes import FakeSerial

VID_PID = "0d28:0204"


def _uid(tag: str) -> str:
    unique = (tag * 4)[:16]
    return "9900" + "0000" + "11112222" + unique + "77778888" + "6e052820"


def _wait_until(predicate, timeout=2.0, interval=0.01) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(interval)
    assert predicate(), "condition never became true within timeout"


# ---------------------------------------------------------------------------
# local registry: the resolve target -- a device tagged host="loki"
# ---------------------------------------------------------------------------


@pytest.fixture
def local_socket_dir():
    d = tempfile.mkdtemp(prefix="mbserial-remote-cli-local-")
    yield d
    shutil.rmtree(d, ignore_errors=True)


@pytest.fixture
def local_store(tmp_path):
    s = Store(tmp_path / "local.db")
    yield s
    s.close()


@pytest.fixture
def local_locks():
    return LockManager()


@pytest.fixture
def local_server(local_socket_dir, local_store, local_locks):
    srv = RegistryAPIServer(
        socket_path=f"{local_socket_dir}/api.sock",
        store=local_store,
        locks=local_locks,
        flash_op=FlashOp(locks=local_locks, store=local_store),
        sweep_interval_s=100.0,
    )
    srv.start()
    yield srv
    srv.stop()


def _seed_remote_device(
    local_store: Store,
    uid: str,
    host: str,
    endpoint: str,
    *,
    device_name: str = "tovez",
) -> None:
    """Replicate a peer-owned device into ``local_store`` -- same helper
    shape as ``tests/deploy/test_deploy_cli_remote.py``'s own.
    """
    local_store.record_peer_seen(host, endpoint)
    local_store.upsert_remote_attached(uid, host, "/dev/ttyACM9", VID_PID)
    local_store.apply_remote_probe(
        uid,
        ProbeResult(
            role="NEZHA2",
            common_name="robot",
            device_name=device_name,
            serial="1",
            raw="raw",
        ),
    )


# ---------------------------------------------------------------------------
# owning registry: a real RemoteAPIServer over loopback TCP
# ---------------------------------------------------------------------------


@pytest.fixture
def owning_store(tmp_path):
    s = Store(tmp_path / "owning.db")
    yield s
    s.close()


@pytest.fixture
def owning_locks():
    return LockManager()


@pytest.fixture
def fake_serials():
    return []


@pytest.fixture
def remote_server(owning_store, owning_locks, fake_serials):
    servers: list[RemoteAPIServer] = []

    def _default_factory(**serial_kwargs):
        fs = FakeSerial(**serial_kwargs)
        fake_serials.append(fs)
        return fs

    def _make(*, serial_factory=None, break_duration_s=0.01):
        srv = RemoteAPIServer(
            host="127.0.0.1",
            port=0,
            store=owning_store,
            locks=owning_locks,
            sweep_interval_s=100.0,
            serial_factory=serial_factory if serial_factory is not None else _default_factory,
            stream_settle_s=0,
            break_duration_s=break_duration_s,
        )
        srv.start()
        servers.append(srv)
        return srv

    yield _make
    for srv in servers:
        srv.stop()


def _seed_owned_device(
    owning_store: Store,
    uid: str,
    *,
    port: str = "/dev/ttyACM9",
    device_name: str = "tovez",
) -> None:
    """Seed ``uid`` into ``owning_store`` as a locally-owned device --
    from the owning registry's own point of view, an ordinary local
    device.
    """
    owning_store.upsert_attached(uid, port, VID_PID)
    owning_store.apply_probe_result(
        uid,
        ProbeResult(
            role="NEZHA2",
            common_name="robot",
            device_name=device_name,
            serial="1",
            raw="raw",
        ),
    )


# ---------------------------------------------------------------------------
# locked-remote-device: the holder message names the host, not a null pid
# ---------------------------------------------------------------------------


@pytest.mark.requires_af_unix
def test_remote_device_locked_error_names_host_not_null_pid(
    local_server, local_store, remote_server, owning_store, capsys
):
    srv = remote_server()
    uid = _uid("lockedrs")
    _seed_owned_device(owning_store, uid)
    _seed_remote_device(local_store, uid, "loki", f"127.0.0.1:{srv.bound_port}")

    holder_client = RemoteRegistryClient("127.0.0.1", srv.bound_port)
    holder_client.connect()
    holder_client.lock(uid, KIND_SERIAL)
    try:
        start = time.monotonic()
        with pytest.raises(SystemExit) as excinfo:
            cli_mod.main(["tovez", "--socket", str(local_server.socket_path)])
        elapsed = time.monotonic() - start
    finally:
        holder_client.unlock(uid)
        holder_client.close()

    assert excinfo.value.code == EXIT_LOCKED
    err = capsys.readouterr().err
    assert "locked for serial" in err
    assert "127.0.0.1" in err
    assert "by pid" not in err
    assert elapsed < 2.0


# ---------------------------------------------------------------------------
# owning-host-unreachable: reported cleanly, not a stack trace
# ---------------------------------------------------------------------------


@pytest.mark.requires_af_unix
def test_owning_host_unreachable_at_connect_reports_cleanly(local_server, local_store, capsys):
    uid = _uid("unreachr")
    probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    probe.bind(("127.0.0.1", 0))
    port = probe.getsockname()[1]
    probe.close()  # nothing listens here

    _seed_remote_device(local_store, uid, "loki", f"127.0.0.1:{port}")

    with pytest.raises(SystemExit) as excinfo:
        cli_mod.main(["tovez", "--socket", str(local_server.socket_path)])

    assert excinfo.value.code == EXIT_NO_DAEMON
    err = capsys.readouterr().err
    assert "Traceback" not in err
    assert "loki" in err


# ---------------------------------------------------------------------------
# interactive/one-shot end to end against a real RemoteAPIServer + FakeSerial
# ---------------------------------------------------------------------------


@pytest.mark.requires_af_unix
def test_remote_interactive_session_end_to_end(
    local_server, local_store, remote_server, owning_store, owning_locks, monkeypatch, capsys
):
    srv = remote_server()
    uid = _uid("intractr")
    _seed_owned_device(owning_store, uid)
    _seed_remote_device(local_store, uid, "loki", f"127.0.0.1:{srv.bound_port}")

    monkeypatch.setattr(sys, "stdin", io.StringIO(""))  # immediate EOF
    monkeypatch.setattr(connect_mod, "EOF_DRAIN", 0.0)

    with pytest.raises(SystemExit) as excinfo:
        cli_mod.main(["tovez", "--socket", str(local_server.socket_path)])

    assert excinfo.value.code == EXIT_OK
    err = capsys.readouterr().err
    assert "connected to tovez" in err
    _wait_until(lambda: owning_locks.status(uid) is None)


@pytest.mark.requires_af_unix
def test_remote_one_shot_message_prints_reply_lines(
    local_server, local_store, remote_server, owning_store, owning_locks, capsys
):
    # announcement_after_writes=1 -- see
    # test_mbserial_remote_connect.py::test_send_command_reads_the_boards_reply
    # for why an unprompted announcement would race send_command()'s own
    # reset_input_buffer() call instead of exercising the reply path.
    def _factory(**kw):
        return FakeSerial(announcement="OK 42", announcement_after_writes=1, **kw)

    srv = remote_server(serial_factory=_factory)
    uid = _uid("oneshotr")
    _seed_owned_device(owning_store, uid)
    _seed_remote_device(local_store, uid, "loki", f"127.0.0.1:{srv.bound_port}")

    with pytest.raises(SystemExit) as excinfo:
        cli_mod.main(
            [
                "tovez",
                "SET",
                "SPEED",
                "50",
                "--socket",
                str(local_server.socket_path),
                "--timeout",
                "1.0",
            ]
        )

    assert excinfo.value.code == EXIT_OK
    out = capsys.readouterr().out
    assert "OK 42" in out
    _wait_until(lambda: owning_locks.status(uid) is None)


@pytest.mark.requires_af_unix
def test_remote_name_at_host_target_reaches_the_owning_host(
    local_server, local_store, remote_server, owning_store, owning_locks, capsys
):
    """``tovez@loki`` resolves locally, but the owning host holds the board
    as a local device (no ``host``), so the ``@loki`` suffix means nothing
    there -- the remote branch must ask for the resolved uid instead."""
    def _factory(**kw):
        return FakeSerial(announcement="OK 42", announcement_after_writes=1, **kw)

    srv = remote_server(serial_factory=_factory)
    uid = _uid("nameathost")
    _seed_owned_device(owning_store, uid)
    _seed_remote_device(local_store, uid, "loki", f"127.0.0.1:{srv.bound_port}")

    with pytest.raises(SystemExit) as excinfo:
        cli_mod.main(
            ["tovez@loki", "PING", "--socket", str(local_server.socket_path),
             "--timeout", "1.0"]
        )

    assert excinfo.value.code == EXIT_OK
    assert "OK 42" in capsys.readouterr().out
    _wait_until(lambda: owning_locks.status(uid) is None)


# ---------------------------------------------------------------------------
# --reset: sends a BREAK frame over the wire, resetting the owning port
# ---------------------------------------------------------------------------


@pytest.mark.requires_af_unix
def test_remote_reset_flag_sends_a_break_over_the_wire(
    local_server, local_store, remote_server, owning_store, owning_locks,
    fake_serials, monkeypatch,
):
    srv = remote_server()
    uid = _uid("resetrmt")
    _seed_owned_device(owning_store, uid)
    _seed_remote_device(local_store, uid, "loki", f"127.0.0.1:{srv.bound_port}")

    monkeypatch.setattr(sys, "stdin", io.StringIO(""))  # immediate EOF
    monkeypatch.setattr(connect_mod, "EOF_DRAIN", 0.0)

    with pytest.raises(SystemExit) as excinfo:
        cli_mod.main(
            ["tovez", "--reset", "--socket", str(local_server.socket_path)]
        )

    assert excinfo.value.code == EXIT_OK
    _wait_until(lambda: len(fake_serials) == 1)
    _wait_until(lambda: len(fake_serials[0].break_calls) == 1)
    _wait_until(lambda: owning_locks.status(uid) is None)
