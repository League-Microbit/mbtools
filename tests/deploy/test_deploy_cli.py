"""Tests for ``mbdeploy deploy`` (ticket 007) -- ``mbtools.deploy.cli``.

Two fixture styles, per sprint.md's own Testing note ("an in-process
daemon+API against a tmp_path store and socket ... with deploy.flash/
deploy.release injected as fakes"):

- A *lightweight* ``server`` fixture (mirrors
  ``tests/registry/cli/test_cli_list.py``'s own pattern): a real
  ``RegistryAPIServer`` over a real ``AF_UNIX`` socket, with devices
  seeded straight into ``Store``/``LockManager`` -- no ``Daemon`` poll
  loop running. Used for every flow that doesn't need an actual
  post-unlock re-probe to land (relay guard, already-locked, blank-board,
  mark_flashed-against-an-old-daemon, ordinary flash failure, and the
  reprobe-timeout case itself -- "nothing ever re-probes" is exactly
  "no announcement arrives").
- A *daemon-backed* fixture (mirrors
  ``tests/registry/cli/test_cli_run.py``'s ``test_run_then_list_smoke``):
  a real ``Daemon`` + ``RegistryAPIServer`` pair, driven by a background
  thread against a ``FakeUSBSource``/scripted ``FakeSerial`` factory, so
  the real flash-triggered re-probe hook actually fires and the CLI's
  wait-for-reprobe step has something genuine to observe. Used only for
  the two end-to-end success flows (``--hex``, ``--repo``), where the
  ticket's own acceptance criteria require seeing a *real* new
  announcement land, not just an absence.

Every test drives ``mbtools.deploy.cli.main([...])``, per this sprint's
CLI-test house style, and monkeypatches ``deploy.flash``/``deploy.release``
at their ``mbtools.deploy.cli`` import sites (the module-level names
``cli.py`` actually calls) rather than touching a real ``pyocd`` binary or
the network.
"""

from __future__ import annotations

import shutil
import tempfile
import threading
import time
from collections import deque
from pathlib import Path

import pytest

from mbtools.common import (
    EXIT_ERROR,
    EXIT_HARDWARE,
    EXIT_LOCKED,
    EXIT_OK,
    EXIT_USAGE,
    DAPLINK_VID_PID,
    PortInfo,
)
from mbtools.deploy import cli as cli_mod
from mbtools.deploy.release import ReleaseResult
from mbtools.registry.api import RegistryAPIServer
from mbtools.registry.cli import assemble_daemon_and_api
from mbtools.registry.flash import FlashOp
from mbtools.registry.identity import ProbeResult
from mbtools.registry.locks import KIND_FLASH, HolderRef, LockManager
from mbtools.registry.store import Store
from mbtools.testing.fakes import FakeSerial, FakeUSBSource

VID_PID = "0d28:0204"
VID, PID_ = DAPLINK_VID_PID
ANNOUNCEMENT = "device NEZHA2 robot tovez 1198504156"


def _local_holder(pid: int) -> HolderRef:
    """Mirrors api.py's own ``_local_holder`` construction (ticket 002)
    for tests that acquire directly against ``LockManager``, bypassing
    the wire protocol."""
    return HolderRef(origin="local", ref=str(pid), pid=pid)


def _uid(tag: str) -> str:
    unique = (tag * 4)[:16]
    return "9900" + "0000" + "11112222" + unique + "77778888" + "6e052820"


def _port_info(uid: str, port: str = "/dev/ttyACM0") -> PortInfo:
    return PortInfo(uid=uid, port=port, vid=VID, pid=PID_)


class _FakeFlashHex:
    """Records every call and returns a scripted ``(rc, log lines)``
    without ever touching a real ``pyocd`` subprocess."""

    def __init__(self, rc: int = 0, log_lines=()):
        self.rc = rc
        self.log_lines = list(log_lines)
        self.calls: list[dict] = []

    def __call__(self, uid, hex_path, target_mcu=None, log=None, board_name=None):
        self.calls.append(
            {
                "uid": uid,
                "hex_path": hex_path,
                "target_mcu": target_mcu,
                "board_name": board_name,
            }
        )
        for line in self.log_lines:
            if log is not None:
                log(line)
        return self.rc


class _FakeResolveHex:
    """Records every call and returns a scripted :class:`ReleaseResult`
    without ever touching the network."""

    def __init__(self, result: ReleaseResult):
        self.result = result
        self.calls: list[dict] = []

    def __call__(self, repo_ref, asset_override=None, **kwargs):
        self.calls.append({"repo_ref": repo_ref, "asset_override": asset_override})
        return self.result


def _unexpected(*args, **kwargs):
    raise AssertionError("this should never be called")


# ---------------------------------------------------------------------------
# lightweight fixture: RegistryAPIServer only, no Daemon poll loop
# ---------------------------------------------------------------------------


@pytest.fixture
def socket_dir():
    d = tempfile.mkdtemp(prefix="mbdeploy-cli-")
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


def _seed_device(
    store: Store,
    uid: str,
    *,
    port: str = "/dev/ttyACM0",
    role: str = "NEZHA2",
    common_name: str = "robot",
    device_name: str = "tovez",
    serial: str = "1",
) -> None:
    store.upsert_attached(uid, port, VID_PID)
    store.apply_probe_result(
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
# --hex / --repo mutual exclusivity (usage error, before any device
# interaction -- argparse itself enforces this, before cmd_deploy runs)
# ---------------------------------------------------------------------------


class TestUsageValidation:
    def test_hex_and_repo_together_is_a_usage_error(self, capsys):
        with pytest.raises(SystemExit) as excinfo:
            cli_mod.main(
                ["deploy", "tovez", "--hex", "a.hex", "--repo", "o/r"]
            )
        assert excinfo.value.code == EXIT_USAGE
        assert "Traceback" not in capsys.readouterr().err

    def test_neither_hex_nor_repo_is_a_usage_error(self, capsys):
        with pytest.raises(SystemExit) as excinfo:
            cli_mod.main(["deploy", "tovez"])
        assert excinfo.value.code == EXIT_USAGE

    def test_asset_without_repo_is_a_usage_error(self, server, capsys):
        with pytest.raises(SystemExit) as excinfo:
            cli_mod.main(
                [
                    "deploy",
                    "tovez",
                    "--hex",
                    "a.hex",
                    "--asset",
                    "MICROBIT.hex",
                    "--socket",
                    str(server.socket_path),
                ]
            )
        assert excinfo.value.code == EXIT_USAGE
        assert "--asset" in capsys.readouterr().err


# ---------------------------------------------------------------------------
# relay guard: refuses before any lock or hex resolution
# ---------------------------------------------------------------------------


def test_relay_guard_refuses_before_lock_or_hex_resolution(
    server, store, locks, monkeypatch, capsys
):
    uid = _uid("relay111")
    _seed_device(store, uid, role="RADIOBRIDGE", device_name="getez")

    monkeypatch.setattr(cli_mod, "flash_hex", _unexpected)
    monkeypatch.setattr(cli_mod, "resolve_hex", _unexpected)

    with pytest.raises(SystemExit) as excinfo:
        cli_mod.main(
            [
                "deploy",
                "getez",
                "--hex",
                "a.hex",
                "--socket",
                str(server.socket_path),
            ]
        )

    assert excinfo.value.code == EXIT_ERROR
    err = capsys.readouterr().err
    assert "relay" in err.lower()
    assert "--force-relay" in err
    assert locks.status(uid) is None  # never locked


# ---------------------------------------------------------------------------
# already-locked: fails fast, no retry, no blocking wait
# ---------------------------------------------------------------------------


def test_already_locked_fails_fast_naming_holder(server, store, locks, capsys):
    uid = _uid("locked11")
    _seed_device(store, uid, device_name="tovez")
    locks.acquire(uid, KIND_FLASH, _local_holder(9911))

    start = time.monotonic()
    with pytest.raises(SystemExit) as excinfo:
        cli_mod.main(
            ["deploy", "tovez", "--hex", "a.hex", "--socket", str(server.socket_path)]
        )
    elapsed = time.monotonic() - start

    assert excinfo.value.code == EXIT_LOCKED
    err = capsys.readouterr().err
    assert "locked for flash by pid 9911" in err
    assert elapsed < 1.0  # no retry, no blocking wait


# ---------------------------------------------------------------------------
# mark_flashed against an old daemon (invalid_request) is a non-fatal warning
# ---------------------------------------------------------------------------


def test_mark_flashed_invalid_request_is_non_fatal_warning(
    server, store, locks, monkeypatch, capsys
):
    uid = _uid("oldflash")
    _seed_device(store, uid, device_name="tovez")

    def _old_daemon_mark_flashed(req, pid):
        return {"ok": False, "code": "invalid_request", "error": "unknown op 'mark_flashed'"}

    monkeypatch.setattr(server, "_op_mark_flashed", _old_daemon_mark_flashed)
    monkeypatch.setattr(cli_mod, "flash_hex", _FakeFlashHex(rc=0))

    with pytest.raises(SystemExit) as excinfo:
        cli_mod.main(
            [
                "deploy",
                "tovez",
                "--hex",
                "a.hex",
                "--socket",
                str(server.socket_path),
                "--reprobe-timeout",
                "0.2",
            ]
        )

    # No daemon poll loop is running in this fixture, so the flash itself
    # succeeded but the re-probe wait times out -- that is a separate,
    # expected outcome from this test's own concern (the mark_flashed
    # warning), asserted below regardless of the final exit code.
    assert excinfo.value.code == EXIT_ERROR
    err = capsys.readouterr().err
    assert "mark_flashed not supported" in err
    assert store.get(uid).flash_count == 0  # never incremented
    assert locks.status(uid) is None  # still unlocked afterwards


# ---------------------------------------------------------------------------
# blank-board report is distinct from a generic flash failure
# ---------------------------------------------------------------------------


def test_blank_board_report_is_distinct_from_generic_flash_failure(
    server, store, locks, monkeypatch, capsys
):
    uid = _uid("blank111")
    _seed_device(store, uid, device_name="tovez")

    monkeypatch.setattr(
        cli_mod,
        "flash_hex",
        _FakeFlashHex(
            rc=1,
            log_lines=[
                "Error: flash still failed after mass erase (exit 1) — "
                "tovez WAS ERASED AND NOW HAS NO FIRMWARE. It will not run "
                "until it is successfully reflashed.",
            ],
        ),
    )

    with pytest.raises(SystemExit) as excinfo:
        cli_mod.main(
            ["deploy", "tovez", "--hex", "a.hex", "--socket", str(server.socket_path)]
        )

    assert excinfo.value.code == EXIT_HARDWARE
    err = capsys.readouterr().err
    assert "NO FIRMWARE" in err
    assert "flash failed (exit" not in err  # never folded into the generic message
    assert store.get(uid).flash_count == 0
    assert locks.status(uid) is None  # unlocked even on failure


def test_ordinary_flash_failure_uses_generic_message(
    server, store, locks, monkeypatch, capsys
):
    uid = _uid("ordfail1")
    _seed_device(store, uid, device_name="tovez")

    monkeypatch.setattr(cli_mod, "flash_hex", _FakeFlashHex(rc=1))

    with pytest.raises(SystemExit) as excinfo:
        cli_mod.main(
            ["deploy", "tovez", "--hex", "a.hex", "--socket", str(server.socket_path)]
        )

    assert excinfo.value.code == EXIT_HARDWARE
    err = capsys.readouterr().err
    assert "flash failed (exit 1)" in err
    assert "NO FIRMWARE" not in err
    assert locks.status(uid) is None


# ---------------------------------------------------------------------------
# wait-for-reprobe timeout: no announcement ever arrives -- reported
# plainly, never hangs, never claimed as success
# ---------------------------------------------------------------------------


def test_wait_for_reprobe_timeout_reports_plainly_and_does_not_hang(
    server, store, locks, monkeypatch, capsys
):
    uid = _uid("timeout1")
    _seed_device(store, uid, device_name="tovez")

    monkeypatch.setattr(cli_mod, "flash_hex", _FakeFlashHex(rc=0))

    start = time.monotonic()
    with pytest.raises(SystemExit) as excinfo:
        cli_mod.main(
            [
                "deploy",
                "tovez",
                "--hex",
                "a.hex",
                "--socket",
                str(server.socket_path),
                "--reprobe-timeout",
                "0.3",
            ]
        )
    elapsed = time.monotonic() - start

    assert excinfo.value.code == EXIT_ERROR
    err = capsys.readouterr().err
    assert "no new announcement arrived" in err
    assert 0.3 <= elapsed < 2.0  # bounded, not instant, not hung


# ---------------------------------------------------------------------------
# _is_relay -- pure-function coverage
# ---------------------------------------------------------------------------


class TestIsRelay:
    @pytest.mark.parametrize(
        "role,expected",
        [
            ("RADIOBRIDGE", True),
            ("radiobridge", True),
            ("RADIORELAY", True),
            ("NEZHA2", False),
            (None, False),
            ("", False),
        ],
    )
    def test_is_relay(self, role, expected):
        assert cli_mod._is_relay(role) is expected


# ---------------------------------------------------------------------------
# daemon-backed fixture: real flash-triggered re-probe, end to end
# ---------------------------------------------------------------------------


class _ProbeScript:
    """Mirrors tests/registry/cli/test_cli_run.py's own helper: hands out
    a fresh FakeSerial per probe() call, scripted with the next queued
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


@pytest.fixture
def daemon_socket_dir():
    d = tempfile.mkdtemp(prefix="mbdeploy-cli-daemon-")
    yield d
    shutil.rmtree(d, ignore_errors=True)


def _run_daemon(tmp_path, socket_dir, announcements):
    """Assemble and start a real Daemon+RegistryAPIServer pair (ticket
    009's own assembly function) in a background thread, seeded with one
    steadily-attached device and a scripted probe announcement sequence.
    Returns (socket_path, store, thread, stop_event, api) -- caller is
    responsible for stopping/joining/closing in a ``finally`` block.
    """
    uid = _uid("e2e11111")
    socket_path = f"{socket_dir}/api.sock"
    store = Store(tmp_path / "devices.db")
    usbwatch = FakeUSBSource([[_port_info(uid)]])
    script = _ProbeScript(announcements)

    daemon, api = assemble_daemon_and_api(
        store=store,
        usbwatch=usbwatch,
        socket_path=socket_path,
        serial_factory=script,
        settle_s=0,
        probe_timeout_s=0.05,
    )
    api.start()
    stop_event = threading.Event()
    thread = threading.Thread(
        target=daemon.run, kwargs={"interval_s": 0.01, "stop": stop_event.is_set}
    )
    thread.start()

    _wait_until(
        lambda: (rec := store.get(uid)) is not None and rec.state == "connected"
    )

    return uid, socket_path, store, thread, stop_event, api


def test_deploy_hex_end_to_end_reports_new_announcement(tmp_path, daemon_socket_dir, monkeypatch, capsys):
    uid, socket_path, store, thread, stop_event, api = _run_daemon(
        tmp_path, daemon_socket_dir, [ANNOUNCEMENT, ANNOUNCEMENT]
    )
    fake_flash = _FakeFlashHex(rc=0, log_lines=["flashing...", "verify ok"])
    monkeypatch.setattr(cli_mod, "flash_hex", fake_flash)

    try:
        with pytest.raises(SystemExit) as excinfo:
            cli_mod.main(
                [
                    "deploy",
                    "tovez",
                    "--hex",
                    "/tmp/does-not-matter.hex",
                    "--socket",
                    socket_path,
                    "--reprobe-timeout",
                    "5",
                ]
            )
        assert excinfo.value.code == EXIT_OK
        out = capsys.readouterr().out
        assert "re-announced" in out
        assert "tovez" in out

        assert len(fake_flash.calls) == 1
        assert fake_flash.calls[0]["uid"] == uid
        assert fake_flash.calls[0]["hex_path"] == "/tmp/does-not-matter.hex"
        assert fake_flash.calls[0]["board_name"] == "tovez"

        assert store.get(uid).flash_count == 1  # mark_flashed really ran
        assert store.get(uid).state == "connected"
    finally:
        stop_event.set()
        thread.join(timeout=5.0)
        assert not thread.is_alive()
        api.stop()
        store.close()


def test_deploy_repo_end_to_end_reports_new_announcement(tmp_path, daemon_socket_dir, monkeypatch, capsys):
    uid, socket_path, store, thread, stop_event, api = _run_daemon(
        tmp_path, daemon_socket_dir, [ANNOUNCEMENT, ANNOUNCEMENT]
    )
    release = ReleaseResult(
        owner="League-Microbit",
        repo="nezha-robot-template",
        tag="v0.20260919.7",
        asset_name="MICROBIT.hex",
        hex_path=Path("/fake/cache/MICROBIT.hex"),
        hex_bytes=b"fake hex bytes",
        from_cache=False,
    )
    fake_resolve = _FakeResolveHex(release)
    fake_flash = _FakeFlashHex(rc=0)
    monkeypatch.setattr(cli_mod, "resolve_hex", fake_resolve)
    monkeypatch.setattr(cli_mod, "flash_hex", fake_flash)

    try:
        with pytest.raises(SystemExit) as excinfo:
            cli_mod.main(
                [
                    "deploy",
                    "tovez",
                    "--repo",
                    "League-Microbit/nezha-robot-template@v0.20260919.7",
                    "--socket",
                    socket_path,
                    "--reprobe-timeout",
                    "5",
                ]
            )
        assert excinfo.value.code == EXIT_OK

        assert len(fake_resolve.calls) == 1
        assert fake_resolve.calls[0]["repo_ref"] == (
            "League-Microbit/nezha-robot-template@v0.20260919.7"
        )
        assert fake_resolve.calls[0]["asset_override"] is None

        assert len(fake_flash.calls) == 1
        assert fake_flash.calls[0]["hex_path"] == "/fake/cache/MICROBIT.hex"

        assert store.get(uid).flash_count == 1
    finally:
        stop_event.set()
        thread.join(timeout=5.0)
        assert not thread.is_alive()
        api.stop()
        store.close()
