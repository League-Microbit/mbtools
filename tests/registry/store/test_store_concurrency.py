"""Concurrency stress tests for ``mbtools.registry.store.Store``.

Additional scope (team-lead, sprint 002 ticket 008 dispatch, not part of
that ticket's own acceptance criteria): ``Store`` is shared across the
daemon's own poll thread, every ``RegistryAPIServer`` per-connection
handler thread, and (in some tests) the test's own main thread, all
against one ``sqlite3.Connection`` opened with ``check_same_thread=False``
and, until now, no internal Python-level guard. Full-suite runs
intermittently hit ``sqlite3.InterfaceError``/``IndexError`` inside
``store._row_to_record`` (reproduced via
``tests/registry/cli/test_cli_run.py::test_run_then_list_smoke`` and
``tests/deploy/test_deploy_cli.py``'s end-to-end tests, both of which run
a real background daemon thread concurrently with API/CLI calls from the
test's own main thread) -- two threads interleaving a write-then-read
sequence can hand a half-updated or cursor-invalidated row back to
``_row_to_record``.

``Store`` now serializes every public method's use of its connection
through an internal ``threading.RLock`` (see its own class docstring's
"Thread safety" note). These tests drive that lock directly and hard,
with real ``threading.Thread``s (not mocked), to prove it holds under
genuine concurrent read/write pressure -- not just to re-demonstrate the
single-threaded CRUD behavior ``test_store.py`` already covers.
"""

from __future__ import annotations

import threading

import pytest

from mbtools.registry.identity import ProbeResult, short_uid
from mbtools.registry.store import STATE_DISCONNECTED, Store, format_vid_pid

VID_PID = format_vid_pid(0x0D28, 0x0204)


def _uid(tag: str) -> str:
    unique = (tag * 4)[:16]
    return "9900" + "0000" + "11112222" + unique + "77778888" + "6e052820"


@pytest.fixture
def store(tmp_path):
    s = Store(tmp_path / "devices.db")
    yield s
    s.close()


def _run_threads(workers: list[threading.Thread], timeout: float = 30.0) -> None:
    for t in workers:
        t.start()
    for t in workers:
        t.join(timeout=timeout)
        assert not t.is_alive(), "worker thread did not finish -- possible deadlock"


# ---------------------------------------------------------------------------
# many threads, each hammering its own uid -- proves the lock doesn't
# corrupt unrelated rows or raise under concurrent full-method-suite use
# ---------------------------------------------------------------------------


def test_concurrent_per_uid_readers_and_writers_do_not_corrupt_or_raise(store):
    n_threads = 16
    n_iters = 60
    uids = [_uid(f"conc{i:04d}") for i in range(n_threads)]

    for uid in uids:
        store.upsert_attached(uid, "/dev/ttyACM0", VID_PID)

    errors: list[BaseException] = []
    errors_lock = threading.Lock()

    def worker(uid: str) -> None:
        try:
            for i in range(n_iters):
                store.upsert_attached(uid, f"/dev/ttyACM{i % 3}", VID_PID)
                store.apply_probe_result(
                    uid,
                    ProbeResult(
                        role="NEZHA2",
                        common_name="robot",
                        device_name=f"name{i % 5}",
                        serial=str(i),
                        raw="raw",
                    ),
                )
                store.increment_flash_count(uid)
                assert store.get(uid) is not None
                assert store.find(uid) is not None
                assert store.needs_probe(uid) is False
                # Exercises the full-table scan path concurrently with
                # every other thread's per-uid writes -- this is exactly
                # the shape (one thread's UPDATE+commit landing between
                # another thread's execute/fetchone) that produced the
                # intermittent sqlite3.InterfaceError/IndexError.
                all_devices = store.list_devices()
                assert any(d.uid == uid for d in all_devices)
        except BaseException as exc:  # noqa: BLE001 -- record, don't swallow
            with errors_lock:
                errors.append(exc)

    _run_threads([threading.Thread(target=worker, args=(uid,)) for uid in uids])

    assert not errors, f"{len(errors)} worker thread(s) raised: {errors!r}"

    for uid in uids:
        record = store.get(uid)
        assert record is not None
        assert record.flash_count == n_iters
        assert record.short_uid == short_uid(uid)


# ---------------------------------------------------------------------------
# many threads hammering the SAME uid -- maximum contention, and proves
# increment_flash_count's read-modify-write is atomic under the lock
# ---------------------------------------------------------------------------


def test_concurrent_writers_on_one_shared_uid_stay_consistent(store):
    n_threads = 20
    n_iters = 50
    uid = _uid("shared11")
    store.upsert_attached(uid, "/dev/ttyACM0", VID_PID)

    errors: list[BaseException] = []
    errors_lock = threading.Lock()

    def worker() -> None:
        try:
            for _ in range(n_iters):
                store.increment_flash_count(uid)
                record = store.get(uid)
                assert record is not None
                assert record.uid == uid
        except BaseException as exc:  # noqa: BLE001
            with errors_lock:
                errors.append(exc)

    _run_threads([threading.Thread(target=worker) for _ in range(n_threads)])

    assert not errors, f"{len(errors)} worker thread(s) raised: {errors!r}"
    record = store.get(uid)
    assert record is not None
    # Every increment must have landed exactly once -- if the internal
    # lock let two threads interleave a read-modify-write on flash_count,
    # this total would come up short.
    assert record.flash_count == n_threads * n_iters


# ---------------------------------------------------------------------------
# a mix of mark_disconnected/upsert_attached reattach races on one uid --
# exercises the branch (existing.state == STATE_DISCONNECTED) that itself
# calls self.get() from inside an already-held lock (reentrancy)
# ---------------------------------------------------------------------------


def test_concurrent_disconnect_reattach_cycles_stay_consistent(store):
    n_threads = 8
    n_iters = 40
    uid = _uid("cycle111")
    store.upsert_attached(uid, "/dev/ttyACM0", VID_PID)

    errors: list[BaseException] = []
    errors_lock = threading.Lock()

    def worker() -> None:
        try:
            for _ in range(n_iters):
                store.mark_disconnected(uid)
                record = store.upsert_attached(uid, "/dev/ttyACM0", VID_PID)
                assert record.state != STATE_DISCONNECTED
        except BaseException as exc:  # noqa: BLE001
            with errors_lock:
                errors.append(exc)

    _run_threads([threading.Thread(target=worker) for _ in range(n_threads)])

    assert not errors, f"{len(errors)} worker thread(s) raised: {errors!r}"
    assert store.get(uid) is not None
