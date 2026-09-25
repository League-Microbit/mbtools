"""Tests for ``mbregistry unlock --force`` (sprint 008, ticket 003) --
the local-socket-only, manual, operator override that drops a device's
lock regardless of who holds it. Mirrors ``test_cli_list.py``'s own
"drive a real RegistryAPIServer over a real AF_UNIX socket in a short
tmp_path-style dir" pattern.
"""

from __future__ import annotations

import shutil
import tempfile

import pytest

from mbtools.common import EXIT_NO_DAEMON, EXIT_OK, EXIT_USAGE
from mbtools.registry.api import RegistryAPIServer
from mbtools.registry.cli import main
from mbtools.registry.flash import FlashOp
from mbtools.registry.identity import ProbeResult
from mbtools.registry.locks import KIND_FLASH, KIND_SERIAL, HolderRef, LockManager
from mbtools.registry.store import Store

VID_PID = "0d28:0204"
UID = "9900" + "0000" + "11112222" + "3333444455556666" + "77778888" + "6e052820"


@pytest.fixture
def socket_dir():
    d = tempfile.mkdtemp(prefix="mbregistry-cli-unlock-")
    yield d
    shutil.rmtree(d, ignore_errors=True)


@pytest.fixture
def store(tmp_path):
    s = Store(tmp_path / "devices.db")
    s.upsert_attached(UID, "/dev/ttyACM0", VID_PID)
    s.apply_probe_result(
        UID,
        ProbeResult(
            role="robot", common_name="Robot", device_name="alpha", serial="123", raw="raw"
        ),
    )
    yield s
    s.close()


@pytest.fixture
def locks():
    return LockManager()


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
# happy path
# ---------------------------------------------------------------------------


@pytest.mark.requires_af_unix
def test_unlock_force_releases_a_held_lock(server, locks, capsys):
    locks.acquire(UID, KIND_FLASH, HolderRef(origin="local", ref="4821", pid=4821))

    with pytest.raises(SystemExit) as excinfo:
        main(["unlock", UID, "--force", "--socket", str(server.socket_path)])

    assert excinfo.value.code == EXIT_OK
    assert locks.status(UID) is None
    out = capsys.readouterr().out
    assert UID in out
    assert "flash" in out
    assert "released" in out


@pytest.mark.requires_af_unix
def test_unlock_force_reports_label(server, locks, capsys):
    locks.acquire(
        UID, KIND_SERIAL, HolderRef(origin="local", ref="4821", pid=4821), label="alice-laptop"
    )

    with pytest.raises(SystemExit) as excinfo:
        main(["unlock", UID, "--force", "--socket", str(server.socket_path)])

    assert excinfo.value.code == EXIT_OK
    out = capsys.readouterr().out
    assert "alice-laptop" in out


@pytest.mark.requires_af_unix
def test_unlock_force_resolves_device_name(server, locks, capsys):
    """AC: resolves UID|NAME via find -- a device_name token works too."""
    locks.acquire(UID, KIND_SERIAL, HolderRef(origin="local", ref="4821", pid=4821))

    with pytest.raises(SystemExit) as excinfo:
        main(["unlock", "alpha", "--force", "--socket", str(server.socket_path)])

    assert excinfo.value.code == EXIT_OK
    assert locks.status(UID) is None


# ---------------------------------------------------------------------------
# already unlocked -- "not locked", not an error
# ---------------------------------------------------------------------------


@pytest.mark.requires_af_unix
def test_unlock_force_on_an_already_unlocked_device_reports_not_locked(server, capsys):
    with pytest.raises(SystemExit) as excinfo:
        main(["unlock", UID, "--force", "--socket", str(server.socket_path)])

    assert excinfo.value.code == EXIT_OK
    out = capsys.readouterr().out
    assert "not locked" in out


# ---------------------------------------------------------------------------
# --force is required
# ---------------------------------------------------------------------------


def test_unlock_without_force_flag_is_a_usage_error(capsys):
    with pytest.raises(SystemExit) as excinfo:
        main(["unlock", UID])

    assert excinfo.value.code == EXIT_USAGE
    err = capsys.readouterr().err
    assert "--force" in err


# ---------------------------------------------------------------------------
# absent socket
# ---------------------------------------------------------------------------


def test_unlock_force_against_absent_socket_prints_clear_message_and_stable_exit_code(
    tmp_path, capsys
):
    missing = tmp_path / "no-such-daemon.sock"

    with pytest.raises(SystemExit) as excinfo:
        main(["unlock", UID, "--force", "--socket", str(missing)])

    assert excinfo.value.code == EXIT_NO_DAEMON
    err = capsys.readouterr().err
    assert "registry unavailable" in err
    assert "Traceback" not in err
