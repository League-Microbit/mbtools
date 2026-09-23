"""mbtools.deploy.cli -- CLI entry point for ``mbdeploy``.

Per sprint.md's Architecture (module "deploy.cli"), this is the one place
that composes ``registry.client`` (resolve/lock/unlock/mark_flashed),
``deploy.flash`` (run pyOCD), ``deploy.release`` (get a hex file from
``--repo``), and ``registry.render`` (``list``) into the ``deploy``/
``list``/``build``/``debug`` subcommands -- plus the relay guard
(``--force-relay``) and the build/debug passthroughs, each a thin wrapper
with no responsibility of its own beyond argument handling. No business
logic lives here that any of those modules could instead own; this
module's own job is argument handling, sequencing, and turning each
result into a message and a stable exit code.

Ticket 007 built ``deploy`` here, replacing the sprint-001 stub
(``mbtools.deploy``'s former ``stub_main`` call -- ``pyproject.toml``'s
``[project.scripts]`` now points ``mbdeploy`` directly at :func:`main`
here, matching ``mbregistry``'s own convention). Ticket 008 adds
``list``/``build``/``debug`` to this same :func:`build_parser` alongside
``deploy`` rather than in a second parser -- there is only ever one
``mbdeploy`` argument parser.

**``list``** (SUC-003) is a plain client of the api socket, exactly like
``mbregistry list``: :func:`cmd_list` calls ``registry.client.list()``
and ``registry.render``'s same ``render_table``/``render_json``
functions ``mbregistry list`` uses -- no ``mbdeploy``-specific rendering
code exists, and a registry-unavailable connection reports the same
``EXIT_NO_DAEMON`` error.

**``build``** (SUC-006) never touches the registry at all -- it shells
out to the firmware build script, ported near-verbatim from today's
``mbdeploy``'s own ``builder.py`` (default ``<python> build.py`` in CWD,
``--build-cmd`` overrides the whole command, ``--clean``/``--verbose``/
``-j`` appended). Per sprint.md's component diagram, this stays inline
here rather than becoming a fifth module -- the diagram's four edges out
of ``deploy.cli`` don't include a "builder", since build has no registry
interaction to abstract.

**``debug``** (SUC-006) resolves and locks the named device
(``kind=debug``) via ``registry.client``, then runs the given pyOCD
invocation as a *bare passthrough* -- sprint.md's own Open Questions
entry ("`mbdeploy debug`'s scope beyond a bare pyOCD passthrough is
undefined ... this sprint ships the minimal passthrough needed to hold
the lock correctly; richer debug UX is deferred"): the argv after
``--`` is handed to ``pyocd`` completely unmodified (no injected
``--uid``, no interpretation), unlike ``deploy.flash``'s own
failure-signature-matching invocations. The lock is released when the
pyOCD subprocess exits -- success, failure, or a Ctrl-C (SIGINT) that
interrupts the session -- never leaked.

**Local flashing, not the registry's own minimal ``flash`` op**
(sprint.md's Design Rationale): this module runs
:func:`~mbtools.deploy.flash.flash_hex` directly, under a ``flash``-kind
lock taken through ``registry.client`` -- it never routes through the
registry's own ``flash`` wire-protocol op. The registry's ``flash`` op is
deliberately minimal (no retry, no mass-erase recovery); this module keeps
all of that "fancy work" in the client where the brief puts it, and
taking the lock via ``lock(kind="flash")`` still gets the daemon's
existing flash-triggered re-probe hook for free when the lock is
released. That leaves ``flash_count`` bookkeeping without a home for this
path, hence the explicit
:meth:`~mbtools.registry.client.RegistryClient.mark_flashed` call below.

**Wait-for-reprobe** compares ``last_probe`` (a monotonic timestamp on
every device record) *and* ``state`` -- not ``last_probe`` alone --
against the device's own value captured just before locking. A failed
re-probe attempt (no announcement heard) still advances ``last_probe``
(the daemon's own probe pipeline writes it either way, successful or
not) but leaves ``state`` at ``"connected_no_firmware"``, not
``"connected"`` -- checking only the timestamp would misreport that
failed attempt as a fresh, successful announcement. Only a strictly later
timestamp *and* ``state == "connected"`` counts as "a new announcement
arrived".

**Literal ``"flash"``/``"connected"`` strings, not imports from
``registry.locks``/``registry.store``**: sprint.md's own component diagram
enumerates exactly four edges out of ``deploy.cli`` (``registry.client``,
``deploy.flash``, ``deploy.release``, ``registry.render``) and calls out,
in its dotted-edge note, that this is "exactly the boundary that keeps
deploy/serial from ever importing store/locks/daemon directly."
``registry.client.lock()`` already takes ``kind`` as a plain string (its
own docstring: "Acquire a ``kind``-kind lock"), so the wire-protocol
string is what a client speaks, not a Python symbol imported from the
daemon's own internals.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

from mbtools.common import (
    EXIT_ERROR,
    EXIT_HARDWARE,
    EXIT_NO_DAEMON,
    EXIT_OK,
    EXIT_USAGE,
)
from mbtools.deploy.flash import flash_hex
from mbtools.deploy.release import GithubReleaseError, resolve_hex
from mbtools.registry.client import (
    DEFAULT_SOCKET_PATH,
    DeviceLockedError,
    DeviceNotFoundError,
    InvalidRequestError,
    RegistryClient,
    RegistryClientError,
    RegistryUnavailable,
)
from mbtools.registry.client import SOCKET_ENV_VAR as _SOCKET_ENV_VAR
from mbtools.registry.client import resolve_socket_path
from mbtools.registry.render import render_json, render_table

__all__ = [
    "main",
    "build_parser",
    "cmd_deploy",
    "cmd_list",
    "cmd_build",
    "cmd_debug",
]

#: Lock kind the deploy flow takes -- a plain wire-protocol string, not a
#: ``registry.locks.KIND_FLASH`` import (module docstring, "Literal ...
#: not imports").
_LOCK_KIND_FLASH = "flash"

#: Lock kind the debug flow takes -- same "plain wire-protocol string,
#: not an import" reasoning as ``_LOCK_KIND_FLASH`` above.
_LOCK_KIND_DEBUG = "debug"

#: ``registry.store``'s own "a real announcement was heard" state string
#: -- same "plain string, not an import" reasoning as ``_LOCK_KIND_FLASH``.
_STATE_CONNECTED = "connected"

# Invoke pyocd through the running interpreter rather than as a bare PATH
# lookup -- same reasoning, and the same literal invocation shape, as
# deploy.flash._PYOCD/registry.flash._PYOCD (mbtools is typically
# installed via an isolated venv, so pyocd -- a declared dependency -- is
# importable here but its console script may not be on PATH). Duplicated
# locally rather than imported since it is each module's own private
# constant, not a shared export.
_PYOCD = [sys.executable, "-m", "pyocd"]

#: Conventional Unix "killed by SIGINT" exit code (128 + signal number 2)
#: -- ``debug``'s own exit code when the pyOCD session is interrupted by
#: Ctrl-C, distinct from every ``EXIT_*`` constant in ``mbtools.common``
#: (those are this project's own stable protocol-level codes; this one
#: describes how the child process ended, matching what a shell itself
#: reports for a SIGINT-killed foreground command).
_EXIT_SIGINT = 130

#: How long ``deploy`` waits for the post-unlock re-probe to land a fresh
#: announcement before giving up and reporting plainly that none arrived
#: (this ticket's own "bounded timeout, never hang" acceptance
#: criterion). Generous relative to ``identity.probe()``'s worst case
#: (two ~1.6s read windows, per ticket 004's bounded extra retry) plus
#: daemon poll-interval jitter.
DEFAULT_REPROBE_TIMEOUT_S = 15.0

#: Poll cadence while waiting for the re-probe -- fine-grained enough not
#: to overshoot a short ``--reprobe-timeout`` (tests use a sub-second
#: one) by much, coarse enough not to hammer the socket.
_REPROBE_POLL_INTERVAL_S = 0.1


def _is_relay(role: str | None) -> bool:
    """True if ``role`` (a device record's announced firmware role) names
    a radio relay/bridge.

    Ported from today's ``mbdeploy``'s own ``devices.is_relay`` (case-
    insensitive ``"RELAY"``/``"BRIDGE"`` substring match) -- matches both
    ``RADIORELAY`` and the firmware's actual ``RADIOBRIDGE``.
    """
    if not role:
        return False
    upper = role.upper()
    return "RELAY" in upper or "BRIDGE" in upper


def _wait_for_reprobe(
    client: RegistryClient,
    uid: str,
    previous_last_probe: float,
    timeout_s: float,
    poll_interval_s: float = _REPROBE_POLL_INTERVAL_S,
) -> dict[str, Any] | None:
    """Poll ``client.find(uid)`` until a fresh, successful announcement
    lands (``last_probe`` strictly later than ``previous_last_probe``,
    and ``state == "connected"`` -- module docstring's "Wait-for-reprobe"
    note on why both are checked) or ``timeout_s`` elapses.

    Always makes at least one ``find()`` attempt, even if ``timeout_s <=
    0``. Returns the device dict on success, ``None`` on timeout -- never
    raises for "no announcement yet": a :class:`DeviceNotFoundError`
    mid-poll (e.g. the daemon momentarily dropped the record) is treated
    the same way, keep waiting, not a reason to crash the command over a
    transient gap. Any other :class:`RegistryClientError` (a real
    protocol problem) propagates, since that is not "still waiting", it's
    a bug.
    """
    deadline = time.monotonic() + timeout_s
    while True:
        try:
            device = client.find(uid)
        except DeviceNotFoundError:
            device = None
        if (
            device is not None
            and device.get("state") == _STATE_CONNECTED
            and float(device.get("last_probe") or 0.0) > previous_last_probe
        ):
            return device
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return None
        time.sleep(min(poll_interval_s, remaining))


def _run_deploy(client: RegistryClient, args: argparse.Namespace) -> int:
    """The ``deploy`` subcommand's actual body, run against an already-
    connected ``client`` -- see :func:`cmd_deploy` for the socket-path
    resolution and :class:`RegistryUnavailable` handling one level up.

    Follows the ticket's own numbered flow: resolve, relay guard, resolve
    hex, lock, flash, mark_flashed, unlock, wait-for-reprobe, report.
    """
    if args.asset and not args.repo:
        print("mbdeploy: --asset only makes sense with --repo", file=sys.stderr)
        return EXIT_USAGE

    # -- 1. resolve <name> -----------------------------------------------
    try:
        device = client.find(args.target)
    except RegistryClientError as exc:
        print(f"mbdeploy: {exc.message}", file=sys.stderr)
        return exc.exit_code

    uid = device["uid"]
    name = device.get("device_name") or uid

    # -- 2. relay guard -- before any lock or hex resolution -------------
    if _is_relay(device.get("role")) and not args.force_relay:
        print(
            f"mbdeploy: {name} is a relay/bridge ({device.get('role')}) -- "
            "use --force-relay to flash it anyway.",
            file=sys.stderr,
        )
        return EXIT_ERROR

    # -- 3. resolve the hex file ------------------------------------------
    if args.hex:
        hex_path = args.hex
    else:
        try:
            release = resolve_hex(args.repo, asset_override=args.asset)
        except ValueError as exc:
            # parse_repo_ref's own "not OWNER/REPO[@TAG]" -- a usage
            # error, not a network one (deploy.release's own docstring).
            print(f"mbdeploy: {exc}", file=sys.stderr)
            return EXIT_USAGE
        except GithubReleaseError as exc:
            print(f"mbdeploy: {exc}", file=sys.stderr)
            return EXIT_ERROR
        hex_path = str(release.hex_path)

    # -- 4. lock (kind=flash) -- fail fast, no retry, no blocking wait ---
    try:
        client.lock(uid, _LOCK_KIND_FLASH)
    except DeviceLockedError as exc:
        holder = exc.holder or {}
        print(
            f"mbdeploy: {name} is locked for {holder.get('kind')} by pid "
            f"{holder.get('pid')}",
            file=sys.stderr,
        )
        return exc.exit_code
    except RegistryClientError as exc:
        print(f"mbdeploy: {exc.message}", file=sys.stderr)
        return exc.exit_code

    previous_last_probe = float(device.get("last_probe") or 0.0)
    blank_board = False

    def _log_line(line: str) -> None:
        nonlocal blank_board
        if "no firmware" in line.lower():
            blank_board = True
        print(line, file=sys.stderr)

    # -- 5/6/7: flash, mark_flashed, unlock -- unlock always runs, even on
    # a flash failure or an unexpected mark_flashed error, since it's also
    # what triggers the daemon's flash-lock-release re-probe hook (Design
    # Rationale) and must never be skipped just because something
    # upstream of it went wrong.
    try:
        rc = flash_hex(uid, hex_path, log=_log_line, board_name=name)
        if rc == 0:
            try:
                client.mark_flashed(uid)
            except InvalidRequestError as exc:
                # Deployment-sequencing risk (sprint.md's Migration
                # Concerns): an old, sprint-001-vintage daemon has no
                # mark_flashed op. Non-fatal -- the flash itself already
                # succeeded; only flash_count bookkeeping is lost.
                print(
                    "mbdeploy: warning: mark_flashed not supported by this "
                    f"registry daemon ({exc.message}) -- flash_count was "
                    "not updated",
                    file=sys.stderr,
                )
    finally:
        try:
            client.unlock(uid)
        except RegistryClientError as exc:
            print(
                f"mbdeploy: warning: unlock failed: {exc.message}",
                file=sys.stderr,
            )

    # -- flash failure reporting -- blank-board is distinct, never folded
    # into the generic message (this ticket's own acceptance criterion).
    if rc != 0:
        if blank_board:
            print(
                f"mbdeploy: {name} was mass-erased and never successfully "
                "reflashed -- it has NO FIRMWARE and will not run until it "
                "is successfully reflashed.",
                file=sys.stderr,
            )
        else:
            print(f"mbdeploy: flash failed (exit {rc})", file=sys.stderr)
        return EXIT_HARDWARE

    # -- 8. wait-for-reprobe -----------------------------------------------
    new_device = _wait_for_reprobe(
        client, uid, previous_last_probe, args.reprobe_timeout
    )
    if new_device is None:
        print(
            f"mbdeploy: no new announcement arrived from {name} within "
            f"{args.reprobe_timeout:.1f}s -- the flash command itself "
            "succeeded, but the board hasn't been confirmed back online "
            "yet; check 'mbdeploy list'.",
            file=sys.stderr,
        )
        return EXIT_ERROR

    print(
        f"mbdeploy: {new_device.get('device_name') or uid} re-announced as "
        f"{new_device.get('role') or '?'} "
        f"(flash_count={new_device.get('flash_count')})"
    )
    return EXIT_OK


def cmd_deploy(args: argparse.Namespace) -> int:
    """``mbdeploy deploy <name> [--hex FILE | --repo OWNER/REPO[@TAG]]
    [--asset NAME] [--force-relay]`` -- resolve the socket path, open one
    :class:`RegistryClient` session for the whole flow
    (``registry.client``'s own "Session model" docstring: a lock taken on
    this connection must be released, or reported, on this same
    connection -- never reconnect mid-flow), and hand off to
    :func:`_run_deploy`.
    """
    socket_path = resolve_socket_path(
        args.socket, _SOCKET_ENV_VAR, DEFAULT_SOCKET_PATH
    )
    try:
        with RegistryClient(socket_path) as client:
            return _run_deploy(client, args)
    except RegistryUnavailable as exc:
        print(f"mbdeploy: {exc}", file=sys.stderr)
        print(
            "mbdeploy: is the registry daemon running? start it with "
            "'mbregistry run'",
            file=sys.stderr,
        )
        return EXIT_NO_DAEMON


# ---------------------------------------------------------------------------
# list -- a client of the api socket, identical output to `mbregistry list`
# ---------------------------------------------------------------------------


def cmd_list(args: argparse.Namespace) -> int:
    """``mbdeploy list [--json]`` -- connect to the api socket, ask for
    every device, render it (SUC-003). The exact same
    ``registry.client.list()`` fetch and ``registry.render.render_table``/
    ``render_json`` functions ``mbregistry list`` uses -- no
    ``mbdeploy``-specific rendering code exists, so the two commands'
    output is identical for the same device set by construction, not by
    coincidence. A registry-unavailable connection reports the same
    ``EXIT_NO_DAEMON`` error ``mbregistry list`` already gives, not a
    stack trace.
    """
    socket_path = resolve_socket_path(
        args.socket, _SOCKET_ENV_VAR, DEFAULT_SOCKET_PATH
    )
    try:
        with RegistryClient(socket_path) as client:
            devices = client.list()
    except RegistryUnavailable as exc:
        print(f"mbdeploy: {exc}", file=sys.stderr)
        print(
            "mbdeploy: is the registry daemon running? start it with "
            "'mbregistry run'",
            file=sys.stderr,
        )
        return EXIT_NO_DAEMON
    except RegistryClientError as exc:
        print(f"mbdeploy: {exc.message}", file=sys.stderr)
        return exc.exit_code

    if args.json:
        print(json.dumps(render_json(devices), indent=2))
        return EXIT_OK

    print(render_table(devices))
    return EXIT_OK


# ---------------------------------------------------------------------------
# build -- shells out to the firmware build script, no registry at all
# ---------------------------------------------------------------------------


def cmd_build(args: argparse.Namespace) -> int:
    """``mbdeploy build [--clean] [--verbose] [--build-cmd CMD] [-j N]``
    -- shells out to the firmware build script and returns its own exit
    code verbatim (SUC-006). No registry interaction at all: this command
    never touches a board.

    Ported near-verbatim from today's ``mbdeploy``'s own ``builder.run``
    (``src/mbdeploy/builder.py``): the default command is ``<python>
    build.py`` in the current working directory unless ``--build-cmd``
    overrides the entire command (split on whitespace); ``--clean``/
    ``--verbose``/``-j N`` are appended either way. Missing ``build.py``
    with no ``--build-cmd`` override is a clear, immediate error rather
    than a subprocess-not-found stack trace.
    """
    if args.build_cmd:
        cmd = args.build_cmd.split()
    else:
        if not Path("build.py").exists():
            print(
                "mbdeploy: build.py not found in CWD. Use --build-cmd to "
                "override.",
                file=sys.stderr,
            )
            return EXIT_ERROR
        cmd = [sys.executable, "build.py"]

    if args.clean:
        cmd.append("--clean")
    if args.verbose:
        cmd.append("--verbose")
    if args.jobs is not None:
        cmd += ["-j", str(args.jobs)]

    result = subprocess.run(cmd)
    return result.returncode


# ---------------------------------------------------------------------------
# debug -- resolve, lock (kind=debug), bare pyocd passthrough, unlock
# ---------------------------------------------------------------------------


def _run_pyocd(cmd: list[str]) -> int:
    """Run a pyocd invocation with inherited stdio, and return its exit
    code.

    Unlike ``deploy.flash``'s streamed/captured invocations (which parse
    pyocd's output for retry/mass-erase decisions), a ``debug`` session
    can be genuinely interactive (``pyocd commander``'s REPL reads
    stdin; ``pyocd gdbserver`` prints progress a user watches live) --
    this is a plain, uncaptured ``subprocess.run()`` so the pyOCD
    subprocess gets the real terminal directly, not a pipe. Kept as its
    own module-level function (rather than inlined into :func:`_run_debug`)
    so a test can inject a fake in its place without touching a real
    ``pyocd`` binary or probe.
    """
    return subprocess.run(cmd).returncode


def _run_debug(client: RegistryClient, args: argparse.Namespace) -> int:
    """``debug``'s actual body, run against an already-connected
    ``client`` -- see :func:`cmd_debug` for the socket-path resolution
    and :class:`RegistryUnavailable` handling one level up.

    Resolve, lock (``kind=debug``, fail fast per SUC-005 -- no retry, no
    blocking wait), run the given pyOCD invocation as a bare passthrough
    (module docstring), release the lock when it exits -- success,
    failure, or a Ctrl-C (SIGINT) that interrupts the session. The
    ``finally`` block is what guarantees the lock is never leaked on an
    interrupted debug session: :func:`_run_pyocd` propagating
    ``KeyboardInterrupt`` still runs it before that exception (caught
    just inside it) turns into a plain, reported exit code.
    """
    try:
        device = client.find(args.target)
    except RegistryClientError as exc:
        print(f"mbdeploy: {exc.message}", file=sys.stderr)
        return exc.exit_code

    uid = device["uid"]
    name = device.get("device_name") or uid

    try:
        client.lock(uid, _LOCK_KIND_DEBUG)
    except DeviceLockedError as exc:
        holder = exc.holder or {}
        print(
            f"mbdeploy: {name} is locked for {holder.get('kind')} by pid "
            f"{holder.get('pid')}",
            file=sys.stderr,
        )
        return exc.exit_code
    except RegistryClientError as exc:
        print(f"mbdeploy: {exc.message}", file=sys.stderr)
        return exc.exit_code

    cmd = [*_PYOCD, *args.pyocd_args]
    try:
        try:
            return _run_pyocd(cmd)
        except KeyboardInterrupt:
            print(
                f"mbdeploy: debug session on {name} interrupted",
                file=sys.stderr,
            )
            return _EXIT_SIGINT
    finally:
        try:
            client.unlock(uid)
        except RegistryClientError as exc:
            print(
                f"mbdeploy: warning: unlock failed: {exc.message}",
                file=sys.stderr,
            )


def cmd_debug(args: argparse.Namespace) -> int:
    """``mbdeploy debug <name> -- <pyocd args>`` -- resolve the socket
    path, open one :class:`RegistryClient` session for the whole flow
    (same "Session model" reasoning as :func:`cmd_deploy`: the debug lock
    taken on this connection must be released, or reported, on this same
    connection), and hand off to :func:`_run_debug`.
    """
    socket_path = resolve_socket_path(
        args.socket, _SOCKET_ENV_VAR, DEFAULT_SOCKET_PATH
    )
    try:
        with RegistryClient(socket_path) as client:
            return _run_debug(client, args)
    except RegistryUnavailable as exc:
        print(f"mbdeploy: {exc}", file=sys.stderr)
        print(
            "mbdeploy: is the registry daemon running? start it with "
            "'mbregistry run'",
            file=sys.stderr,
        )
        return EXIT_NO_DAEMON


# ---------------------------------------------------------------------------
# argument parsing / entry point
# ---------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="mbdeploy",
        description="flash and manage local micro:bit devices by name",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    # More subcommands (list, build, debug) are ticket 008's job -- added
    # here, alongside deploy, in this same build_parser rather than a
    # second parser.
    deploy_p = sub.add_parser(
        "deploy", help="flash a hex file to a local device, resolved by name"
    )
    deploy_p.add_argument("target", help="device name, uid, or short-uid token")
    hex_source = deploy_p.add_mutually_exclusive_group(required=True)
    hex_source.add_argument("--hex", help="local .hex file to flash")
    hex_source.add_argument(
        "--repo",
        help="OWNER/REPO[@TAG] to fetch the release hex from (default tag: "
        "GitHub's own 'Latest' release)",
    )
    deploy_p.add_argument(
        "--asset", help="release asset filename to select (only with --repo)"
    )
    deploy_p.add_argument(
        "--force-relay",
        action="store_true",
        help="allow flashing a device whose announced role looks like a "
        "relay/bridge",
    )
    deploy_p.add_argument(
        "--socket",
        help=f"api socket path (default {DEFAULT_SOCKET_PATH}, or "
        f"${_SOCKET_ENV_VAR})",
    )
    deploy_p.add_argument(
        "--reprobe-timeout",
        type=float,
        default=DEFAULT_REPROBE_TIMEOUT_S,
        dest="reprobe_timeout",
        help="seconds to wait for the post-flash re-announcement (default "
        f"{DEFAULT_REPROBE_TIMEOUT_S})",
    )
    deploy_p.set_defaults(func=cmd_deploy)

    list_p = sub.add_parser("list", help="list devices known to the registry")
    list_p.add_argument(
        "--json", action="store_true", help="emit machine-readable JSON"
    )
    list_p.add_argument(
        "--socket",
        help=f"api socket path (default {DEFAULT_SOCKET_PATH}, or "
        f"${_SOCKET_ENV_VAR})",
    )
    list_p.set_defaults(func=cmd_list)

    build_p = sub.add_parser(
        "build", help="build firmware locally (no registry interaction)"
    )
    build_p.add_argument(
        "--clean", action="store_true", help="clean before building"
    )
    build_p.add_argument(
        "--verbose", action="store_true", help="show build output"
    )
    build_p.add_argument(
        "-j", dest="jobs", type=int, metavar="N", help="parallel jobs"
    )
    build_p.add_argument(
        "--build-cmd",
        dest="build_cmd",
        metavar="CMD",
        help="override the entire build command (default: '<python> "
        "build.py' in CWD)",
    )
    build_p.set_defaults(func=cmd_build)

    debug_p = sub.add_parser(
        "debug",
        help="run a pyocd invocation directly against a named device",
        description="mbdeploy debug <name> [--socket ...] -- <pyocd args>: "
        "give any flags of mbdeploy's own before <name>, not after -- "
        "everything from <name> onward (including a literal '--') is a "
        "bare pyocd passthrough.",
    )
    debug_p.add_argument("target", help="device name, uid, or short-uid token")
    debug_p.add_argument(
        "--socket",
        help=f"api socket path (default {DEFAULT_SOCKET_PATH}, or "
        f"${_SOCKET_ENV_VAR}) -- must be given before <name>",
    )
    debug_p.add_argument(
        "pyocd_args",
        nargs=argparse.REMAINDER,
        metavar="-- pyocd-args",
        help="the pyocd subcommand and arguments to run, verbatim "
        "(e.g. -- commander --uid <uid>)",
    )
    debug_p.set_defaults(func=cmd_debug)

    return parser


def main(argv: list[str] | None = None) -> None:
    parser = build_parser()
    args = parser.parse_args(argv)
    sys.exit(args.func(args))


if __name__ == "__main__":
    main()
