"""Tests for ``mbdeploy deploy``'s remote-transport branch (ticket 012) --
flashing a peer-owned device by name.

Per ticket 012's own Testing section, hardware-level end-to-end coverage
is ticket 014's job, not this one -- every test here drives
``mbtools.deploy.cli.main([...])`` against a *local* registry (a real
``RegistryAPIServer`` over a real ``AF_UNIX`` socket, exactly like
``tests/deploy/test_deploy_cli.py``'s own "lightweight fixture") seeded
with one remote-owned (``host`` set) device, and an *owning* registry (a
real ``RemoteAPIServer`` over a real loopback TCP socket, exactly like
``tests/registry/remote_client/test_remote_client.py``'s own fixture)
that actually owns that same uid. ``mbdeploy``'s own resolve step talks
to the local registry; everything from there on -- lock, flash,
wait-for-reprobe -- talks directly to the owning one, per sprint.md's
Decision 8.

Two fixture styles, mirroring ``test_deploy_cli.py``'s own split:

- *Lightweight* owning side (``remote_server`` fixture): a real
  ``RemoteAPIServer`` with devices seeded straight into ``Store``/
  ``LockManager``, no ``Daemon`` poll loop running. Used for every flow
  that doesn't need a genuine post-unlock re-probe to land (relay guard,
  already-locked, unreachable-owning-host, ordinary/blank-board flash
  failure, the reprobe-timeout case itself).
- *Daemon-backed* owning side (``_run_owning_daemon_and_remote_api``): a
  real ``Daemon`` + ``RemoteAPIServer`` pair sharing one lock, driven by
  a background thread against a ``FakeUSBSource``/scripted ``FakeSerial``
  factory -- so the real flash-triggered re-probe hook actually fires.
  Used only for the one end-to-end success flow, where the ticket's own
  acceptance criteria require seeing a *real* new announcement land.
"""

from __future__ import annotations

import shutil
import socket
import subprocess
import tempfile
import threading
import time
from collections import deque
from pathlib import Path

import pytest

from mbtools.common import (
    DAPLINK_VID_PID,
    EXIT_ERROR,
    EXIT_HARDWARE,
    EXIT_LOCKED,
    EXIT_NO_DAEMON,
    EXIT_OK,
    PortInfo,
)
from mbtools.deploy import cli as cli_mod
from mbtools.registry.api import RegistryAPIServer
from mbtools.registry.daemon import Daemon
from mbtools.registry.flash import FlashOp
from mbtools.registry.identity import ProbeResult
from mbtools.registry.locks import KIND_FLASH, LockManager
from mbtools.registry.remote_api import RemoteAPIServer
from mbtools.registry.remote_client import RegistryUnavailable, RemoteRegistryClient
from mbtools.registry.store import Store
from mbtools.testing.fakes import (
    FakeSerial,
    FakeUSBSource,
    unavailable_chip_identity_session_factory,
)

VID_PID = "0d28:0204"
VID, PID_ = DAPLINK_VID_PID
ANNOUNCEMENT = "device NEZHA2 robot tovez 1198504156"

#: A minimal, complete, valid Intel HEX file -- just the EOF record, same
#: content tests/registry/remote_api/test_remote_flash.py and
#: tests/registry/remote_client/test_remote_client.py both use.
_VALID_HEX_CONTENT = ":00000001FF\n"


def _uid(tag: str) -> str:
    unique = (tag * 4)[:16]
    return "9900" + "0000" + "11112222" + unique + "77778888" + "6e052820"


def _port_info(uid: str, port: str = "/dev/ttyACM0") -> PortInfo:
    return PortInfo(uid=uid, port=port, vid=VID, pid=PID_)


class _FakeProcess:
    """Stand-in for a ``subprocess.Popen`` instance -- mirrors
    ``test_remote_client.py``'s own fake: ``flashlogic._run_streamed``
    only ever iterates ``.stdout`` for lines and calls ``.wait()``."""

    def __init__(self, returncode: int, lines: tuple[str, ...] = ()):
        self.returncode = returncode
        self.stdout = iter(f"{line}\n" for line in lines)

    def wait(self) -> int:
        return self.returncode


# ---------------------------------------------------------------------------
# local registry: the resolve target -- a device tagged host="loki"
# ---------------------------------------------------------------------------


@pytest.fixture
def local_socket_dir():
    d = tempfile.mkdtemp(prefix="mbdeploy-remote-cli-local-")
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
    flash_op = FlashOp(locks=local_locks, store=local_store)
    srv = RegistryAPIServer(
        socket_path=f"{local_socket_dir}/api.sock",
        store=local_store,
        locks=local_locks,
        flash_op=flash_op,
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
    port: str = "/dev/ttyACM9",
    role: str = "NEZHA2",
    common_name: str = "robot",
    device_name: str = "tovez",
    serial: str = "1",
) -> None:
    """Replicate a peer-owned device into ``local_store``, exactly the
    shape ``registry.peering`` (ticket 005) would apply from a snapshot
    or live event -- ``store.record_peer_seen``/``upsert_remote_attached``/
    ``apply_remote_probe``, same helpers ``tests/registry/api/
    test_api.py``'s own remote-row fixtures use.
    """
    local_store.record_peer_seen(host, endpoint)
    local_store.upsert_remote_attached(uid, host, port, VID_PID)
    local_store.apply_remote_probe(
        uid,
        ProbeResult(
            role=role,
            common_name=common_name,
            device_name=device_name,
            serial=serial,
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
def remote_server(owning_store, owning_locks):
    servers: list[RemoteAPIServer] = []

    def _make(**kwargs):
        srv = RemoteAPIServer(
            host="127.0.0.1",
            port=0,
            store=owning_store,
            locks=owning_locks,
            sweep_interval_s=100.0,
            **kwargs,
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
    role: str = "NEZHA2",
    common_name: str = "robot",
    device_name: str = "tovez",
    serial: str = "1",
) -> None:
    """Seed ``uid`` into ``owning_store`` as a *locally* owned device
    (``host`` stays ``NULL``) -- from the owning registry's own point of
    view, this is an ordinary local device, exactly like
    ``test_deploy_cli.py``'s own ``_seed_device``.
    """
    owning_store.upsert_attached(uid, port, VID_PID)
    owning_store.apply_probe_result(
        uid,
        ProbeResult(
            role=role,
            common_name=common_name,
            device_name=device_name,
            serial=serial,
            raw="raw",
        ),
    )


# ---------------------------------------------------------------------------
# relay guard: refuses before any lock or hex resolution, remote target too
# ---------------------------------------------------------------------------


@pytest.mark.requires_af_unix
def test_remote_relay_guard_refuses_before_lock_or_hex_resolution(
    local_server, local_store, remote_server, owning_store, owning_locks, capsys
):
    srv = remote_server()
    uid = _uid("relayrmt")
    _seed_owned_device(owning_store, uid, role="RADIOBRIDGE", device_name="getez")
    _seed_remote_device(
        local_store, uid, "loki", f"127.0.0.1:{srv.bound_port}",
        role="RADIOBRIDGE", device_name="getez",
    )

    with pytest.raises(SystemExit) as excinfo:
        cli_mod.main(
            [
                "deploy",
                "getez",
                "--hex",
                "a.hex",
                "--socket",
                str(local_server.socket_path),
            ]
        )

    assert excinfo.value.code == EXIT_ERROR
    err = capsys.readouterr().err
    assert "relay" in err.lower()
    assert "--force-relay" in err
    assert owning_locks.status(uid) is None  # never locked on the owning host


# ---------------------------------------------------------------------------
# already-locked: the holder message names the host, not a null pid
# ---------------------------------------------------------------------------


@pytest.mark.requires_af_unix
def test_remote_device_locked_error_names_host_not_null_pid(
    local_server, local_store, remote_server, owning_store, capsys
):
    srv = remote_server()
    uid = _uid("lockedrm")
    _seed_owned_device(owning_store, uid, device_name="tovez")
    _seed_remote_device(local_store, uid, "loki", f"127.0.0.1:{srv.bound_port}")

    # Someone else already holds the flash lock on the owning host, via
    # their own remote session -- a HolderRef(origin="remote", host=...),
    # per ticket 006's response shape (pid is always None for it).
    holder_client = RemoteRegistryClient("127.0.0.1", srv.bound_port)
    holder_client.connect()
    holder_client.lock(uid, KIND_FLASH)
    try:
        start = time.monotonic()
        with pytest.raises(SystemExit) as excinfo:
            cli_mod.main(
                [
                    "deploy",
                    "tovez",
                    "--hex",
                    "a.hex",
                    "--socket",
                    str(local_server.socket_path),
                ]
            )
        elapsed = time.monotonic() - start
    finally:
        holder_client.unlock(uid)
        holder_client.close()

    assert excinfo.value.code == EXIT_LOCKED
    err = capsys.readouterr().err
    assert "locked for flash" in err
    assert "127.0.0.1" in err  # the owning host's view of the holder's origin
    assert "by pid" not in err  # never a null-pid phrasing for a remote holder
    assert elapsed < 2.0  # no retry, no blocking wait


# ---------------------------------------------------------------------------
# owning-host-unreachable: at connect time, and mid-flash
# ---------------------------------------------------------------------------


@pytest.mark.requires_af_unix
def test_owning_host_unreachable_at_connect_reports_cleanly(
    local_server, local_store, capsys
):
    uid = _uid("unreach1")
    probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    probe.bind(("127.0.0.1", 0))
    port = probe.getsockname()[1]
    probe.close()  # nothing listens here

    _seed_remote_device(local_store, uid, "loki", f"127.0.0.1:{port}")

    with pytest.raises(SystemExit) as excinfo:
        cli_mod.main(
            [
                "deploy",
                "tovez",
                "--hex",
                "a.hex",
                "--socket",
                str(local_server.socket_path),
            ]
        )

    assert excinfo.value.code == EXIT_NO_DAEMON
    err = capsys.readouterr().err
    assert "Traceback" not in err
    assert "loki" in err


@pytest.mark.requires_af_unix
def test_owning_host_unreachable_mid_flash_reports_cleanly(
    local_server, local_store, remote_server, owning_store, owning_locks,
    monkeypatch, capsys,
):
    srv = remote_server()
    uid = _uid("midflash")
    _seed_owned_device(owning_store, uid, device_name="tovez")
    _seed_remote_device(local_store, uid, "loki", f"127.0.0.1:{srv.bound_port}")

    def _boom(self, uid, hex_path, log_callback=None):
        raise RegistryUnavailable("registry unavailable: connection reset")

    monkeypatch.setattr(RemoteRegistryClient, "flash", _boom)

    with pytest.raises(SystemExit) as excinfo:
        cli_mod.main(
            [
                "deploy",
                "tovez",
                "--hex",
                "a.hex",
                "--socket",
                str(local_server.socket_path),
            ]
        )

    assert excinfo.value.code == EXIT_NO_DAEMON
    err = capsys.readouterr().err
    assert "Traceback" not in err
    # unlock still ran over the real (unpatched) connection -- never leaked.
    assert owning_locks.status(uid) is None


# ---------------------------------------------------------------------------
# remote flash: streamed success/failure against a real RemoteAPIServer
# ---------------------------------------------------------------------------


@pytest.mark.requires_af_unix
def test_remote_flash_success_increments_flash_count_server_side(
    local_server, local_store, remote_server, owning_store, owning_locks,
    monkeypatch, tmp_path, capsys,
):
    def fake_popen(cmd, **kw):
        if "flash" in cmd:
            return _FakeProcess(0, ("erasing...", "programming..."))
        return _FakeProcess(0, ("resetting...",))

    monkeypatch.setattr(subprocess, "Popen", fake_popen)

    srv = remote_server()
    uid = _uid("rflash11")
    _seed_owned_device(owning_store, uid, device_name="tovez")
    _seed_remote_device(local_store, uid, "loki", f"127.0.0.1:{srv.bound_port}")

    hex_path = tmp_path / "firmware.hex"
    hex_path.write_text(_VALID_HEX_CONTENT)

    with pytest.raises(SystemExit) as excinfo:
        cli_mod.main(
            [
                "deploy",
                "tovez",
                "--hex",
                str(hex_path),
                "--socket",
                str(local_server.socket_path),
                "--reprobe-timeout",
                "0.2",
            ]
        )

    # No daemon poll loop on the (lightweight) owning side, so the
    # re-probe wait times out -- a separate, expected outcome from this
    # test's own concern (flash_count bookkeeping), asserted regardless.
    assert excinfo.value.code == EXIT_ERROR
    err = capsys.readouterr().err
    assert "no new announcement arrived" in err
    assert "mark_flashed" not in err  # remote branch never calls it
    assert owning_store.get(uid).flash_count == 1  # incremented server-side, once
    assert owning_locks.status(uid) is None  # unlocked afterwards


@pytest.mark.requires_af_unix
def test_remote_flash_pyocd_failure_uses_generic_message(
    local_server, local_store, remote_server, owning_store, owning_locks,
    monkeypatch, tmp_path, capsys,
):
    monkeypatch.setattr(subprocess, "Popen", lambda cmd, **kw: _FakeProcess(1, ("boom",)))

    srv = remote_server()
    uid = _uid("rfail111")
    _seed_owned_device(owning_store, uid, device_name="tovez")
    _seed_remote_device(local_store, uid, "loki", f"127.0.0.1:{srv.bound_port}")

    hex_path = tmp_path / "firmware.hex"
    hex_path.write_text(_VALID_HEX_CONTENT)

    with pytest.raises(SystemExit) as excinfo:
        cli_mod.main(
            [
                "deploy",
                "tovez",
                "--hex",
                str(hex_path),
                "--socket",
                str(local_server.socket_path),
            ]
        )

    assert excinfo.value.code == EXIT_HARDWARE
    err = capsys.readouterr().err
    assert "flash failed" in err
    assert "NO FIRMWARE" not in err
    assert owning_store.get(uid).flash_count == 0
    assert owning_locks.status(uid) is None


# ---------------------------------------------------------------------------
# daemon-backed owning side: a genuine flash-triggered re-probe, end to end
# ---------------------------------------------------------------------------


class _ProbeScript:
    """Mirrors ``test_deploy_cli.py``'s own helper: hands out a fresh
    ``FakeSerial`` per ``probe()`` call, scripted with the next queued
    announcement (or silence once exhausted)."""

    def __init__(self, announcements):
        self._queue = deque(announcements)

    def __call__(self, **kwargs):
        announcement = self._queue.popleft() if self._queue else None
        return FakeSerial(announcement=announcement, **kwargs)


def _wait_until(predicate, timeout=5.0, interval=0.01):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(interval)
    assert predicate(), "condition never became true within timeout"


def _run_owning_daemon_and_remote_api(tmp_path, uid, announcements):
    """Assemble and start a real ``Daemon`` + ``RemoteAPIServer`` pair,
    sharing one lock -- the ``RemoteAPIServer`` counterpart to
    ``test_deploy_cli.py``'s own ``_run_daemon`` (which pairs ``Daemon``
    with a local ``RegistryAPIServer`` instead). Seeded with one
    steadily-attached device and a scripted probe announcement sequence,
    so the daemon's real flash-triggered re-probe hook has something
    genuine to observe once the flash lock releases.

    Returns ``(store, remote_api, thread, stop_event)`` -- caller stops/
    joins/closes in a ``finally`` block.
    """
    store = Store(tmp_path / "owning.db")
    usbwatch = FakeUSBSource([[_port_info(uid)]])
    script = _ProbeScript(announcements)
    shared_lock = threading.RLock()

    daemon = Daemon(
        usbwatch=usbwatch,
        store=store,
        serial_factory=script,
        settle_s=0,
        probe_timeout_s=0.05,
        lock=shared_lock,
        # pyocd is a real, installed dependency here -- never let this
        # daemon touch a real session if a probe ever comes back silent.
        chip_identity_session_factory=unavailable_chip_identity_session_factory,
    )
    remote_api = RemoteAPIServer(
        host="127.0.0.1",
        port=0,
        store=store,
        locks=daemon.locks,
        sweep_interval_s=100.0,
        lock=shared_lock,
    )
    remote_api.start()
    stop_event = threading.Event()
    thread = threading.Thread(
        target=daemon.run, kwargs={"interval_s": 0.01, "stop": stop_event.is_set}
    )
    thread.start()

    _wait_until(
        lambda: (rec := store.get(uid)) is not None and rec.state == "connected"
    )

    return store, remote_api, thread, stop_event


@pytest.mark.requires_af_unix
def test_remote_deploy_hex_end_to_end_reports_new_announcement(
    tmp_path, local_socket_dir, monkeypatch, capsys
):
    uid = _uid("e2ermt11")
    local_store = Store(tmp_path / "local.db")
    local_locks = LockManager()

    owning_store, remote_api, thread, stop_event = _run_owning_daemon_and_remote_api(
        tmp_path, uid, [ANNOUNCEMENT, ANNOUNCEMENT]
    )

    endpoint = f"127.0.0.1:{remote_api.bound_port}"
    _seed_remote_device(local_store, uid, "loki", endpoint)

    local_server = RegistryAPIServer(
        socket_path=f"{local_socket_dir}/api.sock",
        store=local_store,
        locks=local_locks,
        flash_op=FlashOp(locks=local_locks, store=local_store),
        sweep_interval_s=100.0,
    )
    local_server.start()

    def fake_popen(cmd, **kw):
        if "flash" in cmd:
            return _FakeProcess(0, ("erasing...", "programming..."))
        return _FakeProcess(0, ("resetting...",))

    monkeypatch.setattr(subprocess, "Popen", fake_popen)

    hex_path = tmp_path / "firmware.hex"
    hex_path.write_text(_VALID_HEX_CONTENT)

    try:
        with pytest.raises(SystemExit) as excinfo:
            cli_mod.main(
                [
                    "deploy",
                    "tovez",
                    "--hex",
                    str(hex_path),
                    "--socket",
                    str(local_server.socket_path),
                    "--reprobe-timeout",
                    "5",
                ]
            )
        assert excinfo.value.code == EXIT_OK
        out = capsys.readouterr().out
        assert "re-announced" in out
        assert "tovez" in out

        # Incremented once, server-side, by the remote `flash` op itself
        # -- mbdeploy's remote branch never calls mark_flashed.
        assert owning_store.get(uid).flash_count == 1
        assert owning_store.get(uid).state == "connected"
    finally:
        stop_event.set()
        thread.join(timeout=5.0)
        assert not thread.is_alive()
        remote_api.stop()
        local_server.stop()
        owning_store.close()
        local_store.close()
