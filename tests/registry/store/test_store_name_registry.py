"""Tests for sprint 004, ticket 001's additions to mbtools.registry.store --
the ``name_registry`` table and its resolve/get_name/set/clear/name_for/
conflicts/channel_conflicts/listing methods.

Runs against a real SQLite file in tmp_path, same convention as
test_store.py/test_store_peer_host.py -- no mock database layer.
"""

from __future__ import annotations

import itertools
import sqlite3
import threading

import pytest

from mbtools.relay import naming
from mbtools.registry.identity import short_uid
from mbtools.registry.store import (
    SOURCE_DERIVED,
    SOURCE_REGISTRY,
    Store,
    format_vid_pid,
)

UID = "9900" + "0000" + "11112222" + "3333444455556666" + "77778888" + "6e052820"
VID_PID = format_vid_pid(0x0D28, 0x0204)

# Three well-formed names (CVCVC over zvgpt/uoiea) picked because their
# derived addresses (mbtools.relay.naming.name_to_radio) are all distinct --
# any name-registry conflict below is therefore one this test deliberately
# creates via Store.set, never one the derivation itself could produce
# (naming.py's whole point is that every name has its own pair).
NAME_A = "zuzuz"
NAME_B = "tatat"
NAME_C = "zavaz"
NAME_UNSEEN = "vevov"


def _clock(start: float = 1000.0, step: float = 1.0):
    counter = itertools.count()
    return lambda: start + step * next(counter)


@pytest.fixture
def store(tmp_path):
    return Store(tmp_path / "devices.db", now_fn=_clock())


# ---------------------------------------------------------------------------
# Schema creation
# ---------------------------------------------------------------------------


def _create_sprint_3_shape_db(db_path) -> None:
    """Build a database with exactly the sprint-1/2/3 shape (device + peer,
    no ``name_registry``), pre-populated with one device row -- mirroring a
    live sprint-3 deployment's ``devices.db`` before this ticket's migration
    runs against it."""
    conn = sqlite3.connect(str(db_path))
    conn.execute(
        """
        CREATE TABLE device (
            uid TEXT PRIMARY KEY,
            short_uid TEXT NOT NULL,
            port TEXT,
            vid_pid TEXT,
            role TEXT,
            common_name TEXT,
            device_name TEXT,
            serial_payload TEXT,
            raw_announcement TEXT,
            state TEXT NOT NULL,
            error_note TEXT,
            flash_count INTEGER NOT NULL DEFAULT 0,
            first_seen REAL NOT NULL,
            last_seen REAL NOT NULL,
            last_probe REAL NOT NULL DEFAULT 0.0,
            host TEXT,
            remote_lock_kind TEXT,
            remote_lock_display TEXT
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE peer (
            host TEXT PRIMARY KEY,
            endpoint TEXT NOT NULL,
            last_seen REAL NOT NULL,
            reachable INTEGER NOT NULL DEFAULT 0
        )
        """
    )
    conn.execute(
        """
        INSERT INTO device (
            uid, short_uid, port, vid_pid, role, common_name, device_name,
            serial_payload, raw_announcement, state, error_note, flash_count,
            first_seen, last_seen, last_probe, host, remote_lock_kind, remote_lock_display
        ) VALUES (?, ?, '/dev/ttyACM0', ?, 'NEZHA2', 'robot', 'vevov', '123',
                  'raw', 'connected', NULL, 2, 1000.0, 1000.0, 1000.0, NULL, NULL, NULL)
        """,
        (UID, short_uid(UID), VID_PID),
    )
    conn.commit()
    conn.close()


def test_name_registry_table_created_on_fresh_database(tmp_path):
    db_path = tmp_path / "devices.db"
    assert not db_path.exists()

    store = Store(db_path, now_fn=_clock())

    entry = store.resolve(NAME_A)
    assert entry.name == NAME_A


def test_name_registry_table_created_on_existing_sprint_3_shape_database(tmp_path):
    db_path = tmp_path / "devices.db"
    _create_sprint_3_shape_db(db_path)

    store = Store(db_path, now_fn=_clock())

    # Existing device row untouched by the migration.
    record = store.get(UID)
    assert record is not None
    assert record.device_name == "vevov"
    # New table exists and is usable.
    assert store.get_name(NAME_A) is None
    entry = store.resolve(NAME_A)
    assert entry.source == SOURCE_DERIVED


def test_reopening_db_does_not_re_migrate_or_lose_name_registry_data(tmp_path):
    db_path = tmp_path / "devices.db"
    _create_sprint_3_shape_db(db_path)

    first = Store(db_path, now_fn=_clock())
    first.set(NAME_A, 20, 30)
    first.close()

    second = Store(db_path, now_fn=_clock())
    entry = second.get_name(NAME_A)
    assert entry is not None
    assert (entry.channel, entry.group) == (20, 30)
    assert entry.source == SOURCE_REGISTRY


# ---------------------------------------------------------------------------
# resolve / get_name
# ---------------------------------------------------------------------------


def test_resolve_derives_and_persists_on_first_ask(store):
    expected_channel, expected_group = naming.name_to_radio(NAME_UNSEEN)

    entry = store.resolve(NAME_UNSEEN)

    assert entry.name == NAME_UNSEEN
    assert (entry.channel, entry.group) == (expected_channel, expected_group)
    assert entry.source == SOURCE_DERIVED
    assert entry.updated > 0
    # Persisted -- a non-creating lookup now finds it too.
    assert store.get_name(NAME_UNSEEN) == entry


def test_resolve_returns_existing_entry_unchanged_on_repeat_ask(store):
    first = store.resolve(NAME_UNSEEN)
    second = store.resolve(NAME_UNSEEN)

    assert second == first  # same updated timestamp -- not re-derived/re-stamped


def test_resolve_normalizes_and_rejects_malformed_names(store):
    assert store.resolve(" ZUZUZ ").name == "zuzuz"
    with pytest.raises(ValueError):
        store.resolve("robot1")


def test_get_name_does_not_create(store):
    assert store.get_name(NAME_UNSEEN) is None
    assert store.resolve(NAME_UNSEEN) is not None
    assert store.get_name(NAME_UNSEEN) is not None


def test_resolve_is_idempotent_under_concurrent_calls_for_the_same_unseen_name(store):
    expected = naming.name_to_radio(NAME_UNSEEN)
    results: list = []
    errors: list = []

    def _resolve():
        try:
            results.append(store.resolve(NAME_UNSEEN))
        except Exception as exc:  # pragma: no cover - failure path only
            errors.append(exc)

    threads = [threading.Thread(target=_resolve) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert errors == []
    assert len(results) == 8
    # Every thread saw the same deterministic derived pair/source, and the
    # store settled on exactly one row for the name.
    assert all((e.channel, e.group) == expected for e in results)
    assert all(e.source == SOURCE_DERIVED for e in results)
    assert len({e.updated for e in results}) == 1


# ---------------------------------------------------------------------------
# set / clear
# ---------------------------------------------------------------------------


def test_set_persists_registry_source(store):
    entry = store.set(NAME_A, 20, 30)

    assert (entry.channel, entry.group) == (20, 30)
    assert entry.source == SOURCE_REGISTRY
    assert store.get_name(NAME_A) == entry


def test_set_overwrites_a_previously_derived_entry(store):
    derived = store.resolve(NAME_A)
    assert derived.source == SOURCE_DERIVED

    overridden = store.set(NAME_A, 50, 60)

    assert (overridden.channel, overridden.group) == (50, 60)
    assert overridden.source == SOURCE_REGISTRY


def test_clear_deletes_the_row_and_a_later_resolve_rederives(store):
    store.set(NAME_A, 50, 60)
    assert store.get_name(NAME_A) is not None

    store.clear(NAME_A)

    assert store.get_name(NAME_A) is None
    rederived = store.resolve(NAME_A)
    assert rederived.source == SOURCE_DERIVED
    assert (rederived.channel, rederived.group) == naming.name_to_radio(NAME_A)


def test_clear_on_an_unseen_name_is_not_an_error(store):
    store.clear(NAME_A)  # no row exists yet -- must not raise
    assert store.get_name(NAME_A) is None


# ---------------------------------------------------------------------------
# name_for
# ---------------------------------------------------------------------------


def test_name_for_derived_pair_answers_even_before_anyone_resolved_it(store):
    """The derived bijection is a free answer -- name_for doesn't require a
    prior resolve()/set() to have touched the row, only that nothing has
    since moved the name off its own default (see the "has moved" test
    below)."""
    channel, group = naming.name_to_radio(NAME_A)
    assert store.get_name(NAME_A) is None  # nothing on record yet

    assert store.name_for(channel, group) == NAME_A


def test_name_for_prefers_explicit_registry_entry(store):
    store.set(NAME_A, 20, 30)
    assert store.name_for(20, 30) == NAME_A


def test_name_for_returns_none_when_the_derived_name_has_moved(store):
    channel, group = naming.name_to_radio(NAME_A)
    store.resolve(NAME_A)  # NAME_A now sits on its derived pair
    store.set(NAME_A, channel + 1, group)  # move it elsewhere via an override

    # The vacated derived pair no longer belongs to NAME_A.
    assert store.name_for(channel, group) is None


def test_name_for_returns_none_for_a_pair_no_name_derives(store):
    with pytest.raises(ValueError):
        naming.radio_to_name(0, 10)  # sanity: confirms this pair has no name
    assert store.name_for(0, 10) is None


# ---------------------------------------------------------------------------
# conflicts / channel_conflicts / listing
# ---------------------------------------------------------------------------


def test_conflicts_flags_two_names_sharing_a_pair_as_error_severity(store):
    store.set(NAME_A, 20, 30)
    store.set(NAME_B, 20, 30)
    store.set(NAME_C, 20, 31)  # same channel, different group -- not this conflict

    conflicts = store.conflicts()

    assert set(conflicts) == {(20, 30)}
    assert set(conflicts[(20, 30)]) == {NAME_A, NAME_B}


def test_channel_conflicts_flags_same_channel_different_group_as_warning_severity(store):
    store.set(NAME_A, 20, 30)
    store.set(NAME_B, 20, 30)
    store.set(NAME_C, 20, 31)

    channel_conflicts = store.channel_conflicts()

    assert 20 in channel_conflicts
    assert set(channel_conflicts[20]) == {NAME_A, NAME_B, NAME_C}


def test_channel_conflicts_does_not_flag_a_channel_used_by_only_one_group(store):
    store.set(NAME_A, 20, 30)
    store.set(NAME_B, 20, 30)  # exact-pair conflict, but only one group on ch 20

    assert store.channel_conflicts() == {}


def test_listing_returns_every_row_annotated_with_conflict_names(store):
    store.set(NAME_A, 20, 30)
    store.set(NAME_B, 20, 30)
    store.set(NAME_C, 20, 31)

    rows = {e.name: e for e in store.listing()}

    assert set(rows) == {NAME_A, NAME_B, NAME_C}
    assert set(rows[NAME_A].conflict) == {NAME_B}
    assert set(rows[NAME_B].conflict) == {NAME_A}
    assert rows[NAME_C].conflict == ()
    assert set(rows[NAME_A].channel_conflict) == {NAME_C}
    assert set(rows[NAME_C].channel_conflict) == {NAME_A, NAME_B}


def test_listing_on_an_empty_registry_is_empty(store):
    assert store.listing() == []
