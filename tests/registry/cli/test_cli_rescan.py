"""Tests for ``mbregistry rescan`` (sprint 010, ticket 003) -- the CLI
surface over ticket 002's local-only ``rescan`` op: summary rendering,
``--dry-run`` (no fresh table), ``--json`` (one combined object), and the
standard ``RegistryUnavailable``/``RegistryClientError`` handling every
other local-socket subcommand already gives (``cmd_unlock``'s own
precedent -- see ``test_cli_unlock.py``).

Mirrors ``test_cli_unlock.py``'s "drive a real ``RegistryAPIServer`` over
a real ``AF_UNIX`` socket in a short ``tmp_path``-style dir" pattern.
"""

from __future__ import annotations

import json
import shutil
import tempfile

import pytest

from mbtools.common import EXIT_NO_DAEMON, EXIT_OK
from mbtools.registry.api import RegistryAPIServer
from mbtools.registry.cli import main
from mbtools.registry.flash import FlashOp
from mbtools.registry.locks import KIND_SERIAL, HolderRef, LockManager
from mbtools.registry.store import Store

VID_PID = "0d28:0204"


def _uid(tag: str) -> str:
    unique = (tag * 4)[:16]
    return "9900" + "0000" + "11112222" + unique + "77778888" + "6e052820"


UID_ATTACHED = _uid("aaaa1111")
UID_GONE = _uid("bbbb2222")
UID_LOCKED_GONE = _uid("cccc3333")


@pytest.fixture
def socket_dir():
    d = tempfile.mkdtemp(prefix="mbregistry-cli-rescan-")
    yield d
    shutil.rmtree(d, ignore_errors=True)


@pytest.fixture
def store(tmp_path):
    """One live, attached device (never a purge candidate) and one
    ``gone`` device -- the single purge candidate most tests below act
    against. ``test_rescan_reports_skipped_locked`` seeds its own extra
    ``gone``-and-locked row, rather than adding it here, so every other
    test's "exactly one candidate" counts/assertions stay simple.
    """
    s = Store(tmp_path / "devices.db")
    s.upsert_attached(UID_ATTACHED, "/dev/ttyACM0", VID_PID)
    s.upsert_attached(UID_GONE, "/dev/ttyACM1", VID_PID)
    s.mark_disconnected(UID_GONE)
    yield s
    s.close()


@pytest.fixture
def locks():
    return LockManager()


@pytest.fixture
def poll_calls():
    return []


@pytest.fixture
def resync_calls():
    return []


@pytest.fixture
def server(socket_dir, store, locks, poll_calls, resync_calls):
    flash_op = FlashOp(locks=locks, store=store)
    srv = RegistryAPIServer(
        socket_path=f"{socket_dir}/api.sock",
        store=store,
        locks=locks,
        flash_op=flash_op,
        sweep_interval_s=100.0,
        poll_callback=lambda: poll_calls.append(1),
        peer_resync_callback=lambda: resync_calls.append(1),
    )
    srv.start()
    yield srv
    srv.stop()


# ---------------------------------------------------------------------------
# plain rescan -- summary, then a fresh list table
# ---------------------------------------------------------------------------


@pytest.mark.requires_af_unix
def test_rescan_prints_summary_then_fresh_table(server, store, capsys):
    with pytest.raises(SystemExit) as excinfo:
        main(["rescan", "--socket", str(server.socket_path)])

    assert excinfo.value.code == EXIT_OK
    out = capsys.readouterr().out
    assert "removed 1 device" in out
    # UID_GONE is gone; shown by its short_uid, the same identifier the
    # fresh table below would use for the same row.
    assert "bbbb2222" in out
    # The fresh table follows, separated from the summary by a blank
    # line -- UID_ATTACHED is still known, UID_GONE is not.
    summary, table = out.split("\n\n", 1)
    assert "aaaa1111" in table
    assert "bbbb2222" not in table
    assert store.get(UID_GONE) is None
    assert store.get(UID_ATTACHED) is not None


@pytest.mark.requires_af_unix
def test_rescan_triggers_poll_and_peer_resync(server, store, poll_calls, resync_calls):
    with pytest.raises(SystemExit) as excinfo:
        main(["rescan", "--socket", str(server.socket_path)])

    assert excinfo.value.code == EXIT_OK
    assert len(poll_calls) == 1
    assert len(resync_calls) == 1


@pytest.mark.requires_af_unix
def test_rescan_reports_skipped_locked(server, store, locks, capsys):
    store.upsert_attached(UID_LOCKED_GONE, "/dev/ttyACM2", VID_PID)
    store.mark_disconnected(UID_LOCKED_GONE)
    locks.acquire(
        UID_LOCKED_GONE, KIND_SERIAL, HolderRef(origin="local", ref="4821", pid=4821)
    )

    with pytest.raises(SystemExit) as excinfo:
        main(["rescan", "--socket", str(server.socket_path)])

    assert excinfo.value.code == EXIT_OK
    out = capsys.readouterr().out
    assert "skipped 1 locked" in out
    assert "cccc3333" in out
    # The unlocked gone row is still removed in the same call.
    assert "removed 1 device" in out
    assert store.get(UID_LOCKED_GONE) is not None
    assert store.get(UID_GONE) is None


@pytest.mark.requires_af_unix
def test_rescan_with_nothing_to_remove_says_so(server, store, capsys):
    # Clear the one gone row first via a real rescan, then rescan again.
    with pytest.raises(SystemExit) as excinfo:
        main(["rescan", "--socket", str(server.socket_path)])
    assert excinfo.value.code == EXIT_OK
    capsys.readouterr()  # discard first run's output

    with pytest.raises(SystemExit) as excinfo:
        main(["rescan", "--socket", str(server.socket_path)])

    assert excinfo.value.code == EXIT_OK
    out = capsys.readouterr().out
    assert "nothing to remove" in out


# ---------------------------------------------------------------------------
# --dry-run -- no fresh table, "would" wording
# ---------------------------------------------------------------------------


@pytest.mark.requires_af_unix
def test_rescan_dry_run_reports_without_mutating_or_printing_a_table(
    server, store, poll_calls, resync_calls, capsys
):
    with pytest.raises(SystemExit) as excinfo:
        main(["rescan", "--dry-run", "--socket", str(server.socket_path)])

    assert excinfo.value.code == EXIT_OK
    out = capsys.readouterr().out
    assert "would remove 1 device" in out
    # No fresh list table -- SUC-002: nothing changed, so no second blob.
    assert "STATE" not in out
    assert store.get(UID_GONE) is not None
    assert len(poll_calls) == 0
    assert len(resync_calls) == 0


# ---------------------------------------------------------------------------
# --json -- one combined object, no human sentence
# ---------------------------------------------------------------------------


@pytest.mark.requires_af_unix
def test_rescan_json_includes_removed_and_fresh_devices_in_one_object(server, store, capsys):
    with pytest.raises(SystemExit) as excinfo:
        main(["rescan", "--json", "--socket", str(server.socket_path)])

    assert excinfo.value.code == EXIT_OK
    out = capsys.readouterr().out
    # Exactly one JSON object on stdout -- not two separate blobs.
    payload = json.loads(out)
    assert payload["dry_run"] is False
    assert payload["removed"]["devices"] == [UID_GONE]
    assert payload["skipped_locked"] == []
    uids = {d["uid"] for d in payload["devices"]}
    assert UID_ATTACHED in uids
    assert UID_GONE not in uids


@pytest.mark.requires_af_unix
def test_rescan_dry_run_json_has_no_devices_key(server, store, capsys):
    with pytest.raises(SystemExit) as excinfo:
        main(["rescan", "--dry-run", "--json", "--socket", str(server.socket_path)])

    assert excinfo.value.code == EXIT_OK
    out = capsys.readouterr().out
    payload = json.loads(out)
    assert payload["dry_run"] is True
    assert payload["removed"]["devices"] == [UID_GONE]
    assert "devices" not in payload


# ---------------------------------------------------------------------------
# absent socket
# ---------------------------------------------------------------------------


def test_rescan_against_absent_socket_prints_clear_message_and_stable_exit_code(
    tmp_path, capsys
):
    missing = tmp_path / "no-such-daemon.sock"

    with pytest.raises(SystemExit) as excinfo:
        main(["rescan", "--socket", str(missing)])

    assert excinfo.value.code == EXIT_NO_DAEMON
    err = capsys.readouterr().err
    assert "registry unavailable" in err
    assert "Traceback" not in err
