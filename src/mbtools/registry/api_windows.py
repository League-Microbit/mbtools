"""mbtools.registry.api_windows — the Windows counterpart to
``mbtools.registry.api.RegistryAPIServer``: the same local device-query
API (``list``/``find``/``lock``/``unlock``/``mark_flashed``, plus the
name-registry ops ``registry._api_base.BaseAPIServer`` also provides)
over a Win32 named pipe instead of a Unix domain socket (spec §3.5 /
brief §9.2, this sprint's ticket 003).

Per sprint.md Decision 1 ("standard library only, no ``pywin32``"),
every Windows API call here goes through ``ctypes.windll`` directly —
``CreateNamedPipeW``/``ConnectNamedPipe``/``ReadFile``/``WriteFile``/
``DisconnectNamedPipe``/``CloseHandle`` for the pipe transport itself,
plus ``GetNamedPipeClientProcessId`` (the named-pipe counterpart to
``registry.api``'s ``SO_PEERCRED``/``LOCAL_PEERPID``-based
``default_peer_pid`` — same "kernel-verified, never a client-supplied
PID" property) and ``ConvertStringSecurityDescriptorToSecurityDescriptorW``
(advapi32, SDDL) to build an explicit, restrictive security descriptor
for the pipe rather than shipping ``CreateNamedPipeW``'s broader OS
default (sprint.md Open Questions). ``OpenProcess``/``GetExitCodeProcess``
back a Windows-appropriate liveness check for the periodic lock sweep
(``api.default_is_pid_alive``'s POSIX ``os.kill(pid, 0)`` has no Windows
equivalent — Python's ``os.kill`` on Windows does not support signal 0
as a liveness probe) — not explicitly named in the ticket's own
Approach, but needed for this module's sweep to mean anything real on
Windows; flagged here as an implementer addition, same "needs real-
Windows confirmation" caveat as everything else in this module.

**Import-safety** (mirrors ``usbwatch.py``/``identity.py``'s existing
``pyserial`` guard): this module must import cleanly on macOS/Linux so
the test suite can exercise its op-dispatch logic and so ``registry.cli``
can import it unconditionally on every platform. ``ctypes.windll`` does
not exist off Windows (``ctypes.wintypes`` does — it needs no DLL, only
type aliases — so it is imported unconditionally below), so every real
``kernel32``/``advapi32`` binding is guarded by ``sys.platform ==
"win32"`` at import time; off Windows, the module-level ``_kernel32``/
``_advapi32`` names stay ``None`` and the real transport
(``_Win32PipeAPI``) is simply never constructed in production (tests
inject a fake instead — see ``tests/registry/api_windows/
test_api_windows.py``). ``_kernel32``/``_advapi32`` are looked up
through these two module-level names — never a fresh
``ctypes.windll.kernel32`` lookup inline at each call site — specifically
so a test can monkeypatch them with fakes even on macOS/Linux and
exercise ``_Win32PipeAPI``'s own ctypes-calling code, including the
security-descriptor construction, without any real Windows API ever
running (mirrors ``usbwatch.py``'s own
``monkeypatch.setattr(usbwatch._list_ports, ...)`` precedent).

**No protocol/op logic of its own**: every op
(``list``/``find``/``lock``/``unlock``/``mark_flashed``, plus the
name-registry ops) dispatches through
``mbtools.registry._api_base.BaseAPIServer``, exactly as
``registry.api.RegistryAPIServer`` does — ``WindowsPipeAPIServer``
supplies only ``_holder_for_connection`` and the pipe transport/
threading mechanics, mirroring ``RegistryAPIServer``'s own division of
labor. ``flash`` is deliberately not exposed here: it is
``api.RegistryAPIServer``'s own addition (ticket 008's remote-flash
territory, ``FlashOp``-backed), not part of ``BaseAPIServer``, and this
ticket's own scope statement ("adds no protocol/op logic of its own")
does not ask for a ``FlashOp`` to be wired into this module. A request
with ``"op": "flash"`` gets the same ``invalid_request`` response as any
other unrecognized op — see :meth:`WindowsPipeAPIServer._dispatch_line`.

**Security descriptor (sprint.md Open Questions)**: ``CreateNamedPipeW``'s
default security descriptor is broader than the Unix socket's
filesystem-permission-based access control, so this module always
passes an explicit ``SECURITY_ATTRIBUTES`` built from a fixed SDDL
string (``_PIPE_SECURITY_DESCRIPTOR_SDDL``, owner + built-in
Administrators, no inherited ACEs) via
``ConvertStringSecurityDescriptorToSecurityDescriptorW`` — never the OS
default.

**What this proves without Windows hardware**: the SDDL string itself
restricts to owner + local Administrators (a static, inspectable fact —
see ``test_pipe_security_descriptor_sddl_restricts_to_owner_and_administrators``
in the test module), and ``_Win32PipeAPI.create_named_pipe`` always
builds and passes a non-null ``SECURITY_ATTRIBUTES`` pointer to
``CreateNamedPipeW`` rather than ``None`` (proven against a fake
``kernel32``/``advapi32`` double — see
``test_create_named_pipe_passes_explicit_non_null_security_attributes``).
**What is not proven**: that
``ConvertStringSecurityDescriptorToSecurityDescriptorW`` actually parses
this SDDL string into the DACL a real Windows kernel enforces the way
this docstring claims, that ``CreateNamedPipeW`` actually honors it, or
that the ``ctypes`` struct layouts/calling convention declared below
match a real ``kernel32.dll``/``advapi32.dll`` exactly — that requires
the ``windows-latest`` CI job (ticket 006), the first *real*
verification this code gets; hardware acceptance (ticket 010)
explicitly does not cover it (no Windows hardware exists for this
project).
"""

from __future__ import annotations

import ctypes
import ctypes.wintypes as _wintypes
import json
import logging
import sys
import threading
from typing import Any, Callable

from mbtools.common import CODE_INVALID_REQUEST
from mbtools.registry._api_base import BaseAPIServer, _error
from mbtools.registry.locks import HolderRef, LockManager
from mbtools.registry.paths import default_pipe_name
from mbtools.registry.store import Entry, Store

__all__ = [
    "WindowsPipeAPIServer",
    "default_is_pid_alive_windows",
    "DEFAULT_PIPE_NAME",
    "DEFAULT_SWEEP_INTERVAL_S",
]

logger = logging.getLogger(__name__)

#: Production default named-pipe name — sourced from ``registry.paths
#: .default_pipe_name`` (ticket 002), never a literal here, per this
#: ticket's own acceptance criteria. See that function's own ASSUMPTION
#: note.
DEFAULT_PIPE_NAME = default_pipe_name()

#: Mirrors ``api.DEFAULT_SWEEP_INTERVAL_S`` exactly — same liveness-sweep
#: cadence, same rationale (see that module's own docstring).
DEFAULT_SWEEP_INTERVAL_S = 5.0

#: An explicit, restrictive DACL for the named pipe (sprint.md Open
#: Questions — "do not ship the OS default unexamined"): ``"D:P"`` is a
#: protected DACL (no inherited ACEs), granting Generic-All only to
#: BUILTIN\\Administrators (``"BA"``) and the object's own Owner Rights
#: (``"OW"``) — no Everyone/Authenticated-Users ACE at all. This is this
#: module's ASSUMPTION for "owner + local administrators, or
#: equivalent" (the ticket's own words); it is not verifiable against a
#: real ACL without Windows hardware — see the module docstring's "What
#: is not proven".
_PIPE_SECURITY_DESCRIPTOR_SDDL = "D:P(A;;GA;;;BA)(A;;GA;;;OW)"

_PIPE_BUFFER_SIZE = 4096

# Win32 constants restated here since they are simple integers, not
# something `ctypes` provides. Byte-mode, not message-mode: this
# protocol frames on '\n' itself (newline-delimited JSON, same as the
# Unix-socket transport), so the pipe should behave like a plain byte
# stream, not add a second, message-boundary framing underneath it.
_PIPE_ACCESS_DUPLEX = 0x00000003
_PIPE_TYPE_BYTE = 0x00000000
_PIPE_READMODE_BYTE = 0x00000000
_PIPE_WAIT = 0x00000000
_PIPE_UNLIMITED_INSTANCES = 255
_ERROR_PIPE_CONNECTED = 535
_PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
_STILL_ACTIVE = 259

#: ``INVALID_HANDLE_VALUE``, computed via plain ``ctypes`` (no ``windll``
#: needed) so it is available off Windows too — ``c_void_p(-1)``'s
#: unsigned representation, not literal ``-1`` (a raw ``HANDLE`` compare
#: against ``-1`` would never match what ``ctypes`` actually returns).
_INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value


# ---------------------------------------------------------------------------
# ctypes bindings — guarded, import-safe off Windows (see module docstring)
# ---------------------------------------------------------------------------

if sys.platform == "win32":  # pragma: no cover - real binding only exists on Windows; exercised by windows-latest CI (ticket 006), not this suite
    _kernel32 = ctypes.windll.kernel32
    _advapi32 = ctypes.windll.advapi32

    _kernel32.CreateNamedPipeW.argtypes = [
        _wintypes.LPCWSTR,
        _wintypes.DWORD,
        _wintypes.DWORD,
        _wintypes.DWORD,
        _wintypes.DWORD,
        _wintypes.DWORD,
        _wintypes.DWORD,
        ctypes.c_void_p,
    ]
    _kernel32.CreateNamedPipeW.restype = _wintypes.HANDLE

    _kernel32.ConnectNamedPipe.argtypes = [_wintypes.HANDLE, ctypes.c_void_p]
    _kernel32.ConnectNamedPipe.restype = _wintypes.BOOL

    _kernel32.GetLastError.argtypes = []
    _kernel32.GetLastError.restype = _wintypes.DWORD

    _kernel32.ReadFile.argtypes = [
        _wintypes.HANDLE,
        ctypes.c_void_p,
        _wintypes.DWORD,
        ctypes.POINTER(_wintypes.DWORD),
        ctypes.c_void_p,
    ]
    _kernel32.ReadFile.restype = _wintypes.BOOL

    _kernel32.WriteFile.argtypes = [
        _wintypes.HANDLE,
        ctypes.c_void_p,
        _wintypes.DWORD,
        ctypes.POINTER(_wintypes.DWORD),
        ctypes.c_void_p,
    ]
    _kernel32.WriteFile.restype = _wintypes.BOOL

    _kernel32.DisconnectNamedPipe.argtypes = [_wintypes.HANDLE]
    _kernel32.DisconnectNamedPipe.restype = _wintypes.BOOL

    _kernel32.CloseHandle.argtypes = [_wintypes.HANDLE]
    _kernel32.CloseHandle.restype = _wintypes.BOOL

    _kernel32.GetNamedPipeClientProcessId.argtypes = [
        _wintypes.HANDLE,
        ctypes.POINTER(_wintypes.ULONG),
    ]
    _kernel32.GetNamedPipeClientProcessId.restype = _wintypes.BOOL

    _kernel32.OpenProcess.argtypes = [_wintypes.DWORD, _wintypes.BOOL, _wintypes.DWORD]
    _kernel32.OpenProcess.restype = _wintypes.HANDLE

    _kernel32.GetExitCodeProcess.argtypes = [
        _wintypes.HANDLE,
        ctypes.POINTER(_wintypes.DWORD),
    ]
    _kernel32.GetExitCodeProcess.restype = _wintypes.BOOL

    _advapi32.ConvertStringSecurityDescriptorToSecurityDescriptorW.argtypes = [
        _wintypes.LPCWSTR,
        _wintypes.DWORD,
        ctypes.POINTER(ctypes.c_void_p),
        ctypes.POINTER(_wintypes.ULONG),
    ]
    _advapi32.ConvertStringSecurityDescriptorToSecurityDescriptorW.restype = _wintypes.BOOL
else:
    _kernel32 = None
    _advapi32 = None


class _SECURITY_ATTRIBUTES(ctypes.Structure):
    """The Win32 ``SECURITY_ATTRIBUTES`` struct layout — built from
    plain ``ctypes.wintypes`` aliases, so (unlike the DLL bindings above)
    this class exists identically on every platform; only *constructing
    a real one* (:meth:`_Win32PipeAPI._security_attributes`) needs
    ``_advapi32``.
    """

    _fields_ = [
        ("nLength", _wintypes.DWORD),
        ("lpSecurityDescriptor", ctypes.c_void_p),
        ("bInheritHandle", _wintypes.BOOL),
    ]


# ---------------------------------------------------------------------------
# real transport — only ever constructed on real Windows
# ---------------------------------------------------------------------------


class _Win32PipeAPI:
    """The real, ``ctypes``-backed named-pipe transport — only ever
    constructed in production on real Windows
    (:class:`WindowsPipeAPIServer`'s own default, ``win32=None``);
    every method here assumes ``_kernel32``/``_advapi32`` are bound
    (non-``None``), which is only true when ``sys.platform == "win32"``
    at import time (see module docstring). Tests exercise this class's
    own ctypes-calling logic — including the security-descriptor
    construction — by monkeypatching the module-level ``_kernel32``/
    ``_advapi32`` names with fakes, even on macOS/Linux; no test
    constructs this class against a real Windows kernel, since no
    Windows hardware exists for this project (see module docstring's
    "What is not proven"). Op-dispatch tests instead inject a
    ``_FakePipeAPI`` double implementing this same method surface,
    mirroring how ``FakeSerial`` stands in for pyserial elsewhere in
    this package.
    """

    def _security_attributes(self) -> _SECURITY_ATTRIBUTES:
        sd_ptr = ctypes.c_void_p()
        ok = _advapi32.ConvertStringSecurityDescriptorToSecurityDescriptorW(
            _PIPE_SECURITY_DESCRIPTOR_SDDL, 1, ctypes.pointer(sd_ptr), None
        )
        if not ok:
            raise OSError(
                "api_windows: ConvertStringSecurityDescriptorToSecurityDescriptorW "
                "failed to build the pipe's security descriptor"
            )
        sa = _SECURITY_ATTRIBUTES()
        sa.nLength = ctypes.sizeof(_SECURITY_ATTRIBUTES)
        sa.lpSecurityDescriptor = sd_ptr
        sa.bInheritHandle = False
        return sa

    def create_named_pipe(self, name: str) -> int:
        sa = self._security_attributes()
        handle = _kernel32.CreateNamedPipeW(
            name,
            _PIPE_ACCESS_DUPLEX,
            _PIPE_TYPE_BYTE | _PIPE_READMODE_BYTE | _PIPE_WAIT,
            _PIPE_UNLIMITED_INSTANCES,
            _PIPE_BUFFER_SIZE,
            _PIPE_BUFFER_SIZE,
            0,
            ctypes.pointer(sa),
        )
        if handle is None or handle == _INVALID_HANDLE_VALUE:
            raise OSError(f"api_windows: CreateNamedPipeW failed for {name!r}")
        return handle

    def connect_named_pipe(self, handle: int) -> None:
        ok = _kernel32.ConnectNamedPipe(handle, None)
        if not ok and _kernel32.GetLastError() != _ERROR_PIPE_CONNECTED:
            raise OSError("api_windows: ConnectNamedPipe failed")

    def read_file(self, handle: int, size: int) -> bytes:
        buf = ctypes.create_string_buffer(size)
        bytes_read = _wintypes.DWORD(0)
        ok = _kernel32.ReadFile(handle, buf, size, ctypes.byref(bytes_read), None)
        if not ok:
            return b""  # broken pipe / client disconnected -- treated as EOF
        return buf.raw[: bytes_read.value]

    def write_file(self, handle: int, data: bytes) -> None:
        bytes_written = _wintypes.DWORD(0)
        ok = _kernel32.WriteFile(handle, data, len(data), ctypes.byref(bytes_written), None)
        if not ok:
            raise OSError("api_windows: WriteFile failed")

    def disconnect_named_pipe(self, handle: int) -> None:
        _kernel32.DisconnectNamedPipe(handle)

    def close_handle(self, handle: int) -> None:
        _kernel32.CloseHandle(handle)

    def get_client_process_id(self, handle: int) -> int:
        """The named-pipe counterpart to ``api.default_peer_pid``:
        kernel-verified, never a client-supplied value (see module
        docstring). Raises :class:`OSError` on failure, mirroring
        ``default_peer_pid``'s own contract.
        """
        pid = _wintypes.ULONG(0)
        ok = _kernel32.GetNamedPipeClientProcessId(handle, ctypes.byref(pid))
        if not ok:
            raise OSError("api_windows: GetNamedPipeClientProcessId failed")
        return pid.value


def default_is_pid_alive_windows(pid: int) -> bool:
    """The production ``is_pid_alive_fn``: ``OpenProcess``/
    ``GetExitCodeProcess``-based liveness check — the named-pipe
    counterpart to ``api.default_is_pid_alive``'s POSIX
    ``os.kill(pid, 0)`` (not usable here — Python's ``os.kill`` on
    Windows does not support signal 0 as a liveness probe). Mirrors that
    function's own contract as closely as the two platforms allow: a pid
    this process can open for query-only access and that reports
    ``STILL_ACTIVE`` is alive; anything else (open failed, or the
    process already exited) is not.
    """
    if pid <= 0:
        return False
    handle = _kernel32.OpenProcess(_PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not handle:
        return False
    try:
        exit_code = _wintypes.DWORD(0)
        if not _kernel32.GetExitCodeProcess(handle, ctypes.byref(exit_code)):
            return False
        return exit_code.value == _STILL_ACTIVE
    finally:
        _kernel32.CloseHandle(handle)


def _local_holder(pid: int) -> HolderRef:
    """Mirrors ``api.py``'s own ``_local_holder`` construction exactly —
    every lock this server grants is local, PID-tied, never a
    client-supplied value (see :meth:`WindowsPipeAPIServer
    ._holder_for_connection`)."""
    return HolderRef(origin="local", ref=str(pid), pid=pid)


# ---------------------------------------------------------------------------
# line framing over a named pipe (no makefile() equivalent)
# ---------------------------------------------------------------------------


class _PipeLineReader:
    """Newline-delimited-JSON framing over repeated
    ``Win32PipeAPI.read_file`` calls, buffered until a ``\\n`` appears —
    the named-pipe counterpart to ``conn.makefile("r", ...)``'s line
    iteration in ``api.py``'s own ``_handle_connection`` (a named pipe
    has no ``makefile()``). ``__iter__``/``__next__`` let
    :meth:`WindowsPipeAPIServer._handle_connection`'s ``for raw_line in
    rfile:`` loop work exactly as ``api.py``'s does.
    """

    def __init__(self, win32: Any, handle: int, chunk_size: int = _PIPE_BUFFER_SIZE) -> None:
        self._win32 = win32
        self._handle = handle
        self._chunk_size = chunk_size
        self._buf = b""
        self._eof = False

    def readline(self) -> str:
        while b"\n" not in self._buf and not self._eof:
            chunk = self._win32.read_file(self._handle, self._chunk_size)
            if not chunk:
                self._eof = True
                break
            self._buf += chunk
        if b"\n" in self._buf:
            line, _, rest = self._buf.partition(b"\n")
            self._buf = rest
            return (line + b"\n").decode("utf-8")
        if self._buf:
            line, self._buf = self._buf, b""
            return line.decode("utf-8")
        return ""

    def __iter__(self) -> "_PipeLineReader":
        return self

    def __next__(self) -> str:
        line = self.readline()
        if not line:
            raise StopIteration
        return line


class _PipeWriter:
    """The write half — ``write``/``flush`` matching the subset of
    ``conn.makefile("w", ...)``'s interface
    ``_api_base.BaseAPIServer._write`` uses (``write(text)`` twice, then
    ``flush()``), buffering until ``flush()`` issues one
    ``Win32PipeAPI.write_file`` call.
    """

    def __init__(self, win32: Any, handle: int) -> None:
        self._win32 = win32
        self._handle = handle
        self._buf = ""

    def write(self, text: str) -> None:
        self._buf += text

    def flush(self) -> None:
        if self._buf:
            self._win32.write_file(self._handle, self._buf.encode("utf-8"))
            self._buf = ""

    def close(self) -> None:
        self.flush()


# ---------------------------------------------------------------------------
# server
# ---------------------------------------------------------------------------


class WindowsPipeAPIServer(BaseAPIServer):
    """The Win32-named-pipe query/control API server — the Windows
    counterpart to ``api.RegistryAPIServer`` (see module docstring).

    ``store``/``locks`` are injected references, never constructed here,
    mirroring ``RegistryAPIServer``'s own "don't construct a second
    ``LockManager``" contract. ``peer_pid_fn``/``is_pid_alive_fn`` are
    the same kind of injectable escape hatch ``api.py`` offers, both
    defaulting to real, kernel-verified implementations (``peer_pid_fn``
    defaults to ``self._win32.get_client_process_id`` — the transport
    object's own PID lookup — rather than a free function, since it
    needs the transport handle *and* the transport object making the
    call; ``is_pid_alive_fn`` defaults to :func:`default_is_pid_alive_windows`).
    ``win32`` is the injectable transport double itself: production
    code leaves it ``None`` (constructs a real :class:`_Win32PipeAPI`,
    which only works on real Windows); every test in
    ``tests/registry/api_windows/test_api_windows.py`` supplies a fake
    implementing the same method surface.
    """

    def __init__(
        self,
        *,
        pipe_name: str | None = None,
        store: Store,
        locks: LockManager,
        peer_pid_fn: Callable[[int], int] | None = None,
        is_pid_alive_fn: Callable[[int], bool] | None = None,
        sweep_interval_s: float = DEFAULT_SWEEP_INTERVAL_S,
        lock: threading.RLock | None = None,
        name_set_callback: Callable[[Entry], None] | None = None,
        name_clear_callback: Callable[[str], None] | None = None,
        win32: Any | None = None,
    ) -> None:
        self.pipe_name = pipe_name if pipe_name is not None else DEFAULT_PIPE_NAME
        self._store = store
        self._locks = locks
        self._win32 = win32 if win32 is not None else _Win32PipeAPI()
        self._peer_pid_fn = (
            peer_pid_fn if peer_pid_fn is not None else self._win32.get_client_process_id
        )
        self._is_pid_alive_fn = (
            is_pid_alive_fn if is_pid_alive_fn is not None else default_is_pid_alive_windows
        )
        self._sweep_interval_s = sweep_interval_s
        # sprint 004, ticket 005's replication wiring -- see
        # `_api_base.BaseAPIServer`'s own docstring; `None` (the
        # default) leaves a bare server, unaffected, exactly like
        # `api.RegistryAPIServer`'s own constructor.
        self._name_set_callback = name_set_callback
        self._name_clear_callback = name_clear_callback

        self._lock = lock if lock is not None else threading.RLock()
        self._stop_event = threading.Event()
        self._accept_thread: threading.Thread | None = None
        self._sweep_thread: threading.Thread | None = None
        self._conn_threads: list[threading.Thread] = []
        # Guards `_pending_handle` -- the handle currently blocked in
        # `connect_named_pipe`, so `stop()` can close it from another
        # thread to unblock the accept loop (a named pipe has no
        # `socket.close()`-from-another-thread equivalent to interrupt a
        # blocking call other than closing the handle itself).
        self._accept_gate = threading.Lock()
        self._pending_handle: int | None = None

    # -- lifecycle -----------------------------------------------------

    def start(self) -> None:
        """Start accepting connections plus the periodic sweep, both on
        background threads. Returns immediately. Unlike
        ``RegistryAPIServer.start`` there is no socket file to bind/
        chmod here -- the pipe's access control is the explicit security
        descriptor :class:`_Win32PipeAPI` builds per connection instance
        (see module docstring), not filesystem permission bits.
        """
        self._stop_event.clear()
        self._accept_thread = threading.Thread(target=self._accept_loop, daemon=True)
        self._accept_thread.start()
        self._sweep_thread = threading.Thread(target=self._sweep_loop, daemon=True)
        self._sweep_thread.start()

    def serve_forever(self) -> None:
        """Blocking convenience mirroring ``RegistryAPIServer
        .serve_forever`` exactly: :meth:`start`, then block until
        :meth:`stop` is called from another thread."""
        self.start()
        self._stop_event.wait()

    def stop(self) -> None:
        """Stop accepting new connections and the sweep timer, and join
        every connection-handler thread that is still alive, before
        returning -- mirrors ``RegistryAPIServer.stop``'s own join
        rationale (a caller that closes ``store`` immediately after
        ``stop()`` returns must not race a not-yet-finished handler
        thread).
        """
        self._stop_event.set()
        with self._accept_gate:
            pending = self._pending_handle
            self._pending_handle = None
        if pending is not None:
            try:
                self._win32.close_handle(pending)
            except OSError:
                pass
        if self._accept_thread is not None:
            self._accept_thread.join(timeout=2.0)
        if self._sweep_thread is not None:
            self._sweep_thread.join(timeout=2.0)
        for thread in self._conn_threads:
            thread.join(timeout=2.0)

    def __enter__(self) -> "WindowsPipeAPIServer":
        self.start()
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.stop()

    # -- accept / sweep loops -------------------------------------------

    def _accept_loop(self) -> None:
        while not self._stop_event.is_set():
            try:
                handle = self._win32.create_named_pipe(self.pipe_name)
            except OSError:
                break  # transport unavailable -- nothing more this loop can do
            with self._accept_gate:
                if self._stop_event.is_set():
                    self._pending_handle = None
                    try:
                        self._win32.close_handle(handle)
                    except OSError:
                        pass
                    break
                self._pending_handle = handle
            try:
                self._win32.connect_named_pipe(handle)
            except OSError:
                with self._accept_gate:
                    self._pending_handle = None
                try:
                    self._win32.close_handle(handle)
                except OSError:
                    pass
                break  # closed by stop() (or a real connect error) -- either way, done
            with self._accept_gate:
                self._pending_handle = None
            if self._stop_event.is_set():
                try:
                    self._win32.close_handle(handle)
                except OSError:
                    pass
                break
            thread = threading.Thread(
                target=self._handle_connection, args=(handle,), daemon=True
            )
            thread.start()
            self._conn_threads.append(thread)

    def _sweep_loop(self) -> None:
        """Mirrors ``RegistryAPIServer._sweep_loop`` exactly -- see that
        method's own docstring for why the sweep and connection-close
        release are kept as two independent triggers."""
        while not self._stop_event.wait(self._sweep_interval_s):
            with self._lock:
                released = self._locks.sweep(self._is_holder_alive)
            if released:
                logger.info("api_windows: liveness sweep released locks for %s", released)

    def _is_holder_alive(self, holder: HolderRef) -> bool:
        """Mirrors ``RegistryAPIServer._is_holder_alive`` exactly -- see
        that method's own docstring for the ``"remote"``-is-always-alive
        rationale (this server never grants a remote-origin lock)."""
        if holder.origin == "local":
            return self._is_pid_alive_fn(holder.pid)
        return True

    # -- connection handling ---------------------------------------------

    def _holder_for_connection(self, handle: int) -> HolderRef:
        """The :class:`~mbtools.registry._api_base.BaseAPIServer` hook:
        wraps this connection's kernel-verified client PID
        (``GetNamedPipeClientProcessId``, via ``self._peer_pid_fn``) in a
        local :class:`~mbtools.registry.locks.HolderRef` -- the
        named-pipe counterpart to ``api.RegistryAPIServer
        ._holder_for_connection``.
        """
        pid = self._peer_pid_fn(handle)
        return _local_holder(pid)

    def _handle_connection(self, handle: int) -> None:
        try:
            holder = self._holder_for_connection(handle)
        except OSError:
            logger.exception(
                "api_windows: could not read peer pid; dropping connection"
            )
            try:
                self._win32.close_handle(handle)
            except OSError:
                pass
            return
        pid = holder.pid
        assert pid is not None  # every holder this class builds carries one

        acquired_uids: set[str] = set()
        rfile = _PipeLineReader(self._win32, handle)
        wfile = _PipeWriter(self._win32, handle)
        try:
            for raw_line in rfile:
                line = raw_line.strip()
                if not line:
                    continue
                self._dispatch_line(line, pid, holder, acquired_uids, wfile)
        except OSError:
            pass
        finally:
            # Connection closed (cleanly or abruptly): release exactly
            # the locks this connection acquired -- see api.py's own
            # "Lock release on connection close" note.
            with self._lock:
                for uid in list(acquired_uids):
                    self._locks.release(uid, holder)
            try:
                self._win32.disconnect_named_pipe(handle)
            except OSError:
                pass
            try:
                self._win32.close_handle(handle)
            except OSError:
                pass

    def _dispatch_line(
        self,
        line: str,
        pid: int,
        holder: HolderRef,
        acquired_uids: set[str],
        wfile: Any,
    ) -> None:
        """Mirrors ``RegistryAPIServer._dispatch_line`` exactly, minus
        the ``flash`` branch -- see module docstring's "No protocol/op
        logic of its own"."""
        try:
            req = json.loads(line)
        except json.JSONDecodeError:
            self._write(wfile, _error(CODE_INVALID_REQUEST, "malformed JSON request"))
            return
        if not isinstance(req, dict):
            self._write(
                wfile, _error(CODE_INVALID_REQUEST, "request must be a JSON object")
            )
            return

        op = req.get("op")
        if op == "list":
            resp = self._op_list()
        elif op in ("get", "find"):
            resp = self._op_find(req)
        elif op == "lock":
            resp = self._op_lock(req, holder, acquired_uids)
        elif op == "unlock":
            resp = self._op_unlock(req, holder, acquired_uids)
        elif op == "mark_flashed":
            resp = self._op_mark_flashed(req, holder)
        elif op == "names_get":
            resp = self._op_names_get(req)
        elif op == "names_set":
            resp = self._op_names_set(req)
        elif op == "names_clear":
            resp = self._op_names_clear(req)
        elif op == "names_list":
            resp = self._op_names_list(req)
        else:
            resp = _error(CODE_INVALID_REQUEST, f"unknown op {op!r}")
        self._write(wfile, resp)

    # -- ops --------------------------------------------------------------
    #
    # list/find/lock/unlock/mark_flashed/names_* are all inherited from
    # BaseAPIServer -- see this class's own docstring and that module's.
