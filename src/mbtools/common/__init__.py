"""Shared DTOs and constants used by more than one mbtools subpackage.

Kept deliberately minimal in sprint 001 — per sprint.md's ticket 001 notes,
this module stays empty except for a genuinely shared type needed to make
the test fakes typecheck cleanly. :class:`PortInfo` is that type: it's the
per-port value sprint.md's architecture describes
``usbwatch.PortWatcher.scan() -> {uid: PortInfo}`` returning, and
``mbtools.testing.fakes.FakeUSBSource`` needs a concrete shape to script
against ahead of ticket 003, which owns the real ``usbwatch`` module and
may extend this type then. The registry wire-protocol DTOs sprint 002
needs will live here too, once that sprint needs them.

:data:`DAPLINK_VID_PID` is here for the same one-place reason: ticket 002's
``mbtools.registry.identity`` needs it to decide a device is worth probing
before opening a port, and ticket 003's ``mbtools.registry.usbwatch`` needs
it to filter ``comports()`` — two modules, one literal, so they can't drift
apart the way a copy in each would.

Ticket 008 (``mbtools.registry.api``) adds the registry wire-protocol's
stable exit codes and error codes here rather than in ``api`` itself, per
that ticket's own acceptance criterion ("defined in one place ... for
sprint 002 to reuse") and this module's own stated purpose above ("the
registry wire-protocol DTOs sprint 002 needs will live here too, once
that sprint needs them"). Ticket 009's CLI is the first consumer inside
this sprint; sprint 002's client tools (``mbdeploy``, ``mbserial``) are
the reason these live here instead of buried in ``api.py`` where only
that module's own tests would see them.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

#: ARM DAPLink's USB VID:PID — every micro:bit's onboard debug/CDC
#: interface enumerates as this pair, on both macOS and Linux (ported from
#: mbdeploy's ``devices.py`` ``_DAPLINK_VID_PID``).
DAPLINK_VID_PID = (0x0D28, 0x0204)

# -- registry wire-protocol constants (ticket 008) --------------------------
#
# Stable process exit codes for mbregistry clients (the CLI, ticket 009,
# and sprint 002+'s mbdeploy/mbserial). Ported from
# microbit-radio-relay/server/src/mbrelay/errors.py's precedent -- HIL
# tests and Ansible branch on stable exit codes, so this shape ("stable,
# documented, small integer per failure category") is kept and these
# values must never be renumbered once released.
EXIT_OK = 0
EXIT_ERROR = 1
EXIT_USAGE = 2
EXIT_NO_DAEMON = 3  # api socket not present/unreachable -- UC-004's error flow
EXIT_NO_DEVICE = 4  # CODE_NOT_FOUND -- "no such device"
EXIT_LOCKED = 5  # CODE_LOCKED -- device already locked by someone else
EXIT_HARDWARE = 6  # a flash op ran and failed

#: Protocol-level error codes carried on every ``{"ok": false, "code":
#: ..., "error": ...}`` response from :mod:`mbtools.registry.api`. A
#: client switches on ``code`` (stable, small set); ``error`` is a
#: free-text message for humans, per UC-006's "distinct 'no such device'
#: error from 'locked'" requirement -- two different codes, not one
#: generic failure.
CODE_NOT_FOUND = "not_found"
CODE_LOCKED = "locked"
CODE_NOT_LOCKED = "not_locked"
CODE_INVALID_REQUEST = "invalid_request"
CODE_INTERNAL_ERROR = "internal_error"

# -- sprint 003 additions (ticket 006) ---------------------------------------
#
#: ``store.find()`` raises ``AmbiguousNameError`` (ticket 001) when a bare
#: device name collides across more than one host; ticket 006's shared
#: ``_api_base`` ops (the first API-layer callers of ``find()`` to translate
#: exceptions into wire codes) return this for that case, on both the local
#: Unix socket and the remote TCP control plane. The response also carries
#: ``"hosts": [...]`` (``null`` for the local/``NULL`` host) so a client can
#: build a ``name@host`` suggestion.
CODE_AMBIGUOUS_NAME = "ambiguous_name"

#: ``registry.remote_api``'s optional ``--auth-token``/``$MBREGISTRY_TOKEN``
#: shared secret (sprint.md Decision 6): a connection's first message must
#: carry a matching token when one is configured, or every op is refused
#: with this code before any dispatch. Never returned by the local Unix
#: socket, which has no auth concept (sprint 1's trust model, unchanged).
CODE_UNAUTHORIZED = "unauthorized"


# -- sprint 003 additions (ticket 012, shared by ticket 013) ----------------


def format_locked_message(name: str, holder: dict[str, Any]) -> str:
    """UC-006's "``<name>`` is locked for ``<kind>`` by pid ``<pid>``"
    message -- extended (``deploy.cli``, ticket 012) to name the holder's
    host instead of a null pid when the current holder is a remote
    session, and shared here (rather than defined once per client tool)
    so ``mbdeploy``/``mbserial`` (ticket 013) can never drift apart on
    this one piece of wording.

    A remote-origin ``HolderRef`` (ticket 006's response shape: ``{"kind":
    ..., "pid": None, "origin": "remote", "host": ...}``) always carries
    ``pid=None`` -- "a PID means nothing across hosts" -- so the original
    pid-only phrasing would read as "by pid None", which is not sensible.
    This can happen on *either* transport branch of either tool: a
    peer-owned device locked by someone else's remote session, but just as
    easily a *local* device contested by someone connecting to that same
    registry's own ``remote_api`` -- the lock is one process-wide
    ``LockManager``, shared by every listener (Unix socket or TCP)
    regardless of transport. A local holder (``host`` absent) keeps the
    original pid-only phrasing byte-for-byte (asserted by
    ``tests/deploy/test_deploy_cli.py``'s
    ``test_already_locked_fails_fast_naming_holder`` and
    ``tests/serial/test_mbserial_cli.py``'s own
    ``test_already_locked_fails_fast_naming_holder``, both unchanged).
    """
    kind = holder.get("kind")
    host = holder.get("host")
    if host:
        return f"{name} is locked for {kind} by a remote session on {host}"
    return f"{name} is locked for {kind} by pid {holder.get('pid')}"


@dataclass(frozen=True)
class PortInfo:
    """One USB serial port, shaped like a filtered ``comports()`` entry.

    ``uid`` is the DAPLink unique id pyserial reports as ``serial_number``
    (and which doubles as the pyOCD probe UID); ``port`` is the OS device
    path (``/dev/ttyACM0``, ``/dev/cu.usbmodem141101``); ``vid``/``pid``
    are the USB vendor/product id the port enumerated with — kept here
    (rather than assumed already-filtered) so a scripted snapshot can
    include a non-matching device and prove the filter rejects it.
    """

    uid: str
    port: str
    vid: int
    pid: int
