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
"""

from __future__ import annotations

import argparse
import sys

from mbtools.common import (
    EXIT_ERROR,
    EXIT_HARDWARE,
    EXIT_NO_DAEMON,
    EXIT_OK,
)
from mbtools.registry.client import (
    DEFAULT_SOCKET_PATH,
    DeviceLockedError,
    RegistryClient,
    RegistryClientError,
    RegistryUnavailable,
)
from mbtools.registry.client import SOCKET_ENV_VAR as _SOCKET_ENV_VAR
from mbtools.registry.client import resolve_socket_path
from mbtools.serial.connect import (
    BAUD_RATE,
    DEFAULT_TIMEOUT,
    ConnectError,
    connect,
)

__all__ = ["main", "build_parser", "cmd_connect"]


def _run_connect(client: RegistryClient, args: argparse.Namespace) -> int:
    """``mbserial``'s actual body, run against an already-connected
    ``client`` -- see :func:`cmd_connect` for the socket-path resolution
    and :class:`RegistryUnavailable` handling one level up.

    Resolve/lock/open (:func:`~mbtools.serial.connect.connect`), then
    either an interactive terminal or a one-shot command, releasing the
    lock (``session.close()``) on every exit path -- normal Ctrl-D,
    Ctrl-C (already handled inside ``interact()`` itself, which never
    raises it back out here), or the one-shot reply having been printed.
    """
    try:
        session = connect(client, args.target, reset=args.reset, baud=args.baud)
    except DeviceLockedError as exc:
        holder = exc.holder or {}
        print(
            f"mbserial: {args.target} is locked for {holder.get('kind')} by "
            f"pid {holder.get('pid')}",
            file=sys.stderr,
        )
        return exc.exit_code
    except RegistryClientError as exc:
        print(f"mbserial: {exc.message}", file=sys.stderr)
        return exc.exit_code
    except ConnectError as exc:
        print(f"mbserial: {exc}", file=sys.stderr)
        return EXIT_HARDWARE

    try:
        if not args.message:
            print(
                f"connected to {session.name} at {args.baud} baud -- "
                "Ctrl-D or Ctrl-C to exit",
                file=sys.stderr,
            )
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


def cmd_connect(args: argparse.Namespace) -> int:
    """``mbserial <name> [--reset] [message...]`` -- resolve the socket
    path, open one :class:`~mbtools.registry.client.RegistryClient`
    session for the whole flow (same "Session model" reasoning
    ``deploy.cli`` documents: the serial lock taken on this connection
    must be released, or reported, on this same connection), and hand
    off to :func:`_run_connect`.
    """
    socket_path = resolve_socket_path(
        args.socket, _SOCKET_ENV_VAR, DEFAULT_SOCKET_PATH
    )
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
