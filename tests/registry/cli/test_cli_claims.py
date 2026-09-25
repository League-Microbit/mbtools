"""Tests for sprint 007 ticket 002's own ``registry.cli`` additions:
``--only-uid``/``--exclude-uid`` argument parsing (the same
``--remote-port``-style flag test pattern
``tests/registry/cli/test_cli_run_peering.py`` already uses) and
``assemble_daemon_and_api``'s new ``claim_fn`` forwarding into
:class:`~mbtools.registry.daemon.Daemon`.

No real mDNS/socket assembly here (``_run_registry`` itself is never
called directly by any test in this suite -- see
``test_cli_run_peering.py``/``test_cli_run_windows.py`` for why: it has
no seam to inject a fake ``zeroconf``, so exercising it for real would
mean real mDNS registration). ``claims.build_claim_fn``'s own filtering
logic is already fully covered by ``tests/registry/claims/test_claims.py``
-- this file only proves the *plumbing*: the flags parse correctly, and
whatever ``claim_fn`` the CLI hands to ``assemble_daemon_and_api`` really
does reach and gate ``Daemon``.
"""

from __future__ import annotations

from mbtools.common import DAPLINK_VID_PID, PortInfo
from mbtools.registry.cli import assemble_daemon_and_api, build_parser
from mbtools.registry.store import Store
from mbtools.testing.fakes import FakeUSBSource, unavailable_chip_identity_session_factory

VID, PID_ = DAPLINK_VID_PID
UID = "9900" + "0000" + "11112222" + "3333444455556666" + "77778888" + "6e052820"


def _port_info(uid: str = UID) -> PortInfo:
    return PortInfo(uid=uid, port="/dev/ttyACM0", vid=VID, pid=PID_)


# ---------------------------------------------------------------------------
# argument parsing -- mirrors test_cli_run_peering.py's own
# test_build_parser_run_accepts_all_new_flags/..._default_to_none pair.
# ---------------------------------------------------------------------------


def test_build_parser_run_accepts_only_uid_and_exclude_uid_repeatable():
    parser = build_parser()
    args = parser.parse_args(
        [
            "run",
            "--only-uid",
            "aaaa",
            "--only-uid",
            "bbbb",
            "--exclude-uid",
            "cccc",
        ]
    )
    assert args.only_uid == ["aaaa", "bbbb"]
    assert args.exclude_uid == ["cccc"]


def test_build_parser_run_only_uid_and_exclude_uid_default_to_none():
    parser = build_parser()
    args = parser.parse_args(["run"])
    assert args.only_uid is None
    assert args.exclude_uid is None


def test_build_parser_run_exclude_uid_alone_is_accepted():
    parser = build_parser()
    args = parser.parse_args(["run", "--exclude-uid", "dddd"])
    assert args.exclude_uid == ["dddd"]
    assert args.only_uid is None


# ---------------------------------------------------------------------------
# assemble_daemon_and_api's claim_fn forwarding -- proves a claim_fn
# handed to this assembly function really does gate Daemon's attach path,
# not just get accepted and dropped (the same style
# test_assemble_registry_name_set_callback_reaches_peering in
# test_cli_run_peering.py already uses for its own new callback param).
# ---------------------------------------------------------------------------


def test_assemble_daemon_and_api_claim_fn_gates_attach(tmp_path):
    store = Store(tmp_path / "devices.db")
    usbwatch = FakeUSBSource([[_port_info()]])

    daemon, api = assemble_daemon_and_api(
        store=store,
        usbwatch=usbwatch,
        socket_path=str(tmp_path / "api.sock"),
        claim_fn=lambda uid: None,  # deny every claim
        chip_identity_session_factory=unavailable_chip_identity_session_factory,
    )

    daemon.run_once()

    assert store.get(UID) is None


def test_assemble_daemon_and_api_omitted_claim_fn_keeps_default_no_op_behavior(tmp_path):
    """No ``claim_fn`` given at all -- Daemon's own bare default (a
    filesystem-free no-op that always grants) applies, matching every
    pre-ticket-002 caller of this function."""
    store = Store(tmp_path / "devices.db")
    usbwatch = FakeUSBSource([[_port_info()]])

    daemon, api = assemble_daemon_and_api(
        store=store,
        usbwatch=usbwatch,
        socket_path=str(tmp_path / "api.sock"),
        # No serial_factory here -- identity.probe opens a real (but
        # nonexistent) port and returns None, which without this fake
        # would fall through to a *real* pyOCD session attempt (pyocd is
        # actually installed in this project's venv).
        chip_identity_session_factory=unavailable_chip_identity_session_factory,
    )

    daemon.run_once()

    assert store.get(UID) is not None
