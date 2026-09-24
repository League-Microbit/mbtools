"""Tests for mbtools.registry.api_windows -- the Win32 named-pipe query/
control protocol server (ticket 003) and its `_Win32PipeAPI` ctypes
transport.

Per the ticket's own Testing section ("fully fake-transport-based,
runnable on macOS/Linux CI"), every test here drives
`WindowsPipeAPIServer` against a scripted fake standing in for the
`ctypes.windll` calls (mirrors `mbtools.testing.fakes.FakeSerial`'s role
for pyserial) -- no test in this file touches a real Windows API, since
none exists to touch on this machine. The one thing genuinely
unverifiable without real Windows hardware -- whether the real
`_Win32PipeAPI`'s ctypes signatures/calling convention actually match a
real `kernel32.dll`/`advapi32.dll` -- is explicitly out of scope here;
see the module's own docstring "What is not proven" and ticket 006's
`windows-latest` CI job.
"""

from __future__ import annotations

import ctypes
import json
import sys
import threading
import time

import pytest

from mbtools.common import CODE_INVALID_REQUEST, CODE_LOCKED, CODE_NOT_FOUND
from mbtools.registry import api_windows
from mbtools.registry.identity import ProbeResult
from mbtools.registry.locks import KIND_FLASH, KIND_SERIAL, HolderRef, LockManager
from mbtools.registry.paths import default_pipe_name
from mbtools.registry.store import Store

UID = "9900" + "0000" + "11112222" + "3333444455556666" + "77778888" + "6e052820"
UID2 = "aa11" + "0000" + "11112222" + "3333444455556666" + "77778888" + "6e052820"
VID_PID = "0d28:0204"
PID_A = 2001
PID_B = 2002


def _local_holder(pid: int) -> HolderRef:
    return HolderRef(origin="local", ref=str(pid), pid=pid)


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
        ProbeResult(
            role="robot", common_name="Robot", device_name="alpha", serial="123", raw="DEVICE:alpha"
        ),
    )
    yield s
    s.close()


@pytest.fixture
def locks():
    return LockManager()


# ---------------------------------------------------------------------------
# test doubles
# ---------------------------------------------------------------------------


class _RecordingWFile:
    """The op-dispatch test double for a connection's write side: mirrors
    `_PipeWriter`'s own `write`/`flush` interface closely enough for
    `BaseAPIServer._write` to use unmodified, but decodes each flushed
    JSON message into `.messages` immediately, rather than encoding to
    bytes for a real pipe -- exactly the "fake pipe/connection double"
    the ticket's own Testing section calls for.
    """

    def __init__(self):
        self.messages: list[dict] = []
        self._buf = ""

    def write(self, text: str) -> None:
        self._buf += text

    def flush(self) -> None:
        if self._buf:
            self.messages.append(json.loads(self._buf))
            self._buf = ""


def _dispatch(srv, req, *, pid=PID_A, acquired_uids=None):
    """Drives `WindowsPipeAPIServer._dispatch_line` directly against a
    `_RecordingWFile`, the same op-level shape
    `tests/registry/api/test_api.py` exercises over a real socket --
    adapted here to this module's fake transport, per the ticket's own
    acceptance criterion.
    """
    acquired_uids = acquired_uids if acquired_uids is not None else set()
    wfile = _RecordingWFile()
    holder = _local_holder(pid)
    srv._dispatch_line(json.dumps(req), pid, holder, acquired_uids, wfile)
    assert len(wfile.messages) == 1
    return wfile.messages[0], acquired_uids


@pytest.fixture
def make_server(store, locks):
    def _make(*, peer_pid_fn=None, is_pid_alive_fn=None, win32=None, pipe_name=None):
        return api_windows.WindowsPipeAPIServer(
            pipe_name=pipe_name,
            store=store,
            locks=locks,
            peer_pid_fn=peer_pid_fn,
            is_pid_alive_fn=is_pid_alive_fn,
            win32=win32 if win32 is not None else _NullWin32(),
        )

    return _make


class _NullWin32:
    """A `win32=` double that satisfies construction but is never
    actually called by the op-dispatch tests below (they call
    `_dispatch_line` directly, bypassing the transport entirely).
    `__init__` binds `self._peer_pid_fn` to
    `self._win32.get_client_process_id` by default, so this still needs
    the attribute to exist even though nothing here ever calls it --
    keeps intent explicit rather than passing `None` and relying on the
    "construct a real `_Win32PipeAPI`" default, which would be a lie on
    this platform.
    """

    def get_client_process_id(self, handle):  # pragma: no cover - never called
        raise AssertionError("op-dispatch tests never exercise the real transport")


# ---------------------------------------------------------------------------
# AC1: import safety
# ---------------------------------------------------------------------------


def test_module_imports_cleanly_off_windows():
    """Mirrors the module's own docstring claim -- already proven by this
    file's own top-level `from mbtools.registry import api_windows`
    succeeding, but asserted explicitly (and asserts the guard actually
    took the off-Windows branch, not just that some import happened to
    work) so a future edit that breaks the guard fails loudly here
    rather than only in a downstream module's own import.
    """
    assert sys.platform != "win32"  # this suite always runs off real Windows
    assert api_windows._kernel32 is None
    assert api_windows._advapi32 is None


def test_no_pywin32_import():
    """AC: 'No pywin32 (or any other new third-party package) import
    appears anywhere in this module -- ctypes/stdlib only.' Checked by
    parsing the module's *actual import statements* via `ast` (not a
    substring search over the whole source -- this module's own
    identifiers, e.g. `_Win32PipeAPI`, lowercase to something containing
    "win32pipe" and would false-positive a naive text search) -- every
    top-level module named by an `import`/`from ... import` statement
    must be `ctypes` (with or without a `.wintypes` submodule), a
    stdlib module already used elsewhere in this package, or another
    `mbtools` module.
    """
    import ast
    import inspect

    allowed_stdlib = {
        "__future__",
        "ctypes",
        "json",
        "logging",
        "sys",
        "threading",
        "time",
        "typing",
    }
    source = inspect.getsource(api_windows)
    tree = ast.parse(source)
    imported_roots: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                imported_roots.add(alias.name.split(".")[0])
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported_roots.add(node.module.split(".")[0])

    for root in imported_roots:
        assert root == "mbtools" or root in allowed_stdlib, (
            f"unexpected non-stdlib, non-mbtools import root: {root!r}"
        )
    # Belt-and-suspenders: pywin32's own top-level package names, in
    # case a future edit imports one under an alias that still resolves
    # to `win32api`/`pywintypes`/etc. as its root module.
    forbidden_roots = {"win32api", "win32pipe", "win32file", "win32security", "pywintypes", "win32con"}
    assert imported_roots.isdisjoint(forbidden_roots)


def test_pipe_name_sourced_from_registry_paths():
    """AC: 'Pipe name is sourced from registry.paths.default_pipe_name(),
    not a literal in this module.'"""
    assert api_windows.DEFAULT_PIPE_NAME == default_pipe_name()


def test_pipe_name_overridable(make_server):
    srv = make_server(pipe_name=r"\\.\pipe\mbregistry-custom")
    assert srv.pipe_name == r"\\.\pipe\mbregistry-custom"


def test_pipe_name_defaults_when_omitted(make_server):
    srv = make_server()
    assert srv.pipe_name == default_pipe_name()


# ---------------------------------------------------------------------------
# AC2/AC3: op dispatch + holder derivation, against the fake transport
# ---------------------------------------------------------------------------


def test_list_includes_every_device_with_lock_status_folded_in(make_server, locks):
    srv = make_server()
    locks.acquire(UID, KIND_SERIAL, _local_holder(PID_A))

    resp, _ = _dispatch(srv, {"op": "list"})

    assert resp["ok"] is True
    by_uid = {d["uid"]: d for d in resp["devices"]}
    assert set(by_uid) == {UID, UID2}
    assert by_uid[UID]["lock_kind"] == KIND_SERIAL
    assert by_uid[UID]["lock_pid"] == PID_A
    assert by_uid[UID2]["lock_kind"] is None


def test_find_by_uid_short_uid_and_device_name(make_server):
    srv = make_server()
    short_uid = UID[16:24]

    for token in (UID, short_uid, "alpha", "ALPHA"):
        resp, _ = _dispatch(srv, {"op": "find", "uid": token})
        assert resp["ok"] is True, token
        assert resp["device"]["uid"] == UID, token


def test_find_unknown_device_returns_not_found(make_server):
    srv = make_server()

    resp, _ = _dispatch(srv, {"op": "find", "uid": "does-not-exist"})

    assert resp == {
        "ok": False,
        "code": CODE_NOT_FOUND,
        "error": "no such device: 'does-not-exist'",
    }


def test_lock_then_unlock_round_trip(make_server):
    srv = make_server()

    lock_resp, acquired = _dispatch(srv, {"op": "lock", "uid": UID, "kind": KIND_SERIAL})
    assert lock_resp == {"ok": True}
    assert acquired == {UID}

    unlock_resp, acquired = _dispatch(srv, {"op": "unlock", "uid": UID}, acquired_uids=acquired)
    assert unlock_resp == {"ok": True, "released": True}
    assert acquired == set()


def test_lock_conflict_reports_existing_holder(make_server, locks):
    srv = make_server()
    locks.acquire(UID, KIND_SERIAL, _local_holder(PID_A))

    resp, _ = _dispatch(srv, {"op": "lock", "uid": UID, "kind": KIND_SERIAL}, pid=PID_B)

    assert resp["ok"] is False
    assert resp["code"] == CODE_LOCKED
    assert resp["holder"] == {"kind": KIND_SERIAL, "pid": PID_A}


def test_mark_flashed_requires_flash_lock_held_by_this_connection(make_server, locks):
    srv = make_server()
    locks.acquire(UID, KIND_FLASH, _local_holder(PID_A))

    resp, _ = _dispatch(srv, {"op": "mark_flashed", "uid": UID}, pid=PID_A)

    assert resp == {"ok": True}


def test_mark_flashed_without_lock_is_not_locked_error(make_server):
    srv = make_server()

    resp, _ = _dispatch(srv, {"op": "mark_flashed", "uid": UID})

    assert resp["ok"] is False
    assert resp["code"] == "not_locked"


def test_flash_op_is_not_supported_over_the_named_pipe(make_server):
    """This module deliberately does not wire `FlashOp` in (see module
    docstring's 'No protocol/op logic of its own') -- a `flash` request
    gets the same response as any other unrecognized op, not a crash."""
    srv = make_server()

    resp, _ = _dispatch(srv, {"op": "flash", "uid": UID, "hex_path": "/tmp/x.hex"})

    assert resp["ok"] is False
    assert resp["code"] == CODE_INVALID_REQUEST


def test_unknown_op_is_invalid_request(make_server):
    srv = make_server()
    resp, _ = _dispatch(srv, {"op": "no_such_op"})
    assert resp["ok"] is False
    assert resp["code"] == CODE_INVALID_REQUEST


def test_malformed_json_is_invalid_request(make_server):
    srv = make_server()
    wfile = _RecordingWFile()
    srv._dispatch_line("not json{{{", PID_A, _local_holder(PID_A), set(), wfile)
    assert wfile.messages[0]["ok"] is False
    assert wfile.messages[0]["code"] == CODE_INVALID_REQUEST


def test_names_registry_ops_round_trip(make_server):
    """Not explicitly named in the ticket's acceptance criteria, but
    inherited for free from `BaseAPIServer` exactly like the five named
    ops are -- proves the dispatch table wires all of it, not just the
    five explicitly-named ops."""
    srv = make_server()

    set_resp, _ = _dispatch(srv, {"op": "names_set", "name": "tovez", "channel": 5, "group": 1})
    assert set_resp["ok"] is True

    get_resp, _ = _dispatch(srv, {"op": "names_get", "name": "tovez"})
    assert get_resp["ok"] is True
    assert get_resp["entry"]["channel"] == 5

    list_resp, _ = _dispatch(srv, {"op": "names_list"})
    assert any(e["name"] == "tovez" for e in list_resp["entries"])

    clear_resp, _ = _dispatch(srv, {"op": "names_clear", "name": "tovez"})
    assert clear_resp["ok"] is True


def test_holder_for_connection_uses_injected_peer_pid_fn_not_a_client_value(make_server):
    """AC: '_holder_for_connection derives a HolderRef from an injected
    fake client-PID lookup (never a client-supplied PID), matching
    api.py's peer_pid_fn injection pattern.'"""
    seen_handles = []

    def fake_peer_pid_fn(handle):
        seen_handles.append(handle)
        return PID_B

    srv = make_server(peer_pid_fn=fake_peer_pid_fn)

    holder = srv._holder_for_connection(handle=12345)

    assert seen_handles == [12345]
    assert holder == HolderRef(origin="local", ref=str(PID_B), pid=PID_B)


# ---------------------------------------------------------------------------
# AC4: security descriptor
# ---------------------------------------------------------------------------


class _RecordingAdvapi32:
    def __init__(self, *, ok=True):
        self.sddl_calls: list[str] = []
        self._ok = ok

    def ConvertStringSecurityDescriptorToSecurityDescriptorW(
        self, sddl, revision, sd_ptr_out, size_out
    ):
        self.sddl_calls.append(sddl)
        if self._ok:
            sd_ptr_out.contents.value = 0xDEADBEEF
        return 1 if self._ok else 0


class _RecordingKernel32:
    def __init__(self):
        self.create_named_pipe_calls: list = []
        self._next_handle = 100

    def CreateNamedPipeW(
        self, name, open_mode, pipe_mode, max_instances, out_buf, in_buf, timeout, security_attributes
    ):
        self.create_named_pipe_calls.append((name, security_attributes.contents))
        handle = self._next_handle
        self._next_handle += 1
        return handle


def test_pipe_security_descriptor_sddl_restricts_to_owner_and_administrators():
    """What *is* provable without Windows hardware (see module
    docstring): the SDDL string itself is a protected DACL (`D:P` -- no
    inherited ACEs) granting access only to `BA` (built-in
    Administrators) and `OW` (owner rights) -- no `WD`/`AU`
    (Everyone/Authenticated Users) ACE, i.e. not the OS default.
    """
    sddl = api_windows._PIPE_SECURITY_DESCRIPTOR_SDDL
    assert sddl.startswith("D:P")
    assert "BA" in sddl
    assert "OW" in sddl
    assert "WD" not in sddl  # Everyone
    assert "AU" not in sddl  # Authenticated Users


def test_create_named_pipe_passes_explicit_non_null_security_attributes(monkeypatch):
    """AC: the pipe's security descriptor is explicitly set -- verified
    by asserting the ctypes call the module makes includes a non-null,
    restrictive security-attributes argument. Exercises
    `_Win32PipeAPI`'s own real ctypes-calling code (not a fake
    `_Win32PipeAPI` substitute) by monkeypatching the module-level
    `_kernel32`/`_advapi32` names -- mirrors `usbwatch.py`'s own
    `monkeypatch.setattr(usbwatch._list_ports, ...)` precedent (see
    module docstring's 'Import-safety').
    """
    fake_kernel32 = _RecordingKernel32()
    fake_advapi32 = _RecordingAdvapi32()
    monkeypatch.setattr(api_windows, "_kernel32", fake_kernel32)
    monkeypatch.setattr(api_windows, "_advapi32", fake_advapi32)

    win32 = api_windows._Win32PipeAPI()
    handle = win32.create_named_pipe(r"\\.\pipe\mbregistry-test")

    assert handle == 100
    assert fake_advapi32.sddl_calls == [api_windows._PIPE_SECURITY_DESCRIPTOR_SDDL]
    assert len(fake_kernel32.create_named_pipe_calls) == 1
    name, sa = fake_kernel32.create_named_pipe_calls[0]
    assert name == r"\\.\pipe\mbregistry-test"
    assert sa.lpSecurityDescriptor is not None
    assert sa.lpSecurityDescriptor != 0
    assert sa.bInheritHandle == 0


def test_create_named_pipe_raises_if_security_descriptor_conversion_fails(monkeypatch):
    monkeypatch.setattr(api_windows, "_kernel32", _RecordingKernel32())
    monkeypatch.setattr(api_windows, "_advapi32", _RecordingAdvapi32(ok=False))

    win32 = api_windows._Win32PipeAPI()
    with pytest.raises(OSError):
        win32.create_named_pipe(r"\\.\pipe\mbregistry-test")


# ---------------------------------------------------------------------------
# line framing (_PipeLineReader / _PipeWriter)
# ---------------------------------------------------------------------------


class _ScriptedWin32ForFraming:
    """A minimal `read_file`/`write_file` double -- feeds `chunks` out
    one `read_file` call at a time (simulating a real pipe's own
    chunking, which need not align with line boundaries), and records
    every `write_file` call."""

    def __init__(self, chunks):
        self._chunks = list(chunks)
        self.writes: list[bytes] = []

    def read_file(self, handle, size):
        if not self._chunks:
            return b""
        return self._chunks.pop(0)

    def write_file(self, handle, data):
        self.writes.append(data)


def test_pipe_line_reader_reassembles_lines_split_across_reads():
    win32 = _ScriptedWin32ForFraming([b'{"op":', b'"list"}\n', b'{"op":"lock"}\n'])
    reader = api_windows._PipeLineReader(win32, handle=1)

    lines = list(reader)

    assert lines == ['{"op":"list"}\n', '{"op":"lock"}\n']


def test_pipe_line_reader_returns_partial_trailing_data_on_eof():
    win32 = _ScriptedWin32ForFraming([b"no newline at all"])
    reader = api_windows._PipeLineReader(win32, handle=1)

    assert reader.readline() == "no newline at all"
    assert reader.readline() == ""


def test_pipe_writer_batches_write_write_flush_into_one_write_file_call():
    win32 = _ScriptedWin32ForFraming([])
    writer = api_windows._PipeWriter(win32, handle=1)

    writer.write('{"ok": true}')
    writer.write("\n")
    writer.flush()

    assert win32.writes == [b'{"ok": true}\n']


# ---------------------------------------------------------------------------
# full lifecycle: start/accept/dispatch/stop against a scripted fake
# ---------------------------------------------------------------------------


class _ScriptedLifecycleWin32:
    """A scripted double covering the whole
    `create_named_pipe`/`connect_named_pipe`/`read_file`/`write_file`/
    `disconnect_named_pipe`/`close_handle`/`get_client_process_id`/
    `cancel_pending_connect` surface `WindowsPipeAPIServer` uses
    end-to-end -- mirrors `FakeSerial`'s role for pyserial identity
    probing, but for this module's named-pipe transport.

    Serves exactly one scripted client connection (``request_lines``,
    attributed to ``pid``) on the first `create_named_pipe`/
    `connect_named_pipe` pair; every subsequent pipe instance's
    `connect_named_pipe` blocks (mirroring a real pending
    `ConnectNamedPipe`) until `cancel_pending_connect` is called, at
    which point it raises -- exactly how `_accept_loop`'s own
    `stop()`-triggered unwind is meant to work (ticket 006: this used
    to model `close_handle` as the unblocking call, which is what real
    Windows does *not* reliably honor for a synchronous pending
    `ConnectNamedPipe` -- see `_Win32PipeAPI.cancel_pending_connect`'s
    own docstring for the real deadlock this replaced).
    """

    def __init__(self, pid, request_lines):
        self._pid = pid
        self._inbound = "".join(
            line if line.endswith("\n") else line + "\n" for line in request_lines
        ).encode("utf-8")
        self._served = False
        self._next_handle = 1
        self._state: dict[int, dict] = {}
        self._cancelled = threading.Event()
        self.written: list[bytes] = []
        self.create_calls: list[str] = []
        self.disconnect_calls: list[int] = []
        self.close_calls: list[int] = []
        self.cancel_calls: list[int] = []

    def create_named_pipe(self, name):
        self.create_calls.append(name)
        handle = self._next_handle
        self._next_handle += 1
        self._state[handle] = {"inbound": b""}
        return handle

    def connect_named_pipe(self, handle):
        if not self._served:
            self._served = True
            self._state[handle]["inbound"] = self._inbound
            return
        if not self._cancelled.wait(timeout=5.0):
            raise TimeoutError(
                "test: stop() never cancelled the pending pipe handle"
            )
        raise OSError("api_windows: pipe connect cancelled")

    def read_file(self, handle, size):
        state = self._state[handle]
        data, state["inbound"] = state["inbound"][:size], state["inbound"][size:]
        return data

    def write_file(self, handle, data):
        self.written.append(data)

    def disconnect_named_pipe(self, handle):
        self.disconnect_calls.append(handle)

    def close_handle(self, handle):
        self.close_calls.append(handle)

    def cancel_pending_connect(self, thread_id):
        self.cancel_calls.append(thread_id)
        self._cancelled.set()

    def get_client_process_id(self, handle):
        return self._pid


def _wait_until(predicate, timeout=2.0, interval=0.01):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(interval)
    assert predicate(), "condition never became true within timeout"


def test_end_to_end_start_accept_dispatch_stop(store, locks):
    win32 = _ScriptedLifecycleWin32(PID_A, [json.dumps({"op": "list"})])
    srv = api_windows.WindowsPipeAPIServer(
        store=store, locks=locks, win32=win32, sweep_interval_s=100.0
    )

    srv.start()
    try:
        _wait_until(lambda: len(win32.written) >= 1)
    finally:
        srv.stop()

    assert win32.create_calls[0] == default_pipe_name()
    assert len(win32.written) == 1
    resp = json.loads(win32.written[0].decode("utf-8"))
    assert resp["ok"] is True
    assert {d["uid"] for d in resp["devices"]} == {UID, UID2}

    assert win32.disconnect_calls == [1]  # the one served connection's own handle
    assert srv._conn_threads
    assert all(not t.is_alive() for t in srv._conn_threads)
    assert not srv._accept_thread.is_alive()
    assert not srv._sweep_thread.is_alive()


def test_connection_close_releases_locks_it_acquired(store, locks):
    req = json.dumps({"op": "lock", "uid": UID, "kind": KIND_SERIAL}) + "\n"
    win32 = _ScriptedLifecycleWin32(PID_A, [req])
    srv = api_windows.WindowsPipeAPIServer(
        store=store, locks=locks, win32=win32, sweep_interval_s=100.0
    )

    srv.start()
    try:
        _wait_until(lambda: len(win32.written) >= 1)
        _wait_until(lambda: win32.disconnect_calls == [1])
    finally:
        srv.stop()

    assert locks.status(UID) is None  # released when the connection wound down
