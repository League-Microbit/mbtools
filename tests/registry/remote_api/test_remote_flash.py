"""Tests for mbtools.registry.remote_api's "flash"/"send_hex" ops
(ticket 008) -- the remote-flash territory that gives a TCP client the
same retry/mass-erase/blank-board robustness a local `mbdeploy deploy`
gets, by calling `mbtools.registry.flashlogic.flash_hex` directly (not
the sprint-1 minimal `registry.flash.FlashOp`).

Every test here drives a real RemoteAPIServer over a real AF_INET
loopback socket, mirroring test_remote_api.py's own precedent, with
pyocd faked by patching `subprocess.Popen` -- the same technique
tests/deploy/test_deploy_flash.py uses for the identical function this
ticket proves is being reused, not reimplemented (see the transient/
mass-erase tests below, which assert the exact log line text sprint 1's
local-path tests already assert on).
"""

from __future__ import annotations

import base64
import json
import os
import socket
import subprocess
import sys
import time

import pytest

from mbtools.common import CODE_INVALID_REQUEST, CODE_NOT_LOCKED
from mbtools.registry import remote_api as remote_api_module
from mbtools.registry.identity import ProbeResult
from mbtools.registry.locks import KIND_FLASH, LockManager
from mbtools.registry.remote_api import RemoteAPIServer
from mbtools.registry.store import Store

LOCAL_UID = "aa00" + "0000" + "11112222" + "3333444455556666" + "77778888" + "6e052820"
VID_PID = "0d28:0204"
DEVICE_NAME = "widget"

_PYOCD = [sys.executable, "-m", "pyocd"]

#: A minimal, complete, valid Intel HEX file -- just the EOF record.
_VALID_HEX_CONTENT = ":00000001FF\n"

#: A locked-device failure signature (registry.flashlogic._LOCKED_SIGNATURES).
_LOCKED_SIGNATURE_LINE = "flash erase sector failure (0x67)"

#: A transient probe/communication failure signature
#: (registry.flashlogic._TRANSIENT_SIGNATURES).
_TRANSIENT_SIGNATURE_LINE = "DAPAccess Error: some transient fault"


# ---------------------------------------------------------------------------
# test doubles / helpers
# ---------------------------------------------------------------------------


class _FakeProcess:
    """Stand-in for a `subprocess.Popen` instance -- mirrors
    tests/deploy/test_deploy_flash.py's own fake exactly: `_run_streamed`
    only ever iterates `.stdout` for lines and calls `.wait()`."""

    def __init__(self, returncode: int, lines: tuple[str, ...] = ()):
        self.returncode = returncode
        self.stdout = iter(f"{line}\n" for line in lines)

    def wait(self) -> int:
        return self.returncode


class _Client:
    """A minimal newline-delimited-JSON client over a real AF_INET
    loopback socket (mirrors test_remote_api.py's own _Client), plus
    `request_stream` for this module's one streaming op."""

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

    def request_stream(self, obj: dict) -> tuple[list[str], dict]:
        """Send a `flash` request and collect every `{"type": "log", ...}`
        line until the terminal, non-"log" line -- the dispatch loop
        docs/design/registry-api.md's own "flash" section describes."""
        self.send(obj)
        logs: list[str] = []
        while True:
            resp = self.recv()
            if resp.get("type") == "log":
                logs.append(resp["line"])
                continue
            return logs, resp

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


def _send_hex(client: _Client, content: str = _VALID_HEX_CONTENT) -> str:
    resp = client.request(
        {"op": "send_hex", "data": base64.b64encode(content.encode()).decode()}
    )
    assert resp["ok"] is True, resp
    return resp["hex_path"]


def _any_log_contains(logs: list[str], *substrings: str) -> bool:
    lowered = [line.lower() for line in logs]
    return any(all(s.lower() in line for s in substrings) for line in lowered)


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
            role="robot",
            common_name="Robot",
            device_name=DEVICE_NAME,
            serial="123",
            raw="DEVICE:widget",
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

    def _make():
        srv = RemoteAPIServer(
            host="127.0.0.1", port=0, store=store, locks=locks, sweep_interval_s=100.0
        )
        srv.start()
        servers.append(srv)
        return srv

    yield _make
    for srv in servers:
        srv.stop()


@pytest.fixture
def locked_client(remote_server):
    """A connected, already-flash-locked client -- the common precondition
    for every real flash attempt below."""
    srv = remote_server()
    client = _Client(srv.bound_port)
    resp = client.request({"op": "lock", "uid": LOCAL_UID, "kind": KIND_FLASH})
    assert resp == {"ok": True}
    yield client
    client.close()


# ---------------------------------------------------------------------------
# send_hex + flash: happy path
# ---------------------------------------------------------------------------


def test_send_hex_then_flash_success_streams_logs_and_reports_result(
    monkeypatch, locked_client, store, locks
):
    calls: list[list[str]] = []

    def fake_popen(cmd, **kw):
        calls.append(cmd)
        if "flash" in cmd:
            return _FakeProcess(0, ("erasing...", "programming..."))
        return _FakeProcess(0, ("resetting...",))

    monkeypatch.setattr(subprocess, "Popen", fake_popen)

    hex_path = _send_hex(locked_client)
    logs, result = locked_client.request_stream(
        {"op": "flash", "uid": LOCAL_UID, "hex_path": hex_path}
    )

    assert result == {
        "type": "result",
        "ok": True,
        "success": True,
        "exit_code": 0,
        "error": None,
    }
    assert "erasing..." in logs
    assert "programming..." in logs
    assert store.find(LOCAL_UID).flash_count == 1
    assert locks.status(LOCAL_UID) is None
    assert not os.path.exists(hex_path)


def test_flash_argv_matches_local_flashlogic_exactly(monkeypatch, locked_client):
    """Proves the remote op invokes the exact same pyocd argv the local
    path does (ticket 008 acceptance criterion: calls flashlogic.flash_hex,
    not a reimplementation)."""
    calls: list[list[str]] = []

    def fake_popen(cmd, **kw):
        calls.append(cmd)
        return _FakeProcess(0)

    monkeypatch.setattr(subprocess, "Popen", fake_popen)

    hex_path = _send_hex(locked_client)
    _, result = locked_client.request_stream(
        {"op": "flash", "uid": LOCAL_UID, "hex_path": hex_path}
    )

    assert result["success"] is True
    flash_cmd, reset_cmd = calls
    assert flash_cmd == [*_PYOCD, "flash", "-t", "nrf52833", "--uid", LOCAL_UID, hex_path]
    assert reset_cmd == [*_PYOCD, "reset", "-t", "nrf52833", "--uid", LOCAL_UID]


# ---------------------------------------------------------------------------
# preconditions
# ---------------------------------------------------------------------------


def test_flash_without_a_prior_lock_is_refused(remote_server):
    srv = remote_server()
    client = _Client(srv.bound_port)

    hex_path = _send_hex(client)
    _, result = client.request_stream(
        {"op": "flash", "uid": LOCAL_UID, "hex_path": hex_path}
    )

    assert result["ok"] is False
    assert result["code"] == CODE_NOT_LOCKED
    assert result["type"] == "result"
    assert result["success"] is False
    assert result["exit_code"] is None
    client.close()


def test_flash_rejects_a_hex_path_not_staged_by_this_connection(remote_server):
    """A different connection's staged hex file (or any path a client
    simply names) must never be flashable -- only a path this same
    connection's own `send_hex` returned."""
    srv = remote_server()
    stager = _Client(srv.bound_port)
    other_hex_path = _send_hex(stager)

    flasher = _Client(srv.bound_port)
    flasher.request({"op": "lock", "uid": LOCAL_UID, "kind": KIND_FLASH})
    _, result = flasher.request_stream(
        {"op": "flash", "uid": LOCAL_UID, "hex_path": other_hex_path}
    )

    assert result["ok"] is False
    assert result["code"] == CODE_INVALID_REQUEST
    assert result["type"] == "result"
    # The staged file is still owned by `stager`'s connection -- untouched
    # by the refused attempt on `flasher`'s connection. Checked *before*
    # closing either connection: closing `stager` triggers its own
    # staged-but-unflashed cleanup (see
    # test_connection_close_cleans_up_an_unflashed_staged_hex_file below),
    # which runs on the server's connection-handler thread asynchronously
    # to this client-side close() call. Asserting after both closes raced
    # that cleanup thread -- an intermittent failure this reordering
    # fixes (the file's existence is deterministic before either
    # connection closes; it is not deterministic after).
    assert os.path.exists(other_hex_path)
    flasher.close()
    stager.close()  # cleans up other_hex_path itself; nothing left to unlink


# ---------------------------------------------------------------------------
# transient retry / mass-erase recovery -- proving the *same* function runs
# ---------------------------------------------------------------------------


def test_transient_failure_is_retried_once_then_succeeds(monkeypatch, locked_client, locks):
    calls: list[list[str]] = []
    state = {"flash": 0}

    def fake_popen(cmd, **kw):
        calls.append(cmd)
        if "flash" in cmd:
            state["flash"] += 1
            if state["flash"] == 1:
                return _FakeProcess(1, (_TRANSIENT_SIGNATURE_LINE,))
            return _FakeProcess(0)
        return _FakeProcess(0)

    monkeypatch.setattr(subprocess, "Popen", fake_popen)

    hex_path = _send_hex(locked_client)
    logs, result = locked_client.request_stream(
        {"op": "flash", "uid": LOCAL_UID, "hex_path": hex_path}
    )

    assert result["success"] is True
    assert not any("erase" in c for c in calls)  # no mass erase for a transient fault
    assert _any_log_contains(
        logs, "retrying once before any mass-erase decision"
    ), logs
    assert locks.status(LOCAL_UID) is None


def test_locked_signature_triggers_mass_erase_recovery_then_succeeds(
    monkeypatch, locked_client, store, locks
):
    calls: list[list[str]] = []
    state = {"flash": 0}

    def fake_popen(cmd, **kw):
        calls.append(cmd)
        if "erase" in cmd:
            return _FakeProcess(0)
        if "flash" in cmd:
            state["flash"] += 1
            if state["flash"] == 1:
                return _FakeProcess(1, (_LOCKED_SIGNATURE_LINE,))
            return _FakeProcess(0)
        return _FakeProcess(0)  # reset

    monkeypatch.setattr(subprocess, "Popen", fake_popen)

    hex_path = _send_hex(locked_client)
    logs, result = locked_client.request_stream(
        {"op": "flash", "uid": LOCAL_UID, "hex_path": hex_path}
    )

    assert result["success"] is True
    erase_calls = [c for c in calls if "erase" in c]
    assert len(erase_calls) == 1
    assert _any_log_contains(
        logs, "attempting ctrl-ap mass erase to recover a locked device"
    ), logs
    assert store.find(LOCAL_UID).flash_count == 1
    assert locks.status(LOCAL_UID) is None


def test_blank_board_after_failed_reflash_is_reported_and_not_marked_flashed(
    monkeypatch, locked_client, store, locks
):
    """Mass erase succeeds but the retried flash still fails -- the board
    has no firmware at all. flashlogic.flash_hex's own message (naming
    the board via `board_name`) reaches the client through the ordinary
    streamed log lines, unchanged -- this is ticket 012's client-side
    "blank_board" detection working unmodified against this remote path,
    because the message text is identical by construction (same
    function)."""
    calls: list[list[str]] = []
    state = {"flash": 0}

    def fake_popen(cmd, **kw):
        calls.append(cmd)
        if "erase" in cmd:
            return _FakeProcess(0)
        if "flash" in cmd:
            state["flash"] += 1
            if state["flash"] == 1:
                return _FakeProcess(1, (_LOCKED_SIGNATURE_LINE,))
            return _FakeProcess(1, ("still broken",))
        return _FakeProcess(0)  # reset is never reached on this path

    monkeypatch.setattr(subprocess, "Popen", fake_popen)

    hex_path = _send_hex(locked_client)
    logs, result = locked_client.request_stream(
        {"op": "flash", "uid": LOCAL_UID, "hex_path": hex_path}
    )

    assert result["success"] is False
    assert result["exit_code"] == 1
    assert _any_log_contains(logs, DEVICE_NAME, "no firmware"), logs
    # A failed flash never increments flash_count -- only a successful one.
    assert store.find(LOCAL_UID).flash_count == 0
    # The lock is still released unconditionally on failure -- this is
    # what daemon's flash-triggered re-probe hook watches for.
    assert locks.status(LOCAL_UID) is None


# ---------------------------------------------------------------------------
# send_hex staging: size cap, bad payloads, cleanup
# ---------------------------------------------------------------------------


def test_send_hex_oversized_payload_is_rejected(monkeypatch, remote_server):
    monkeypatch.setattr(remote_api_module, "MAX_HEX_PAYLOAD_BYTES", 10)
    srv = remote_server()
    client = _Client(srv.bound_port)

    resp = client.request(
        {"op": "send_hex", "data": base64.b64encode(b"x" * 100).decode()}
    )

    assert resp["ok"] is False
    assert resp["code"] == CODE_INVALID_REQUEST
    # The connection is still usable afterward -- a rejected send_hex
    # doesn't tear anything down (unlike a malformed binary stream frame).
    assert client.request({"op": "list"})["ok"] is True
    client.close()


def test_send_hex_invalid_base64_is_rejected(remote_server):
    srv = remote_server()
    client = _Client(srv.bound_port)

    resp = client.request({"op": "send_hex", "data": "not-valid-base64!!!"})

    assert resp["ok"] is False
    assert resp["code"] == CODE_INVALID_REQUEST
    client.close()


def test_send_hex_missing_data_is_rejected(remote_server):
    srv = remote_server()
    client = _Client(srv.bound_port)

    resp = client.request({"op": "send_hex"})

    assert resp["ok"] is False
    assert resp["code"] == CODE_INVALID_REQUEST
    client.close()


def test_staged_hex_file_survives_until_flash_consumes_it(remote_server):
    srv = remote_server()
    client = _Client(srv.bound_port)

    hex_path = _send_hex(client)

    assert os.path.exists(hex_path)
    client.close()


def test_connection_close_cleans_up_an_unflashed_staged_hex_file(remote_server):
    srv = remote_server()
    client = _Client(srv.bound_port)
    hex_path = _send_hex(client)
    assert os.path.exists(hex_path)

    client.close()  # never flashed

    _wait_until(lambda: not os.path.exists(hex_path))
