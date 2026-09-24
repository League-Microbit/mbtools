"""mbtools.relay.cli -- CLI entry point for ``mbrelay``.

Per sprint.md's Architecture (module ``relay.cli``) and this sprint's own
SUC-001, this is the one place a human (or an HIL test script) reaches
ticket 003's ``relay.protocol``/ticket 004's ``relay.channel`` through a
registry connection: ``mbrelay connect [robot[@host]]`` and ``mbrelay
names get/set/clear/list``. Structured exactly like ``serial.cli``'s
local/remote dispatch (resolve via ``registry.client``/
``registry.remote_client``, branch on the resolved device's ``host``
field, lock kind ``relay`` instead of ``serial``) -- see that module's
own docstring for the pattern this one mirrors line for line.

Ported from ``microbit-radio-relay/server/src/mbrelay/client.py``'s
socket/terminal helpers (``interactive``/``run_script``/
``tune_to_robot``), adapted to drive ``relay.protocol.RelayControl``
over a ``relay.channel.ByteChannel`` instead of a raw socket -- there is
no separate relay *server* process in this architecture (unlike the
legacy repo, where a client connected to an already-acquired-and-
normalized pool port), so this module does the acquire/normalize/tune/
release dance itself, driven by the registry lock rather than a
server-side connection lifecycle.

**Never calls ``RelayControl.reset_and_normalize()`` for the connect
session itself** (a deliberate deviation from this ticket's own Approach
text, which names that method for "on acquire" -- documented here since
later tickets need to know why): that convenience wrapper opens the
channel, does its work, and *unconditionally closes it again* in its own
``finally`` (ticket 003's own module docstring). For
:class:`~mbtools.relay.channel.LocalRelayChannel` that is harmless (open/
close cycles freely, ticket 004's own Implementation Notes), but
:class:`~mbtools.relay.channel.RemoteRelayChannel` is single-use --
closing it tears down the underlying ``RemoteStream``'s TCP connection
for good, and ``registry.remote_api``'s own "lock release on connection
close" contract means that close *also drops this session's relay lock*,
mid-flow, before ``!CG``/``!GO``/the interactive session ever run. Using
the convenience wrapper here would silently open a window for another
caller to steal the lock between "acquire" and "actually use the relay".
So this module instead calls :class:`~mbtools.relay.protocol.RelayControl`'s
smaller building blocks directly -- :meth:`~...RelayControl.hello`,
:meth:`~...RelayControl.firmware_version`, :meth:`~...RelayControl.normalize`
-- against one channel opened once and closed once, for the whole
session (acquire through release); see :func:`_run_session`. Release
mirrors the ticket's own Approach exactly: :meth:`~...RelayControl.
clear_stored_config` (``!DEFAULTS``) only, no second normalize -- the
*next* acquire's own ``normalize()`` already forces the board back to
defaults unconditionally, so nothing here needs to leave it there too.

**Name resolution always goes through the local registry connection**,
never a peer's, even when the relay itself is remote: ticket 002's
replication keeps every peered registry's ``name_registry`` table
converged (SUC-004), so the local connection already has the answer --
there is no reason to open a second connection to a remote registry just
to ask it the same question its peer already knows. This is also why
:func:`_find_free_relay` resolves the target *relay* via the local
client's ``list`` op (filtering ``role``/free client-side) rather than a
new server-side filtered-``find`` op: ``mbrelay connect`` never
enumerates or probes hardware itself (this ticket's own acceptance
criterion) -- every device access here is a call on the injected
registry client, never a raw serial/socket probe, which is what that
criterion actually guards against (verified in tests by asserting on a
fake registry client, not by inspecting this module's imports).

**Name-registry replication wiring** (ticket 002's own Implementation
Notes flagged this gap and named this ticket as one of the two intended
callers of ``PeerDiscovery.publish_name_set``/``publish_name_clear``):
this module does **not** call those directly, and does not import
``registry.store``/``registry.peering`` at all. ``mbrelay names
set/clear`` (and ``mbrelay connect``'s own name lookup) instead go
through new ``names_get``/``names_set``/``names_clear``/``names_list``
ops on the *registry daemon's own* local API (``registry._api_base
.BaseAPIServer``, wired into ``registry.api.RegistryAPIServer`` and
``registry.cli.assemble_registry``) -- the daemon is what already owns
the ``Store`` and the ``PeerDiscovery`` instance, and it fires
``publish_name_set``/``publish_name_clear`` itself, right after its own
``Store.set()``/``Store.clear()`` write, the same "the component that
owns the write fires the callback" shape ``LockManager``/``Daemon``
already use for lock/attach events. This CLI process never opens
``devices.db`` directly (mirrors every other client tool in this
project -- "other programs consult it through a service," ``registry.
api``'s own module docstring) and never risks a second, unreplicated
writer touching the same sqlite file a running daemon has open.
"""

from __future__ import annotations

import argparse
import queue
import re
import sys
import time
from dataclasses import dataclass, replace
from typing import Any

from mbtools.common import (
    EXIT_ERROR,
    EXIT_HARDWARE,
    EXIT_NO_DAEMON,
    EXIT_NO_DEVICE,
    EXIT_OK,
    EXIT_USAGE,
    format_locked_message,
)
from mbtools.registry.client import (
    DEFAULT_SOCKET_PATH,
    DeviceLockedError,
    RegistryClient,
    RegistryClientError,
    RegistryUnavailable,
)
from mbtools.registry.client import SOCKET_ENV_VAR as _SOCKET_ENV_VAR
from mbtools.registry.client import find_local_api_address
from mbtools.registry.locks import KIND_RELAY
from mbtools.registry.remote_client import RemoteRegistryClient
from mbtools.relay import naming
from mbtools.relay.channel import LocalRelayChannel, RemoteRelayChannel
from mbtools.relay.protocol import ByteChannel, Reader, RelayControl, RelayError

__all__ = ["main", "build_parser", "cmd_connect", "cmd_names_get",
           "cmd_names_set", "cmd_names_clear", "cmd_names_list"]

# -- !CG/!GO/PING sequencing -------------------------------------------------
#
# Not part of RelayControl's own vocabulary (hello/normalize/query/
# firmware_version/clear_stored_config, ticket 003) -- these three are
# specific to "put an already-acquired relay on a robot's link", this
# module's own job, ported from ``microbit-radio-relay/server/src/mbrelay
# /client.py``'s ``tune_to_robot`` regexes, local copies here for the same
# reason ``relay.protocol`` keeps its own local copies of BANNER_RE/
# IDENTITY_RE rather than importing them from elsewhere.

_REPLY_RE = re.compile(rb"#\s*(?:channel:|error:)[^\r\n]*\r?\n")
_TUNED_RE = re.compile(rb"#\s*channel:\s*(\d+)\s+group:\s*(\d+)")
_GO_RE = re.compile(rb"#\s*entering data plane[^\r\n]*\r?\n")
_PONG_RE = re.compile(rb"\bpong\b")

#: Settle time between !CG/!GO/PING (module-level, not a default argument
#: value, so a test can monkeypatch it to 0 -- see ``tests/relay/
#: test_cli.py``'s own fast-tests convention, mirroring ``serial.cli``
#: tests zeroing out ``serial.connect``'s ``OPEN_SETTLE``/``RESET_SETTLE``).
TUNE_SETTLE_S = 0.5


@dataclass(frozen=True)
class Tuned:
    channel: int
    group: int
    answered: "bool | None"  # None = not probed


def _tune(channel: ByteChannel, reader: Reader, entry: dict[str, Any], *,
          timeout: float = 8.0, probe: bool = True) -> Tuned:
    """Put an already-acquired, already-normalized relay on ``entry``'s
    link and enter the data plane -- one line, then its reply, then the
    next (ticket 003's own ``NORMALIZE_STEPS`` discipline: a burst can
    arrive behind a board mid-retune and gets chopped), ported from
    ``tune_to_robot``.
    """
    reader.clear()
    channel.write_nowait(f"!CG {entry['channel']} {entry['group']}\n".encode())
    match = reader.wait_for(_REPLY_RE, timeout)
    if match is None:
        raise RelayError(
            f"relay did not answer !CG {entry['channel']} {entry['group']} "
            f"within {timeout:.0f}s"
        )
    line = match.group(0).strip().decode(errors="replace")
    tuned = _TUNED_RE.search(match.group(0))
    if tuned is None:
        raise RelayError(f"relay refused !CG {entry['channel']} {entry['group']}: {line}")
    # The board echoes what it actually applied; trust that over what was
    # asked for, so a clamped/rejected value shows up to the caller.
    got_channel, got_group = int(tuned.group(1)), int(tuned.group(2))

    time.sleep(TUNE_SETTLE_S)
    reader.clear()
    channel.write_nowait(b"!GO\n")
    if reader.wait_for(_GO_RE, timeout) is None:
        raise RelayError("relay did not enter the data plane")

    answered = None
    if probe:
        time.sleep(TUNE_SETTLE_S)
        reader.clear()
        channel.write_nowait(b"PING\n")
        answered = reader.wait_for(_PONG_RE, 2.0) is not None
    return Tuned(got_channel, got_group, answered)


# -- target parsing -----------------------------------------------------


@dataclass(frozen=True)
class ConnectTarget:
    """``mbrelay connect <target>``'s parsed shape.

    ``tovez``            robot tovez, any free relay (local or remote)
    ``tovez@torture``    robot tovez, only through a relay owned by host
                          ``torture``
    """
    robot: str
    host: "str | None"


def parse_target(target: str) -> ConnectTarget:
    """Raises ``ValueError`` for anything that isn't a well-formed
    micro:bit name, optionally followed by ``@<host>`` -- this CLI names
    a *robot*, never a relay device directly (unlike ``mbserial``'s
    target, which names the device itself); the relay is picked for you
    (:func:`_find_free_relay`).
    """
    text = (target or "").strip()
    robot_part, at, host_part = text.partition("@")
    robot = naming.validate(robot_part)
    host = host_part.strip() if at else None
    if at and not host:
        raise ValueError(f"{target!r} has an empty host after '@'")
    return ConnectTarget(robot=robot, host=host)


def _is_relay_role(role: "str | None") -> bool:
    if not role:
        return False
    upper = role.upper()
    return "RELAY" in upper or "BRIDGE" in upper


def _find_free_relay(client: RegistryClient, host: "str | None") -> "dict[str, Any] | None":
    """Any free relay/bridge device the local registry knows about
    (``role`` contains ``RELAY``/``BRIDGE``, unlocked), optionally
    scoped to a specific peer ``host`` -- via the registry's own ``list``
    op, never a direct hardware scan (module docstring).

    Local relays (``host`` unset on the device record) sort first when
    ``host`` isn't pinned, so a plain ``mbrelay connect <robot>`` prefers
    a relay on this machine over sending the session over the network
    when both are available.
    """
    devices = client.list()
    candidates = [
        d for d in devices
        if _is_relay_role(d.get("role"))
        and d.get("lock_kind") is None
        and (host is None or d.get("host") == host)
    ]
    if not candidates:
        return None
    candidates.sort(key=lambda d: d.get("host") is not None)
    return candidates[0]


# -- data-plane interactive terminal / scripting -----------------------------


def _interactive(channel: ByteChannel, data_queue: "queue.Queue[bytes | None]",
                  escape: str = "]") -> int:
    """A minimal raw-mode terminal over the relay's data plane --
    ported from ``microbit-radio-relay/server/src/mbrelay/client.py``'s
    ``interactive()``. Raw mode matters: the data plane is a transparent
    byte stream, so line discipline/echo/^C handling would corrupt it.

    Adapted from a socket-``select`` loop to ``data_queue`` (fed by
    ``channel.start_reading``'s ``on_data``/``on_error`` callbacks,
    already running on ``channel``'s own background read thread -- see
    :func:`_run_session`) since :class:`~mbtools.relay.protocol.ByteChannel`
    is push-based, not a selectable fd. Falls back to a plain (non-raw,
    non-``select``) read loop whenever ``sys.stdin`` is not a tty --
    gated on ``is_tty``, not merely on whether ``fileno()`` succeeded:
    a redirected file, a pipe, or ``/dev/null`` all have a real file
    descriptor but are not ttys, and ``select``/``termios``/``tty`` are
    only imported in the ``is_tty`` branch below (found the hard way,
    sprint 005 ticket 010's hardware acceptance pass -- ``mbrelay
    connect`` with stdin redirected from ``/dev/null`` used to crash
    with ``AttributeError: 'NoneType' object has no attribute
    'select'`` because the old condition, ``stdin_fd is not None``, is
    true in exactly that non-tty case).
    """
    import io
    import os

    escape_byte = bytes([ord(escape.upper()) - 64])
    print(f"mbrelay: connected. Ctrl-{escape.upper()} to quit.", file=sys.stderr)

    try:
        stdin_fd = sys.stdin.fileno()
    except (AttributeError, io.UnsupportedOperation, ValueError):
        stdin_fd = None
    is_tty = stdin_fd is not None and os.isatty(stdin_fd)

    saved = None
    termios = tty = select = None
    if is_tty:
        import select as _select
        import termios as _termios
        import tty as _tty
        termios, tty, select = _termios, _tty, _select

    try:
        if is_tty:
            saved = termios.tcgetattr(stdin_fd)
            tty.setraw(stdin_fd)
        while True:
            while True:
                try:
                    item = data_queue.get_nowait()
                except queue.Empty:
                    break
                if item is None:
                    print("\r\nmbrelay: relay closed the connection", file=sys.stderr)
                    return EXIT_OK
                sys.stdout.write(item.decode("utf-8", "replace"))
                sys.stdout.flush()

            if is_tty:
                readable, _, _ = select.select([stdin_fd], [], [], 0.05)
                if stdin_fd not in readable:
                    continue
                data = os.read(stdin_fd, 4096)
            else:
                data = sys.stdin.readline().encode()

            if not data:
                return EXIT_OK
            if escape_byte in data:
                before, _, _ = data.partition(escape_byte)
                if before:
                    channel.write_nowait(before)
                print("\r\nmbrelay: closed", file=sys.stderr)
                return EXIT_OK
            channel.write_nowait(data)
    except (KeyboardInterrupt, BrokenPipeError, ConnectionResetError):
        return EXIT_OK
    finally:
        if saved is not None:
            termios.tcsetattr(stdin_fd, termios.TCSADRAIN, saved)


def _run_script(channel: ByteChannel, data_queue: "queue.Queue[bytes | None]",
                sends: list[str], expect: "str | None", timeout: float) -> int:
    """Non-interactive mode: send each line, optionally wait for a
    regex -- ported from ``client.py``'s ``run_script``, driven by
    ``data_queue`` instead of a raw socket recv loop. Returns a process
    exit code, so a shell script or an HIL test can branch on it.
    """
    pattern = re.compile(expect.encode(), re.MULTILINE) if expect else None
    buf = bytearray()
    deadline = time.monotonic() + timeout

    for line in sends:
        payload = line.encode().decode("unicode_escape").encode("latin-1")
        if not payload.endswith(b"\n"):
            payload += b"\n"
        channel.write_nowait(payload)

    while time.monotonic() < deadline:
        remaining = deadline - time.monotonic()
        try:
            item = data_queue.get(timeout=min(0.2, max(remaining, 0.0)))
        except queue.Empty:
            if pattern is None:
                break
            continue
        if item is None:
            break
        buf.extend(item)
        if pattern is not None and pattern.search(buf):
            sys.stdout.write(buf.decode("utf-8", "replace"))
            sys.stdout.flush()
            return EXIT_OK

    sys.stdout.write(buf.decode("utf-8", "replace"))
    sys.stdout.flush()
    if pattern is not None:
        print(f"\nmbrelay: never saw {expect!r} within {timeout:g}s", file=sys.stderr)
        return EXIT_ERROR
    return EXIT_OK


# -- channel construction (test seam, mirrors serial.connect's own
# ``_pyserial`` swap point) --------------------------------------------------


def _open_local_channel(port: str) -> ByteChannel:
    return LocalRelayChannel(port)


def _open_remote_channel(stream: Any) -> ByteChannel:
    return RemoteRelayChannel(stream)


def _make_control() -> RelayControl:
    return RelayControl()


# -- the session itself: acquire, tune, interactive-or-script, release ------


def _run_session(channel: ByteChannel, control: RelayControl, entry: dict[str, Any],
                 args: argparse.Namespace, label: str) -> int:
    """The whole lifecycle on one already-locked, not-yet-open
    ``channel``: open once, reset+normalize, tune to ``entry``'s
    channel/group, hand off to the interactive terminal or ``--send``/
    ``--expect`` scripting, then ``!DEFAULTS`` and close -- exactly
    once each, for the reasons the module docstring explains.
    """
    channel.open()
    time.sleep(control.open_settle)
    reader = Reader(channel)
    try:
        try:
            banner = control.hello(channel, reader)
            banner = replace(banner, firmware=control.firmware_version(channel, reader))
            control.normalize(channel, reader)
        except RelayError as exc:
            print(f"mbrelay: {exc}", file=sys.stderr)
            return EXIT_HARDWARE

        fw = f", firmware {banner.firmware}" if banner.firmware else ""
        print(
            f"mbrelay: {label}: relay {banner.device_name} reset and normalized{fw}",
            file=sys.stderr,
        )

        try:
            tuned = _tune(channel, reader, entry, probe=not args.no_probe)
        except RelayError as exc:
            print(f"mbrelay: {exc}", file=sys.stderr)
            return EXIT_HARDWARE

        print(
            f"mbrelay: tuned to {entry['name']}: channel {tuned.channel} "
            f"group {tuned.group} (source: {entry['source']})",
            file=sys.stderr,
        )
        if tuned.answered is True:
            print(f"mbrelay: {entry['name']} answered PING", file=sys.stderr)
        elif tuned.answered is False:
            print(
                f"mbrelay: no answer from {entry['name']} on channel "
                f"{tuned.channel} group {tuned.group} -- is it powered, in "
                "range, and running self-addressing firmware?",
                file=sys.stderr,
            )

        # Hand the channel's background read thread over to a plain queue
        # for the data-plane session -- see the module docstring; drop
        # anything the command-plane exchange above left buffered so it
        # never leaks into the data plane.
        data_queue: "queue.Queue[bytes | None]" = queue.Queue()
        channel.start_reading(
            on_data=data_queue.put,
            on_error=lambda _exc: data_queue.put(None),
        )
        reader.clear()

        if args.send or args.expect:
            return _run_script(channel, data_queue, args.send, args.expect, args.timeout)
        return _interactive(channel, data_queue, escape=args.escape)
    finally:
        # The interactive/script phase above re-pointed the channel's
        # read callbacks at ``data_queue`` (``channel.start_reading``
        # replaces, not adds to, ``on_data``/``on_error`` -- see both
        # adapters' own docstrings), so ``!DEFAULTS``'s reply would
        # otherwise never reach ``reader`` at all and this call would
        # always run out its own timeout for nothing. Point the channel
        # back at ``reader`` first -- cheap and always safe, even when
        # the interactive/script phase was never reached (``reader`` is
        # already the channel's target in that case, so this is a no-op).
        channel.start_reading(reader._on_data, reader._on_error)
        reader.clear()
        try:
            control.clear_stored_config(channel, reader)
        except RelayError:
            pass
        channel.close()


def _run_connect_local(client: RegistryClient, device: dict[str, Any],
                       entry: dict[str, Any], control: RelayControl,
                       args: argparse.Namespace) -> int:
    uid = device["uid"]
    display = device.get("device_name") or uid
    try:
        client.lock(uid, KIND_RELAY)
    except DeviceLockedError as exc:
        holder = exc.holder or {}
        print(f"mbrelay: {format_locked_message(display, holder)}", file=sys.stderr)
        return exc.exit_code
    except RegistryClientError as exc:
        print(f"mbrelay: {exc.message}", file=sys.stderr)
        return exc.exit_code

    channel = _open_local_channel(device["port"])
    try:
        return _run_session(channel, control, entry, args, f"local relay {display}")
    finally:
        try:
            client.unlock(uid)
        except RegistryClientError:
            pass


def _run_connect_remote(device: dict[str, Any], host: str, entry: dict[str, Any],
                        control: RelayControl, args: argparse.Namespace) -> int:
    """Mirrors ``serial.cli``'s own ticket-013 remote branch: connect
    directly to the owning host's ``remote_api`` and lock/stream there.
    Closing the resulting :class:`~mbtools.relay.channel.RemoteRelayChannel`
    (inside :func:`_run_session`'s own ``finally``) tears down that
    connection for good, which is also what releases the remote lock --
    no separate ``unlock`` call, unlike the local branch (module
    docstring).
    """
    uid = device["uid"]
    display = device.get("device_name") or uid
    endpoint = device.get("endpoint") or ""
    remote_host, sep, port_str = endpoint.rpartition(":")
    try:
        remote_port = int(port_str) if sep else -1
    except ValueError:
        remote_port = -1
    if not sep or remote_port < 0:
        print(
            f"mbrelay: {display} is owned by {host} but no usable endpoint is "
            "known for it -- try again once peering has re-synced.",
            file=sys.stderr,
        )
        return EXIT_ERROR

    try:
        with RemoteRegistryClient(remote_host, remote_port) as remote_client:
            try:
                remote_client.lock(uid, KIND_RELAY)
            except DeviceLockedError as exc:
                holder = exc.holder or {}
                print(f"mbrelay: {format_locked_message(display, holder)}", file=sys.stderr)
                return exc.exit_code
            except RegistryClientError as exc:
                print(f"mbrelay: {exc.message}", file=sys.stderr)
                return exc.exit_code

            stream = remote_client.open_stream(uid)
            channel = _open_remote_channel(stream)
            return _run_session(
                channel, control, entry, args, f"remote relay {display} on {host}"
            )
    except RegistryUnavailable as exc:
        print(f"mbrelay: {exc}", file=sys.stderr)
        print(f"mbrelay: is {host}'s registry daemon reachable?", file=sys.stderr)
        return EXIT_NO_DAEMON


def _run_connect(client: RegistryClient, target: ConnectTarget,
                 args: argparse.Namespace) -> int:
    device = _find_free_relay(client, target.host)
    if device is None:
        scope = f" on {target.host}" if target.host else ""
        print(f"mbrelay: no free relay available{scope}", file=sys.stderr)
        return EXIT_NO_DEVICE

    try:
        entry = client.names_get(target.robot)
    except RegistryClientError as exc:
        print(f"mbrelay: {exc.message}", file=sys.stderr)
        return exc.exit_code
    if entry is None:
        # SUC-001's error flow: distinct from "no free relay" above, and
        # never silently derived the way robot-console's own endpoint
        # derives on miss -- an operator has to say what this robot's
        # link is (`mbrelay names set`) before `connect` will use it.
        print(
            f"mbrelay: {target.robot!r} is not in the name registry -- "
            f"run 'mbrelay names set {target.robot} <channel> <group>' first",
            file=sys.stderr,
        )
        return EXIT_ERROR

    control = _make_control()
    host = device.get("host")
    if host is None:
        return _run_connect_local(client, device, entry, control, args)
    return _run_connect_remote(device, host, entry, control, args)


def cmd_connect(args: argparse.Namespace) -> int:
    try:
        target = parse_target(args.target)
    except ValueError as exc:
        print(f"mbrelay: {exc}", file=sys.stderr)
        return EXIT_USAGE

    socket_path = find_local_api_address(args.socket, _SOCKET_ENV_VAR)
    try:
        with RegistryClient(socket_path) as client:
            return _run_connect(client, target, args)
    except RegistryUnavailable as exc:
        print(f"mbrelay: {exc}", file=sys.stderr)
        print(
            "mbrelay: is the registry daemon running? start it with "
            "'mbregistry run'",
            file=sys.stderr,
        )
        return EXIT_NO_DAEMON


# -- mbrelay names get/set/clear/list ----------------------------------------
#
# A light wrapper over the registry's own names_get/set/clear/list ops
# (module docstring) -- for operator use without going through
# robot-console's HTTP compatibility endpoint (ticket 007).


def _with_client(args: argparse.Namespace, body):
    socket_path = find_local_api_address(args.socket, _SOCKET_ENV_VAR)
    try:
        with RegistryClient(socket_path) as client:
            return body(client)
    except RegistryUnavailable as exc:
        print(f"mbrelay: {exc}", file=sys.stderr)
        return EXIT_NO_DAEMON
    except RegistryClientError as exc:
        print(f"mbrelay: {exc.message}", file=sys.stderr)
        return exc.exit_code


def cmd_names_get(args: argparse.Namespace) -> int:
    def body(client: RegistryClient) -> int:
        entry = client.names_get(args.name)
        if entry is None:
            print(f"mbrelay: {args.name} is not in the name registry", file=sys.stderr)
            return EXIT_ERROR
        print(
            f"{entry['name']}: channel {entry['channel']} group {entry['group']} "
            f"({entry['source']})"
        )
        return EXIT_OK

    return _with_client(args, body)


def cmd_names_set(args: argparse.Namespace) -> int:
    def body(client: RegistryClient) -> int:
        entry = client.names_set(args.name, args.channel, args.group)
        print(
            f"{entry['name']}: channel {entry['channel']} group {entry['group']} "
            f"({entry['source']})"
        )
        return EXIT_OK

    return _with_client(args, body)


def cmd_names_clear(args: argparse.Namespace) -> int:
    def body(client: RegistryClient) -> int:
        client.names_clear(args.name)
        print(f"mbrelay: cleared {args.name}")
        return EXIT_OK

    return _with_client(args, body)


def cmd_names_list(args: argparse.Namespace) -> int:
    def body(client: RegistryClient) -> int:
        entries = client.names_list()
        if not entries:
            print("mbrelay: no names registered", file=sys.stderr)
            return EXIT_OK
        for entry in sorted(entries, key=lambda e: e["name"]):
            note = ""
            if entry.get("conflict"):
                note = f"  CONFLICT with {','.join(entry['conflict'])}"
            elif entry.get("channel_conflict"):
                note = f"  channel shared with {','.join(entry['channel_conflict'])}"
            print(
                f"{entry['name']}\tchannel {entry['channel']}\tgroup "
                f"{entry['group']}\t{entry['source']}{note}"
            )
        return EXIT_OK

    return _with_client(args, body)


# -- argument parsing ---------------------------------------------------


def _add_socket_arg(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--socket",
        help=f"api socket path (default {DEFAULT_SOCKET_PATH}, or "
        f"${_SOCKET_ENV_VAR})",
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="mbrelay",
        description="connect to a robot through a micro:bit radio relay",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    connect = sub.add_parser(
        "connect", help="connect to a robot by name through a free relay"
    )
    connect.add_argument(
        "target", help="robot name, optionally @host (e.g. tovez, tovez@torture)"
    )
    connect.add_argument(
        "--send",
        action="append",
        default=[],
        metavar="LINE",
        help="script mode: send LINE (repeatable), then exit instead of an "
        "interactive session",
    )
    connect.add_argument(
        "--expect",
        metavar="REGEX",
        help="script mode: wait for REGEX in the reply before exiting",
    )
    connect.add_argument(
        "--timeout",
        type=float,
        default=8.0,
        metavar="SEC",
        help="script mode: how long to wait for --expect (default 8)",
    )
    connect.add_argument(
        "--no-probe",
        action="store_true",
        help="skip the PING liveness probe after tuning",
    )
    connect.add_argument(
        "--escape",
        default="]",
        metavar="CHAR",
        help="interactive-mode escape character (default ']', i.e. Ctrl-])",
    )
    _add_socket_arg(connect)
    connect.set_defaults(func=cmd_connect)

    names = sub.add_parser("names", help="operate on the name registry directly")
    names_sub = names.add_subparsers(dest="names_command", required=True)

    names_get = names_sub.add_parser("get", help="show a name's channel/group")
    names_get.add_argument("name")
    _add_socket_arg(names_get)
    names_get.set_defaults(func=cmd_names_get)

    names_set = names_sub.add_parser("set", help="explicitly assign a name")
    names_set.add_argument("name")
    names_set.add_argument("channel", type=int)
    names_set.add_argument("group", type=int)
    _add_socket_arg(names_set)
    names_set.set_defaults(func=cmd_names_set)

    names_clear = names_sub.add_parser("clear", help="drop a name's row")
    names_clear.add_argument("name")
    _add_socket_arg(names_clear)
    names_clear.set_defaults(func=cmd_names_clear)

    names_list = names_sub.add_parser("list", help="list every registered name")
    _add_socket_arg(names_list)
    names_list.set_defaults(func=cmd_names_list)

    return parser


def main(argv: "list[str] | None" = None) -> None:
    parser = build_parser()
    args = parser.parse_args(argv)
    sys.exit(args.func(args))


if __name__ == "__main__":
    main()
