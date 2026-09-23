"""Tests for ``mbserial`` (ticket 009) -- ``mbtools.serial.cli``.

Mirrors ``tests/deploy/test_deploy_cli.py``'s own lightweight fixture: a
real ``RegistryAPIServer`` over a real ``AF_UNIX`` socket, with devices
seeded straight into ``Store``/``LockManager`` -- no ``Daemon`` poll loop
running (this ticket's flow never waits on a re-probe, so nothing here
needs one).

Every test drives ``mbtools.serial.cli.main([...])`` and monkeypatches
``mbtools.serial.connect``'s module-level ``_pyserial`` to a stand-in
whose ``.Serial`` is a ``FakeSerial``-returning factory -- the same seam
``connect()`` itself falls back to in production
(``mbtools/serial/connect.py``'s own "test-only escape hatch" docstring
note), reached here through the real ``connect()`` call
``mbtools.serial.cli`` makes, not a mock of ``connect`` itself -- so
these tests prove the whole resolve/lock/open/interact-or-send_command
wiring end to end, not each piece in isolation.
"""

from __future__ import annotations

import io
import shutil
import sys
import tempfile

import pytest

from mbtools.common import (
    DAPLINK_VID_PID,
    EXIT_LOCKED,
    EXIT_NO_DAEMON,
    EXIT_OK,
)
from mbtools.registry.api import RegistryAPIServer
from mbtools.registry.flash import FlashOp
from mbtools.registry.identity import ProbeResult
from mbtools.registry.locks import KIND_FLASH, LockManager
from mbtools.registry.store import Store
from mbtools.serial import cli as cli_mod
from mbtools.serial import connect as connect_mod
from mbtools.testing.fakes import FakeSerial

VID_PID = "0d28:0204"
UID = "99000000111122223333444455556666e0528ab"
PORT = "/dev/ttyACM7"


@pytest.fixture
def socket_dir():
    d = tempfile.mkdtemp(prefix="mbserial-cli-")
    yield d
    shutil.rmtree(d, ignore_errors=True)


@pytest.fixture
def store(tmp_path):
    s = Store(tmp_path / "devices.db")
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


def _seed_device(store: Store, *, device_name: str = "tovez") -> None:
    store.upsert_attached(UID, PORT, VID_PID)
    store.apply_probe_result(
        UID,
        ProbeResult(
            role="NEZHA2",
            common_name="robot",
            device_name=device_name,
            serial="1",
            raw="raw",
        ),
    )


class _FakePyserialModule:
    """Stands in for the ``serial`` module ``connect.py`` imports as
    ``_pyserial`` -- its ``.Serial`` is whatever factory a test wants
    ``connect()``'s production default path to construct.
    """

    def __init__(self, factory):
        self.Serial = factory


def _install_fake_pyserial(monkeypatch, factory=None):
    if factory is None:
        factory = lambda **kwargs: FakeSerial(**kwargs)  # noqa: E731
    monkeypatch.setattr(connect_mod, "_pyserial", _FakePyserialModule(factory))
    # Keep these tests fast -- the real settle delays are for hardware.
    monkeypatch.setattr(connect_mod, "OPEN_SETTLE", 0.0)
    monkeypatch.setattr(connect_mod, "RESET_SETTLE", 0.0)
    monkeypatch.setattr(connect_mod, "EOF_DRAIN", 0.0)
    return factory


# ---------------------------------------------------------------------------
# registry unavailable
# ---------------------------------------------------------------------------


def test_registry_unavailable_reports_no_daemon(tmp_path, capsys):
    with pytest.raises(SystemExit) as excinfo:
        cli_mod.main(
            ["tovez", "--socket", str(tmp_path / "no-such.sock")]
        )
    assert excinfo.value.code == EXIT_NO_DAEMON
    assert "registry unavailable" in capsys.readouterr().err.lower()


# ---------------------------------------------------------------------------
# already-locked: fails fast, names holder kind + pid
# ---------------------------------------------------------------------------


def test_already_locked_fails_fast_naming_holder(server, store, locks, capsys):
    _seed_device(store)
    locks.acquire(UID, KIND_FLASH, 5150)

    with pytest.raises(SystemExit) as excinfo:
        cli_mod.main(["tovez", "--socket", str(server.socket_path)])

    assert excinfo.value.code == EXIT_LOCKED
    err = capsys.readouterr().err
    assert "locked for flash by pid 5150" in err


# ---------------------------------------------------------------------------
# interactive terminal: no message words
# ---------------------------------------------------------------------------


def test_interactive_session_end_to_end(server, store, locks, monkeypatch, capsys):
    _seed_device(store)
    _install_fake_pyserial(monkeypatch)
    monkeypatch.setattr(sys, "stdin", io.StringIO(""))  # immediate EOF

    with pytest.raises(SystemExit) as excinfo:
        cli_mod.main(["tovez", "--socket", str(server.socket_path)])

    assert excinfo.value.code == EXIT_OK
    err = capsys.readouterr().err
    assert "connected to tovez" in err
    assert locks.status(UID) is None  # released after interact() returns


# ---------------------------------------------------------------------------
# one-shot mode: message words joined, reply lines printed to stdout
# ---------------------------------------------------------------------------


def test_one_shot_message_prints_reply_lines(server, store, locks, monkeypatch, capsys):
    _seed_device(store)
    _install_fake_pyserial(
        monkeypatch, factory=lambda **kw: FakeSerial(announcement="OK 42", **kw)
    )

    with pytest.raises(SystemExit) as excinfo:
        cli_mod.main(
            [
                "tovez",
                "SET",
                "SPEED",
                "50",
                "--socket",
                str(server.socket_path),
                "--timeout",
                "0.3",
            ]
        )

    assert excinfo.value.code == EXIT_OK
    out = capsys.readouterr().out
    assert "OK 42" in out
    assert locks.status(UID) is None


def test_one_shot_no_reply_is_reported_and_unlocked(
    server, store, locks, monkeypatch, capsys
):
    _seed_device(store)
    _install_fake_pyserial(monkeypatch)

    with pytest.raises(SystemExit) as excinfo:
        cli_mod.main(
            [
                "tovez",
                "PING",
                "--socket",
                str(server.socket_path),
                "--timeout",
                "0.1",
            ]
        )

    err = capsys.readouterr().err
    assert "no response from tovez" in err
    assert locks.status(UID) is None


# ---------------------------------------------------------------------------
# --reset reaches connect(): reopen on this suite's own platform (macOS)
# or a break on Linux -- whichever this machine actually is; either way
# the reset path actually ran, not a silent no-op.
# ---------------------------------------------------------------------------


def test_reset_flag_reaches_connect_and_resets_the_board(
    server, store, locks, monkeypatch
):
    _seed_device(store)
    opened: list[FakeSerial] = []

    def _tracking_factory(**kwargs):
        fake = FakeSerial(**kwargs)
        opened.append(fake)
        return fake

    _install_fake_pyserial(monkeypatch, factory=_tracking_factory)
    monkeypatch.setattr(sys, "stdin", io.StringIO(""))  # immediate EOF

    with pytest.raises(SystemExit) as excinfo:
        cli_mod.main(
            ["tovez", "--reset", "--socket", str(server.socket_path)]
        )

    assert excinfo.value.code == EXIT_OK
    # A reset ran: either a BREAK on the session's own opened port
    # (Linux), or a close-and-reopen producing a second FakeSerial
    # instance (macOS) -- never neither.
    reset_happened = bool(opened[-1].break_calls) or len(opened) > 1
    assert reset_happened
    assert locks.status(UID) is None
