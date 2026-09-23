"""mbtools.deploy.cli -- CLI entry point for ``mbdeploy``.

Per sprint.md's Architecture (module "deploy.cli"), this is the one place
that composes ``registry.client`` (resolve/lock/unlock/mark_flashed),
``deploy.flash`` (run pyOCD), and ``deploy.release`` (get a hex file from
``--repo``) into the ``deploy`` subcommand -- plus the relay guard
(``--force-relay``) and the wait-for-reprobe report. No business logic
lives here that any of those modules could instead own; this module's own
job is argument handling, sequencing, and turning each result into a
message and a stable exit code.

Ticket 007 builds only ``deploy`` here, replacing the sprint-001 stub
(``mbtools.deploy``'s former ``stub_main`` call -- ``pyproject.toml``'s
``[project.scripts]`` now points ``mbdeploy`` directly at :func:`main`
here, matching ``mbregistry``'s own convention). ``list``/``build``/
``debug`` are ticket 008's job, added to :func:`build_parser` alongside
``deploy`` rather than in a second parser -- there is only ever one
``mbdeploy`` argument parser.

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
import sys
import time
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

__all__ = ["main", "build_parser", "cmd_deploy"]

#: Lock kind the deploy flow takes -- a plain wire-protocol string, not a
#: ``registry.locks.KIND_FLASH`` import (module docstring, "Literal ...
#: not imports").
_LOCK_KIND_FLASH = "flash"

#: ``registry.store``'s own "a real announcement was heard" state string
#: -- same "plain string, not an import" reasoning as ``_LOCK_KIND_FLASH``.
_STATE_CONNECTED = "connected"

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

    return parser


def main(argv: list[str] | None = None) -> None:
    parser = build_parser()
    args = parser.parse_args(argv)
    sys.exit(args.func(args))


if __name__ == "__main__":
    main()
