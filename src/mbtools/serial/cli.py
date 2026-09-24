"""mbtools.serial.cli -- CLI entry point for ``mbserial``.

Per sprint.md's Architecture (module ``serial.cli``), this is the one
place that wires ``registry.client`` (via ``serial.connect``) to a human:
``mbserial <name> [--reset] [message...]``. Argument handling and
sequencing only -- no business logic lives here that ``serial.connect``
could instead own (module docstring there: resolve, lock, open,
reset). With no ``message`` words, this hands the user an interactive
terminal (:meth:`~mbtools.serial.connect.Session.interact`); with one or
more, it sends them as a single one-shot command
(:meth:`~mbtools.serial.connect.Session.send_command`) and prints the
reply lines. Serves UC-010 in full (SUC-004/SUC-005).

Ticket 009 builds this, replacing the sprint-001 stub
(``mbtools.serial``'s former ``stub_main`` call -- ``pyproject.toml``'s
``[project.scripts]`` now points ``mbserial`` directly at :func:`main`
here, matching ``mbdeploy``'s own ticket-007 convention).

Device output goes to stdout and status text to stderr (ported from
``mbdeploy``'s own ``console.py`` module docstring), so a one-shot call
can be piped without the banner contaminating the data.

**Local vs. remote transport (ticket 013, SUC-004)**: ``_run_connect``
resolves ``<name>`` via the local registry, then branches exactly once on
``device.get("host")`` -- ``None`` keeps the sprint-002 local flow
untouched (:func:`_run_connect_local`, driving
``serial.connect.connect``); a peer-owned device (``host`` set) opens a
``remote_client.RemoteRegistryClient`` against the owning host's
``endpoint`` (``host:remote_api_port``, ticket 010) and drives
``serial.remote_connect.connect`` against that instead
(:func:`_run_connect_remote`), mirroring ``deploy.cli``'s own ticket-012
branch (Decision 8: a client talks directly to the owning host's own
registry, never proxied through the local one). Whichever transport
resolves the session, :func:`_run_session` runs the interactive-or-
one-shot body identically -- both ``serial.connect.Session`` and
``serial.remote_connect.RemoteSession`` expose the same duck-typed
``interact``/``send_command``/``close`` (ticket 013's own acceptance
criterion), so nothing below this point needs to know which transport is
in hand. A locked-device message on either branch is formatted by
``mbtools.common.format_locked_message`` -- shared with ``deploy.cli``,
not reimplemented per client tool, so a remote holder never reads as
"by pid None" on either.
"""

from __future__ import annotations

import argparse
import sys

from mbtools.common import (
    EXIT_ERROR,
    EXIT_HARDWARE,
    EXIT_NO_DAEMON,
    EXIT_OK,
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
from mbtools.registry.client import resolve_local_api_address
from mbtools.registry.remote_client import RemoteRegistryClient
from mbtools.serial import remote_connect
from mbtools.serial.connect import (
    BAUD_RATE,
    DEFAULT_TIMEOUT,
    ConnectError,
    connect,
)

__all__ = ["main", "build_parser", "cmd_connect"]


def _run_session(session, args: argparse.Namespace, banner: str) -> int:
    """The interactive-or-one-shot body, run against an already-connected
    ``session`` -- a local ``serial.connect.Session`` or a remote
    ``serial.remote_connect.RemoteSession``, identical either way (module
    docstring): either an interactive terminal or a one-shot command,
    releasing the lock (``session.close()``) on every exit path -- normal
    Ctrl-D, Ctrl-C (already handled inside ``interact()`` itself, which
    never raises it back out here), or the one-shot reply having been
    printed.
    """
    try:
        if not args.message:
            print(banner, file=sys.stderr)
            return session.interact()

        # One-shot: the reply is the command's output, so it goes to
        # stdout while every status line goes to stderr (module
        # docstring).
        lines = session.send_command(" ".join(args.message), timeout=args.timeout)
        for line in lines:
            print(line)
        if not lines:
            print(
                f"mbserial: no response from {session.name} within "
                f"{args.timeout:g}s.",
                file=sys.stderr,
            )
            return EXIT_ERROR
        return EXIT_OK
    finally:
        session.close()


def _run_connect_local(
    client: RegistryClient, device: dict, args: argparse.Namespace
) -> int:
    """The local branch (unchanged sprint-002 flow, module docstring):
    resolve/lock/open (:func:`~mbtools.serial.connect.connect`), then
    :func:`_run_session`.
    """
    name = device.get("device_name") or device["uid"]
    try:
        session = connect(client, args.target, reset=args.reset, baud=args.baud)
    except DeviceLockedError as exc:
        holder = exc.holder or {}
        print(f"mbserial: {format_locked_message(name, holder)}", file=sys.stderr)
        return exc.exit_code
    except RegistryClientError as exc:
        print(f"mbserial: {exc.message}", file=sys.stderr)
        return exc.exit_code
    except ConnectError as exc:
        print(f"mbserial: {exc}", file=sys.stderr)
        return EXIT_HARDWARE

    banner = (
        f"connected to {session.name} at {args.baud} baud -- "
        "Ctrl-D or Ctrl-C to exit"
    )
    return _run_session(session, args, banner)


def _run_connect_remote(device: dict, host: str, args: argparse.Namespace) -> int:
    """The remote branch (ticket 013): connect directly to the owning
    host's own ``remote_api`` (its ``endpoint`` learned from ``find``'s
    response, ticket 010) and drive
    ``serial.remote_connect.connect``/:func:`_run_session` against it.

    A ``RegistryUnavailable`` raised at any point from here on --
    connecting, locking, opening the stream, or during the interactive/
    one-shot session itself -- is this branch's "owning-host-unreachable"
    error flow (mirrors ``deploy.cli``'s own ticket-012 handling):
    caught here and reported cleanly, never a stack trace.
    """
    name = device.get("device_name") or device["uid"]
    endpoint = device.get("endpoint") or ""
    remote_host, sep, port_str = endpoint.rpartition(":")
    try:
        remote_port = int(port_str) if sep else -1
    except ValueError:
        remote_port = -1
    if not sep or remote_port < 0:
        print(
            f"mbserial: {name} is owned by {host} but no usable endpoint is "
            "known for it -- try again once peering has re-synced.",
            file=sys.stderr,
        )
        return EXIT_ERROR

    try:
        with RemoteRegistryClient(remote_host, remote_port) as remote_client:
            try:
                session = remote_connect.connect(
                    remote_client, args.target, reset=args.reset
                )
            except DeviceLockedError as exc:
                holder = exc.holder or {}
                print(
                    f"mbserial: {format_locked_message(name, holder)}",
                    file=sys.stderr,
                )
                return exc.exit_code
            except RegistryClientError as exc:
                print(f"mbserial: {exc.message}", file=sys.stderr)
                return exc.exit_code

            banner = f"connected to {session.name} -- Ctrl-D or Ctrl-C to exit"
            return _run_session(session, args, banner)
    except RegistryUnavailable as exc:
        print(f"mbserial: {exc}", file=sys.stderr)
        print(f"mbserial: is {host}'s registry daemon reachable?", file=sys.stderr)
        return EXIT_NO_DAEMON


def _run_connect(client: RegistryClient, args: argparse.Namespace) -> int:
    """``mbserial``'s actual body, run against an already-connected local
    ``client`` -- see :func:`cmd_connect` for the socket-path resolution
    and :class:`RegistryUnavailable` handling one level up.

    Resolves ``<name>`` via the local registry, then branches on
    ``device.get("host")`` (ticket 013, module docstring's "Local vs.
    remote transport" note): ``None`` drives :func:`_run_connect_local`
    against this same ``client``; a peer-owned device drives
    :func:`_run_connect_remote` against a fresh connection to the owning
    host instead.
    """
    try:
        device = client.find(args.target)
    except RegistryClientError as exc:
        print(f"mbserial: {exc.message}", file=sys.stderr)
        return exc.exit_code

    host = device.get("host")
    if host is None:
        return _run_connect_local(client, device, args)
    return _run_connect_remote(device, host, args)


def cmd_connect(args: argparse.Namespace) -> int:
    """``mbserial <name> [--reset] [message...]`` -- resolve the socket
    path, open one :class:`~mbtools.registry.client.RegistryClient`
    session for the whole flow (same "Session model" reasoning
    ``deploy.cli`` documents: the serial lock taken on this connection
    must be released, or reported, on this same connection), and hand
    off to :func:`_run_connect`.
    """
    socket_path = resolve_local_api_address(args.socket, _SOCKET_ENV_VAR)
    try:
        with RegistryClient(socket_path) as client:
            return _run_connect(client, args)
    except RegistryUnavailable as exc:
        print(f"mbserial: {exc}", file=sys.stderr)
        print(
            "mbserial: is the registry daemon running? start it with "
            "'mbregistry run'",
            file=sys.stderr,
        )
        return EXIT_NO_DAEMON


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="mbserial",
        description="raw serial connection to a local micro:bit, by name",
    )
    parser.add_argument(
        "target", help="device name, uid, or short-uid token"
    )
    parser.add_argument(
        "message",
        nargs="*",
        metavar="word",
        help="text to send, joined with spaces and terminated with a "
        "newline. Omit it for an interactive session.",
    )
    parser.add_argument(
        "--reset",
        action="store_true",
        help="deliberately reset the board as part of connect (BREAK on "
        "Linux, reopen on macOS) -- by default, connecting does not "
        "reboot the board",
    )
    parser.add_argument(
        "--baud",
        type=int,
        default=BAUD_RATE,
        metavar="N",
        help=f"serial baud rate (default {BAUD_RATE})",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=DEFAULT_TIMEOUT,
        metavar="SEC",
        help=f"how long to wait for the reply to a one-shot message "
        f"(default {DEFAULT_TIMEOUT:g})",
    )
    parser.add_argument(
        "--socket",
        help=f"api socket path (default {DEFAULT_SOCKET_PATH}, or "
        f"${_SOCKET_ENV_VAR})",
    )
    parser.set_defaults(func=cmd_connect)
    return parser


def main(argv: list[str] | None = None) -> None:
    parser = build_parser()
    args = parser.parse_args(argv)
    sys.exit(args.func(args))


if __name__ == "__main__":
    main()
