"""Tests for ``mbregistry run``'s assembly (ticket 009) -- an in-process
daemon (built via :func:`mbtools.registry.cli.assemble_daemon_and_api`,
the same function ``cmd_run`` itself calls) against a ``tmp_path`` store
and a real ``AF_UNIX`` socket, driven with ``FakeUSBSource``/a scripted
serial factory. No subprocess, no real hardware, per this ticket's own
Testing note.

Also covers the two concurrency fixes ticket 008 left as a documented gap
in ``docs/design/registry-api.md``'s "Known limitations", and that
``mbregistry run``'s assembly is where they're closed:

1. ``Daemon.run_once()``'s own thread and the API's per-connection
   threads now share one ``threading.RLock`` (``assemble_daemon_and_api``
   builds it and hands it to both).
2. A ``flash`` request no longer holds that shared lock for the whole
   streamed pyocd run (exercised indirectly here via a concurrent
   ``list``/``lock``/``unlock`` stress test; the flash-specific behavior
   itself is covered by ``tests/registry/api/test_api.py``'s own flash
   tests, unchanged by this ticket).
"""

from __future__ import annotations

import itertools
import json
import shutil
import socket
import tempfile
import threading
import time
from collections import deque

import pytest

from mbtools.common import DAPLINK_VID_PID, EXIT_OK, PortInfo
from mbtools.registry.cli import assemble_daemon_and_api, main
from mbtools.registry.store import STATE_CONNECTED, Store
from mbtools.testing.fakes import FakeSerial, FakeUSBSource

VID, PID_ = DAPLINK_VID_PID
ANNOUNCEMENT = "device NEZHA2 robot vevov 1198504156"


def _uid(tag: str) -> str:
    unique = (tag * 4)[:16]
    return "9900" + "0000" + "11112222" + unique + "77778888" + "6e052820"


def _port_info(uid: str, port: str = "/dev/ttyACM0") -> PortInfo:
    return PortInfo(uid=uid, port=port, vid=VID, pid=PID_)


class _ProbeScript:
    """Mirrors tests/registry/daemon/test_daemon.py's own helper of the
    same name: hands out a fresh FakeSerial per probe() call, scripted
    with the next queued announcement (or silence once exhausted)."""

    def __init__(self, announcements):
        self._queue = deque(announcements)

    def __call__(self, **kwargs):
        announcement = self._queue.popleft() if self._queue else None
        return FakeSerial(announcement=announcement, **kwargs)


class _RawClient:
    """A minimal newline-delimited-JSON client over a real AF_UNIX
    socket, mirroring tests/registry/api/test_api.py's own ``_Client``."""

    def __init__(self, socket_path):
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.connect(str(socket_path))
        self.rfile = self.sock.makefile("r", encoding="utf-8", newline="\n")
        self.wfile = self.sock.makefile("w", encoding="utf-8", newline="\n")

    def request(self, obj):
        self.wfile.write(json.dumps(obj))
        self.wfile.write("\n")
        self.wfile.flush()
        line = self.rfile.readline()
        if not line:
            raise ConnectionError("connection closed")
        return json.loads(line)

    def close(self):
        for f in (self.wfile, self.rfile):
            try:
                f.close()
            except OSError:
                pass
        self.sock.close()


@pytest.fixture
def socket_dir():
    d = tempfile.mkdtemp(prefix="mbregistry-cli-run-")
    yield d
    shutil.rmtree(d, ignore_errors=True)


def _wait_until(predicate, timeout=5.0, interval=0.01):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(interval)
    assert predicate(), "condition never became true within timeout"


# ---------------------------------------------------------------------------
# run-then-list smoke test
# ---------------------------------------------------------------------------


def test_run_then_list_smoke(tmp_path, socket_dir, capsys):
    """``mbregistry run``'s own pipeline, driven in a background thread,
    responds to ``mbregistry list`` from a second connection while it is
    still running -- this ticket's own acceptance criterion, verbatim.
    """
    uid = _uid("smoke111")
    socket_path = f"{socket_dir}/api.sock"
    store = Store(tmp_path / "devices.db")
    usbwatch = FakeUSBSource([[_port_info(uid)]])  # steady snapshot; repeats once exhausted
    script = _ProbeScript([ANNOUNCEMENT])

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
    try:
        _wait_until(
            lambda: (rec := store.get(uid)) is not None and rec.state == STATE_CONNECTED
        )

        with pytest.raises(SystemExit) as excinfo:
            main(["list", "--socket", socket_path])
        assert excinfo.value.code == EXIT_OK
        out = capsys.readouterr().out
        assert "vevov" in out
        assert "free" in out
    finally:
        stop_event.set()
        thread.join(timeout=5.0)
        assert not thread.is_alive()
        api.stop()
        store.close()


# ---------------------------------------------------------------------------
# concurrency: shared lock across daemon cycles and API calls
# ---------------------------------------------------------------------------


def test_concurrent_daemon_cycles_and_api_calls_do_not_race(tmp_path, socket_dir):
    """Hammers ``daemon.run_once()`` from one thread while several client
    threads concurrently call ``list``/``lock``/``unlock`` over the same
    shared ``Store``/``LockManager`` -- the exact scenario
    ``docs/design/registry-api.md``'s "Known limitations" flagged as
    unguarded before this ticket. Proves: no exception from any thread
    (no unguarded interleaving crash), no deadlock (every thread joins
    within the timeout), and the lock table ends up consistent (every
    lock a client acquired, it also released).
    """
    uids = [_uid(f"conc{i}xx") for i in range(4)]
    socket_path = f"{socket_dir}/api.sock"
    store = Store(tmp_path / "devices.db")
    usbwatch = FakeUSBSource(
        [[_port_info(uid, f"/dev/ttyACM{i}") for i, uid in enumerate(uids)]]
    )
    script = _ProbeScript([ANNOUNCEMENT] * len(uids))

    pid_counter = itertools.count(5000)
    pid_counter_lock = threading.Lock()

    def peer_pid_fn(_sock):
        with pid_counter_lock:
            return next(pid_counter)

    daemon, api = assemble_daemon_and_api(
        store=store,
        usbwatch=usbwatch,
        socket_path=socket_path,
        serial_factory=script,
        settle_s=0,
        probe_timeout_s=0.05,
        peer_pid_fn=peer_pid_fn,
    )
    api.start()

    errors: list[BaseException] = []
    errors_lock = threading.Lock()

    def daemon_worker():
        try:
            for _ in range(150):
                daemon.run_once()
        except BaseException as exc:  # noqa: BLE001 -- want to catch and report every failure
            with errors_lock:
                errors.append(exc)

    def client_worker(uid):
        client = None
        try:
            client = _RawClient(socket_path)
            for _ in range(25):
                resp = client.request({"op": "list"})
                assert resp["ok"] is True
                lock_resp = client.request({"op": "lock", "uid": uid, "kind": "serial"})
                if lock_resp["ok"]:
                    unlock_resp = client.request({"op": "unlock", "uid": uid})
                    assert unlock_resp["ok"] is True
                    assert unlock_resp["released"] is True
        except BaseException as exc:  # noqa: BLE001
            with errors_lock:
                errors.append(exc)
        finally:
            if client is not None:
                client.close()

    threads = [threading.Thread(target=daemon_worker, name="daemon")]
    threads += [
        threading.Thread(target=client_worker, args=(uid,), name=f"client-{i}")
        for i, uid in enumerate(uids)
    ]

    try:
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=15.0)
            assert not t.is_alive(), f"{t.name} did not finish -- possible deadlock"

        assert errors == [], f"worker thread(s) raised: {errors!r}"

        # Every lock a client acquired was also released by that same
        # client -- the shared lock made the acquire/release sequences
        # atomic with respect to the daemon's own concurrent bookkeeping.
        for uid in uids:
            assert daemon.locks.status(uid) is None
    finally:
        api.stop()
        store.close()
