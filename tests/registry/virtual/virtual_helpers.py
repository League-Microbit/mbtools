"""Shared helpers for the registered-virtual-board tests (nezsim sprint 015
ticket 001): a JSON-lines client for the registry's Unix socket, a Python
fake board endpoint (a thread speaking ``lock``/``send_hex``/``flash``/
``stream`` the way nezsim's board endpoint will), and a server fixture
factory on a short temp socket path. No real USB, serial port or board.
"""

from __future__ import annotations

import base64
import json
import socket
import threading
import time

from mbtools.registry.flash import FlashOp
from mbtools.registry.api import RegistryAPIServer
from mbtools.registry.locks import LockManager
from mbtools.registry.store import Store

SIM_SERIAL = "1198504156"


def wait_until(predicate, timeout=3.0, interval=0.01) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(interval)
    assert predicate(), "condition never became true within timeout"


class JsonClient:
    """Newline-JSON client over the registry's AF_UNIX socket."""

    def __init__(self, socket_path):
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.settimeout(5.0)
        self.sock.connect(str(socket_path))
        # binary: stream bytes can follow an ack line within one read
        self.rfile = self.sock.makefile("rb")
        self.wfile = self.sock.makefile("w", encoding="utf-8", newline="\n")

    def send(self, obj: dict) -> None:
        self.wfile.write(json.dumps(obj) + "\n")
        self.wfile.flush()

    def recv(self) -> dict:
        line = self.rfile.readline()
        if not line:
            raise ConnectionError("registry: connection closed")
        return json.loads(line)

    def raw(self, n: int) -> bytes:
        """Read exactly ``n`` raw bytes (post-``stream``), via the same
        buffered reader the ack line came from."""
        return self.rfile.read(n)

    def eof(self) -> bool:
        return self.rfile.read(1) == b""

    def request(self, obj: dict) -> dict:
        self.send(obj)
        return self.recv()

    def close(self) -> None:
        try:
            self.sock.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        for f in (self.wfile, self.rfile):
            try:
                f.close()
            except OSError:
                pass
        self.sock.close()


def register_request(name="tinybot", endpoint="127.0.0.1:9", **over) -> dict:
    req = {
        "op": "register_board",
        "name": name,
        "endpoint": endpoint,
        "role": "NEZHA2",
        "serial": SIM_SERIAL,
        "simulated": True,
    }
    req.update(over)
    return req


class FakeEndpoint:
    """A Python stand-in for a simulator's board endpoint. Speaks JSON
    lines for ``lock``/``unlock``/``send_hex``/``flash``/``stream``; after a
    ``stream`` ack, echoes every byte it receives back (upper-cased) and
    also pushes ``greeting`` first, so bytes demonstrably flow both ways.
    Records what it was asked, and can be told to refuse or fail."""

    def __init__(self, *, greeting=b"HELLO", stream_ack=None, lock_ack=None,
                 flash_result=None):
        self.greeting = greeting
        self.stream_ack = stream_ack
        self.lock_ack = lock_ack
        self.flash_result = flash_result or {
            "type": "result", "ok": True, "success": True, "exit_code": 0, "error": None,
        }
        self.requests: list[dict] = []
        self.received_hex: list[bytes] = []
        self.stream_bytes = bytearray()
        self.stream_connections = 0
        self.stream_closed = threading.Event()
        self._srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._srv.bind(("127.0.0.1", 0))
        self._srv.listen(8)
        self.port = self._srv.getsockname()[1]
        self.endpoint = f"127.0.0.1:{self.port}"
        self._stop = False
        self._conns: list[socket.socket] = []
        threading.Thread(target=self._accept, daemon=True).start()

    def close(self) -> None:
        self._stop = True
        for c in list(self._conns):
            try:
                c.close()
            except OSError:
                pass
        try:
            self._srv.close()
        except OSError:
            pass

    def _accept(self) -> None:
        while not self._stop:
            try:
                c, _ = self._srv.accept()
            except OSError:
                return
            self._conns.append(c)
            threading.Thread(target=self._serve, args=(c,), daemon=True).start()

    def _serve(self, c: socket.socket) -> None:
        buf = b""

        def send(obj):
            c.sendall((json.dumps(obj) + "\n").encode())

        try:
            while True:
                while b"\n" not in buf:
                    chunk = c.recv(65536)
                    if not chunk:
                        return
                    buf += chunk
                line, _, buf = buf.partition(b"\n")
                req = json.loads(line)
                self.requests.append(req)
                op = req.get("op")
                if op == "lock":
                    send(self.lock_ack or {"ok": True})
                elif op == "send_hex":
                    self.received_hex.append(base64.b64decode(req["data"]))
                    send({"ok": True, "hex_path": "/staged/1.hex"})
                elif op == "flash":
                    send({"type": "log", "line": "erasing..."})
                    send({"type": "log", "line": "programming..."})
                    send(self.flash_result)
                    return
                elif op == "stream":
                    if self.stream_ack is not None:
                        send(self.stream_ack)
                        return
                    send({"ok": True})
                    self.stream_connections += 1
                    # raw bytes from here on
                    c.sendall(self.greeting + buf)
                    self.stream_bytes += buf
                    buf = b""
                    while True:
                        data = c.recv(65536)
                        if not data:
                            self.stream_closed.set()
                            return
                        self.stream_bytes += data
                        c.sendall(data.upper())
                else:
                    send({"ok": False, "code": "invalid_request", "error": "unsupported"})
        except (OSError, ValueError):
            self.stream_closed.set()


def make_server_factory(socket_dir, store: Store, locks: LockManager, **server_kw):
    servers: list[RegistryAPIServer] = []

    def _make(**kw):
        srv = RegistryAPIServer(
            socket_path=f"{socket_dir}/api.sock",
            store=store,
            locks=locks,
            flash_op=FlashOp(locks=locks, store=store, runner=lambda cmd, log: 0),
            sweep_interval_s=100.0,
            stream_settle_s=0,
            **{**server_kw, **kw},
        )
        srv.start()
        servers.append(srv)
        return srv

    return _make, servers
