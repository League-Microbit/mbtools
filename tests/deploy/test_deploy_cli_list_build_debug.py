"""Tests for ``mbdeploy list``, ``mbdeploy build``, and ``mbdeploy debug``
(ticket 008) -- ``mbtools.deploy.cli``.

``list`` is verified for output *parity* against ``mbregistry list``
(SUC-003's own acceptance criterion: "verified by running both against
the same daemon and diffing output") by driving both CLIs' ``main()``
against one real ``RegistryAPIServer`` and comparing captured stdout
byte-for-byte, rather than re-testing ``registry.render`` itself (already
covered by ``tests/registry/render/test_render.py``).

``build`` never touches the registry, so its tests run against a fake
``build.py``/``--build-cmd`` script written to ``tmp_path`` (a real, but
trivial, subprocess -- never the firmware toolchain) per the ticket's own
Testing note ("no real subprocess" meaning no real firmware build).

``debug`` is verified against the same lightweight ``RegistryAPIServer``
fixture ``test_deploy_cli.py`` uses, with ``mbtools.deploy.cli._run_pyocd``
monkeypatched to a fake -- no test here ever shells out to a real
``pyocd`` binary or touches a probe.
"""

from __future__ import annotations

import json
import shutil
import sys
import tempfile
import time

import pytest

from mbtools.common import (
    EXIT_ERROR,
    EXIT_LOCKED,
    EXIT_NO_DAEMON,
    EXIT_OK,
)
from mbtools.deploy import cli as cli_mod
from mbtools.registry import cli as registry_cli_mod
from mbtools.registry.api import RegistryAPIServer
from mbtools.registry.flash import FlashOp
from mbtools.registry.identity import ProbeResult
from mbtools.registry.locks import KIND_DEBUG, KIND_FLASH, LockManager
from mbtools.registry.store import Store

VID_PID = "0d28:0204"


def _uid(tag: str) -> str:
    unique = (tag * 4)[:16]
    return "9900" + "0000" + "11112222" + unique + "77778888" + "6e052820"


def _unexpected(*args, **kwargs):
    raise AssertionError("this should never be called")


# ---------------------------------------------------------------------------
# shared fixtures -- mirrors test_deploy_cli.py's own lightweight server
# ---------------------------------------------------------------------------


@pytest.fixture
def socket_dir():
    d = tempfile.mkdtemp(prefix="mbdeploy-cli-lbd-")
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
# list -- output parity with `mbregistry list`
# ---------------------------------------------------------------------------


def test_mbdeploy_list_table_identical_to_mbregistry_list(
    server, store, locks, capsys
):
    uid_free = _uid("free1111")
    _seed_device(store, uid_free, device_name="vevov")
    uid_locked = _uid("locked22")
    _seed_device(store, uid_locked, device_name="getez", role="RADIOBRIDGE")
    locks.acquire(uid_locked, KIND_FLASH, 4821)

    with pytest.raises(SystemExit) as info:
        cli_mod.main(["list", "--socket", str(server.socket_path)])
    assert info.value.code == EXIT_OK
    deploy_out = capsys.readouterr().out

    with pytest.raises(SystemExit) as info:
        registry_cli_mod.main(["list", "--socket", str(server.socket_path)])
    assert info.value.code == EXIT_OK
    registry_out = capsys.readouterr().out

    assert deploy_out == registry_out
    assert "vevov" in deploy_out
    assert "locked by flash pid 4821" in deploy_out


def test_mbdeploy_list_json_identical_to_mbregistry_list(server, store, capsys):
    uid = _uid("json1111")
    _seed_device(store, uid, device_name="vevov")

    with pytest.raises(SystemExit) as info:
        cli_mod.main(["list", "--socket", str(server.socket_path), "--json"])
    assert info.value.code == EXIT_OK
    deploy_out = capsys.readouterr().out

    with pytest.raises(SystemExit) as info:
        registry_cli_mod.main(
            ["list", "--socket", str(server.socket_path), "--json"]
        )
    assert info.value.code == EXIT_OK
    registry_out = capsys.readouterr().out

    assert deploy_out == registry_out
    assert json.loads(deploy_out)["devices"][0]["uid"] == uid


def test_mbdeploy_list_against_absent_socket_reports_exit_no_daemon(
    tmp_path, capsys
):
    missing = tmp_path / "no-such-daemon.sock"

    with pytest.raises(SystemExit) as info:
        cli_mod.main(["list", "--socket", str(missing)])

    assert info.value.code == EXIT_NO_DAEMON
    err = capsys.readouterr().err
    assert "registry unavailable" in err
    assert "Traceback" not in err


# ---------------------------------------------------------------------------
# build -- shells out to build.py in CWD, no registry interaction
# ---------------------------------------------------------------------------

_FAKE_BUILD_SCRIPT = """\
import json
import os
import sys
from pathlib import Path

Path("build_argv.json").write_text(json.dumps(sys.argv[1:]))
sys.exit(int(os.environ.get("FAKE_BUILD_EXIT", "0")))
"""


def test_build_shells_out_to_build_py_in_cwd_by_default(
    tmp_path, monkeypatch, capsys
):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "build.py").write_text(_FAKE_BUILD_SCRIPT)

    with pytest.raises(SystemExit) as info:
        cli_mod.main(["build", "--clean", "-j", "3"])

    assert info.value.code == EXIT_OK
    argv = json.loads((tmp_path / "build_argv.json").read_text())
    assert argv == ["--clean", "-j", "3"]


def test_build_returns_build_scripts_own_exit_code(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "build.py").write_text(_FAKE_BUILD_SCRIPT)
    monkeypatch.setenv("FAKE_BUILD_EXIT", "7")

    with pytest.raises(SystemExit) as info:
        cli_mod.main(["build"])

    assert info.value.code == 7


def test_build_cmd_overrides_default_and_still_appends_flags(
    tmp_path, monkeypatch
):
    monkeypatch.chdir(tmp_path)
    fake = tmp_path / "other_build.py"
    fake.write_text(_FAKE_BUILD_SCRIPT)

    with pytest.raises(SystemExit) as info:
        cli_mod.main(
            [
                "build",
                "--build-cmd",
                f"{sys.executable} {fake}",
                "--verbose",
            ]
        )

    assert info.value.code == EXIT_OK
    argv = json.loads((tmp_path / "build_argv.json").read_text())
    assert argv == ["--verbose"]


def test_build_missing_build_py_is_a_clear_error_not_a_traceback(
    tmp_path, monkeypatch, capsys
):
    monkeypatch.chdir(tmp_path)

    with pytest.raises(SystemExit) as info:
        cli_mod.main(["build"])

    assert info.value.code == EXIT_ERROR
    err = capsys.readouterr().err
    assert "build.py not found" in err
    assert "Traceback" not in err


# ---------------------------------------------------------------------------
# debug -- resolve, lock (kind=debug), bare pyocd passthrough, unlock
# ---------------------------------------------------------------------------


def test_debug_takes_lock_before_running_and_releases_after_success(
    server, store, locks, monkeypatch, capsys
):
    uid = _uid("dbgok111")
    _seed_device(store, uid, device_name="tovez")

    seen_cmds = []

    def _fake_run_pyocd(cmd):
        # Verified from inside the fake itself: the lock must already be
        # held, as kind=debug, at the moment pyocd would run.
        status = locks.status(uid)
        assert status is not None
        assert status.kind == KIND_DEBUG
        seen_cmds.append(cmd)
        return 0

    monkeypatch.setattr(cli_mod, "_run_pyocd", _fake_run_pyocd)

    with pytest.raises(SystemExit) as info:
        cli_mod.main(
            [
                "debug",
                "--socket",
                str(server.socket_path),
                "tovez",
                "--",
                "commander",
                "--target",
                "nrf52833",
            ]
        )

    assert info.value.code == EXIT_OK
    assert seen_cmds == [
        [sys.executable, "-m", "pyocd", "commander", "--target", "nrf52833"]
    ]
    assert locks.status(uid) is None  # released after the session ends


def test_debug_releases_lock_after_a_nonzero_pyocd_exit(
    server, store, locks, monkeypatch
):
    uid = _uid("dbgfail1")
    _seed_device(store, uid, device_name="tovez")

    monkeypatch.setattr(cli_mod, "_run_pyocd", lambda cmd: 9)

    with pytest.raises(SystemExit) as info:
        cli_mod.main(
            ["debug", "--socket", str(server.socket_path), "tovez", "--", "commander"]
        )

    assert info.value.code == 9
    assert locks.status(uid) is None


def test_debug_releases_lock_on_sigint_and_reports_interrupted(
    server, store, locks, monkeypatch, capsys
):
    uid = _uid("dbgint11")
    _seed_device(store, uid, device_name="tovez")

    def _fake_run_pyocd(cmd):
        raise KeyboardInterrupt

    monkeypatch.setattr(cli_mod, "_run_pyocd", _fake_run_pyocd)

    with pytest.raises(SystemExit) as info:
        cli_mod.main(
            ["debug", "--socket", str(server.socket_path), "tovez", "--", "commander"]
        )

    assert info.value.code == cli_mod._EXIT_SIGINT
    err = capsys.readouterr().err
    assert "interrupted" in err
    assert "Traceback" not in err
    assert locks.status(uid) is None  # no leaked lock on an interrupted session


def test_debug_against_already_locked_device_fails_fast_no_retry(
    server, store, locks, monkeypatch, capsys
):
    uid = _uid("dbglock1")
    _seed_device(store, uid, device_name="tovez")
    locks.acquire(uid, KIND_FLASH, 9911)

    monkeypatch.setattr(cli_mod, "_run_pyocd", _unexpected)

    start = time.monotonic()
    with pytest.raises(SystemExit) as info:
        cli_mod.main(
            ["debug", "--socket", str(server.socket_path), "tovez", "--", "commander"]
        )
    elapsed = time.monotonic() - start

    assert info.value.code == EXIT_LOCKED
    err = capsys.readouterr().err
    assert "locked for flash by pid 9911" in err
    assert elapsed < 1.0  # no retry, no blocking wait
    status = locks.status(uid)
    assert status is not None and status.kind == KIND_FLASH  # untouched
