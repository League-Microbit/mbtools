"""Tests for ``mbregistry list`` (ticket 009) -- table and ``--json``
rendering against a real ``RegistryAPIServer`` over a real ``AF_UNIX``
socket in a short ``tmp_path``-style dir (mirrors
``tests/registry/api/test_api.py``'s own ``socket_dir`` fixture, and for
the same reason: ``AF_UNIX`` paths are limited to ~104-108 bytes).

No ``Daemon`` is involved here -- these tests only exercise the CLI's
client-of-the-api-socket path (per spec section 3.5, ``list`` never reads
``store``/``locks`` directly), so devices are seeded straight into a real
``Store``/``LockManager`` pair, exactly like ``test_api.py``'s own tests
do for the server side of the same protocol.
"""

from __future__ import annotations

import json
import shutil
import tempfile

import pytest

from mbtools.common import EXIT_NO_DAEMON, EXIT_OK
from mbtools.registry.api import RegistryAPIServer
from mbtools.registry.cli import main
from mbtools.registry.flash import FlashOp
from mbtools.registry.identity import ProbeResult
from mbtools.registry.locks import KIND_SERIAL, LockManager
from mbtools.registry.store import Store

VID_PID = "0d28:0204"


def _uid(tag: str) -> str:
    unique = (tag * 4)[:16]
    return "9900" + "0000" + "11112222" + unique + "77778888" + "6e052820"


UID_FREE_UNPROBED = _uid("aaaa1111")
UID_CONNECTED = _uid("bbbb2222")
UID_LOCKED = _uid("cccc3333")
UID_NO_FIRMWARE = _uid("dddd4444")
UID_GONE = _uid("eeee5555")


@pytest.fixture
def socket_dir():
    d = tempfile.mkdtemp(prefix="mbregistry-cli-")
    yield d
    shutil.rmtree(d, ignore_errors=True)


@pytest.fixture
def store(tmp_path):
    s = Store(tmp_path / "devices.db")
    s.upsert_attached(UID_FREE_UNPROBED, "/dev/ttyACM0", VID_PID)

    s.upsert_attached(UID_CONNECTED, "/dev/ttyACM1", VID_PID)
    s.apply_probe_result(
        UID_CONNECTED,
        ProbeResult(
            role="NEZHA2", common_name="robot", device_name="vevov", serial="1", raw="raw"
        ),
    )

    s.upsert_attached(UID_LOCKED, "/dev/ttyACM2", VID_PID)
    s.apply_probe_result(
        UID_LOCKED,
        ProbeResult(
            role="RADIOBRIDGE", common_name="relay", device_name="getez", serial="2", raw="raw"
        ),
    )

    s.upsert_attached(UID_NO_FIRMWARE, "/dev/ttyACM3", VID_PID)
    s.apply_probe_result(UID_NO_FIRMWARE, None)  # no announcement -> connected_no_firmware

    s.upsert_attached(UID_GONE, "/dev/ttyACM4", VID_PID)
    s.mark_disconnected(UID_GONE)

    yield s
    s.close()


@pytest.fixture
def locks():
    lm = LockManager()
    lm.acquire(UID_LOCKED, KIND_SERIAL, 4821)
    return lm


@pytest.fixture
def server(socket_dir, store, locks):
    flash_op = FlashOp(locks=locks, store=store)
    srv = RegistryAPIServer(
        socket_path=f"{socket_dir}/api.sock",
        store=store,
        locks=locks,
        flash_op=flash_op,
        sweep_interval_s=100.0,
    )
    srv.start()
    yield srv
    srv.stop()


# ---------------------------------------------------------------------------
# table rendering
# ---------------------------------------------------------------------------


def test_list_table_shows_state_uid_firmware_port_and_error_notes(server, capsys):
    with pytest.raises(SystemExit) as excinfo:
        main(["list", "--socket", str(server.socket_path)])

    assert excinfo.value.code == EXIT_OK
    out = capsys.readouterr().out

    assert "STATE" in out and "UID" in out and "FIRMWARE" in out and "PORT" in out
    assert "free" in out  # UID_FREE_UNPROBED and UID_CONNECTED
    assert "locked by serial pid 4821" in out  # UID_LOCKED
    assert "no-firmware" in out  # UID_NO_FIRMWARE
    assert "gone" in out  # UID_GONE
    assert "NEZHA2/robot" in out  # UID_CONNECTED's firmware cell
    assert "vevov" in out  # UID_CONNECTED's NAME cell
    # error-note line under the row that needs one
    assert "no announcement received during probe" in out


def test_list_json_matches_api_device_fields(server, capsys):
    with pytest.raises(SystemExit) as excinfo:
        main(["list", "--socket", str(server.socket_path), "--json"])

    assert excinfo.value.code == EXIT_OK
    payload = json.loads(capsys.readouterr().out)
    by_uid = {d["uid"]: d for d in payload["devices"]}

    assert set(by_uid) == {
        UID_FREE_UNPROBED,
        UID_CONNECTED,
        UID_LOCKED,
        UID_NO_FIRMWARE,
        UID_GONE,
    }
    assert by_uid[UID_LOCKED]["lock_kind"] == KIND_SERIAL
    assert by_uid[UID_LOCKED]["lock_pid"] == 4821
    assert by_uid[UID_CONNECTED]["device_name"] == "vevov"
    assert by_uid[UID_GONE]["state"] == "disconnected"


# ---------------------------------------------------------------------------
# absent socket
# ---------------------------------------------------------------------------


def test_list_against_absent_socket_prints_clear_message_and_stable_exit_code(
    tmp_path, capsys
):
    missing = tmp_path / "no-such-daemon.sock"

    with pytest.raises(SystemExit) as excinfo:
        main(["list", "--socket", str(missing)])

    assert excinfo.value.code == EXIT_NO_DAEMON
    err = capsys.readouterr().err
    assert "registry unavailable" in err
    assert "Traceback" not in err


# ---------------------------------------------------------------------------
# socket path resolution: --socket flag beats $MBREGISTRY_SOCKET beats default
# ---------------------------------------------------------------------------


def test_list_uses_socket_env_var_when_no_flag_given(server, monkeypatch, capsys):
    monkeypatch.setenv("MBREGISTRY_SOCKET", str(server.socket_path))

    with pytest.raises(SystemExit) as excinfo:
        main(["list"])

    assert excinfo.value.code == EXIT_OK


def test_list_flag_overrides_socket_env_var(server, tmp_path, monkeypatch, capsys):
    # The env var points at a socket that doesn't exist; the --flag points
    # at the real one and must win.
    monkeypatch.setenv("MBREGISTRY_SOCKET", str(tmp_path / "wrong.sock"))

    with pytest.raises(SystemExit) as excinfo:
        main(["list", "--socket", str(server.socket_path)])

    assert excinfo.value.code == EXIT_OK
