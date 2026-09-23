"""mbtools.registry.store — SQLite device database and re-probe-eligibility
fields.

Per sprint.md's Architecture (module "store", the ERD, and Design Rationale
"SQLite for the store (ASSUMPTION)"), this is a SQLite-backed, single-table
(``device``) store, machine-level (not per-user), living under
``/var/lib/mbregistry/`` in production and overridable to a ``tmp_path`` in
tests. WAL mode is turned on per the same Design Rationale entry ("no design
here for multi-writer contention beyond SQLite's own file locking (WAL
mode)") — the daemon is the only writer, clients only read via the API
(ticket 008), never the database file directly.

``store`` is deliberately the *only* place that knows the schema. The
re-probe rule ("never reopen unless reattached or flashed") is enforced by
``mbtools.registry.daemon`` (ticket 006) reading ``state``/``last_probe``
from here — this module's job is correct persistence, not policy. The one
piece of policy this module *does* encode is what "correct persistence"
means for a reattach: :meth:`Store.upsert_attached` resets ``last_probe``
to ``0.0`` only when the record's previous ``state`` was ``disconnected``
(a genuine reattach — the prior probe session ended when the device went
away) or the record is brand new. A record that was already
``attached_unprobed``/``connected``/``connected_no_firmware`` (e.g. the
daemon restarting and rescanning devices that never actually left) keeps
its ``last_probe`` untouched, so :meth:`Store.needs_probe` correctly
reports "already probed, still attached" per SUC-007's "daemon restart is
not a reattach" postcondition.

Precedent for "never delete, always update in place" comes from both
predecessor tools: ``mbdeploy/devices.py``'s registry-layer docstring
("Entries are never deleted... Prior announcement fields are preserved
when ``probe_type`` returns None") and ``mbrelay/inventory.py``'s module
docstring ("identity is cached to disk, a FREE board already in the cache
is not re-probed"). ``store`` follows the same convention: a
``disconnected`` record is updated, never dropped, and a failed probe
(``apply_probe_result(uid, None)``) never clobbers an existing
announcement field it didn't get fresh data for.
"""

from __future__ import annotations

import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from mbtools.registry.identity import ProbeResult, short_uid

__all__ = [
    "DeviceRecord",
    "Store",
    "STATE_ATTACHED_UNPROBED",
    "STATE_CONNECTED",
    "STATE_CONNECTED_NO_FIRMWARE",
    "STATE_DISCONNECTED",
    "DEFAULT_DB_PATH",
    "format_vid_pid",
]

#: Production default for the SQLite file — under ``/var/lib/mbregistry/``
#: per sprint.md's Design Rationale "file layout (ASSUMPTION)": identity
#: worth keeping across reboots. Every test overrides this to a ``tmp_path``
#: (see sprint.md's Test Strategy: "store CRUD runs against a real SQLite
#: file in ``tmp_path``").
DEFAULT_DB_PATH = Path("/var/lib/mbregistry/devices.db")

# Device lifecycle states, per sprint.md's ERD §4.
STATE_ATTACHED_UNPROBED = "attached_unprobed"
STATE_CONNECTED = "connected"
STATE_CONNECTED_NO_FIRMWARE = "connected_no_firmware"
STATE_DISCONNECTED = "disconnected"

#: error_note text set by apply_probe_result(uid, None) — a probe that
#: opened the port (or tried to) but got no announcement within the probe
#: window. Kept as one constant so the API layer (ticket 008) and any test
#: asserting on it can't drift out of sync with each other.
_ERROR_NOTE_NO_ANNOUNCEMENT = "no announcement received during probe"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS device (
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
    last_probe REAL NOT NULL DEFAULT 0.0
)
"""


def format_vid_pid(vid: int, pid: int) -> str:
    """Format a USB (vid, pid) pair the way the ``device.vid_pid`` column
    stores it -- lowercase 4-digit hex, colon-separated (``"0d28:0204"``).

    A shared helper so every caller that has a
    :class:`mbtools.common.PortInfo` (``usbwatch``, ``daemon``) formats the
    column the same way, rather than each re-inventing the format and
    drifting apart -- the same one-place reasoning
    :data:`mbtools.common.DAPLINK_VID_PID` itself exists for.
    """
    return f"{vid:04x}:{pid:04x}"


@dataclass(frozen=True)
class DeviceRecord:
    """One ``device`` row, per sprint.md's ERD §4.

    ``role``/``common_name``/``device_name``/``serial_payload`` are
    ``None`` until a successful probe. ``port`` is left at its last known
    value when a device disconnects (see :meth:`Store.mark_disconnected`)
    -- it is stale once ``state == "disconnected"``, but keeping the last
    value is a diagnostic nicety and callers must not treat it as live
    without checking ``state`` first. ``last_probe == 0.0`` means "never
    probed" (see :meth:`Store.needs_probe`).
    """

    uid: str
    short_uid: str
    port: str | None
    vid_pid: str | None
    role: str | None
    common_name: str | None
    device_name: str | None
    serial_payload: str | None
    raw_announcement: str | None
    state: str
    error_note: str | None
    flash_count: int
    first_seen: float
    last_seen: float
    last_probe: float


def _row_to_record(row: sqlite3.Row) -> DeviceRecord:
    return DeviceRecord(
        uid=row["uid"],
        short_uid=row["short_uid"],
        port=row["port"],
        vid_pid=row["vid_pid"],
        role=row["role"],
        common_name=row["common_name"],
        device_name=row["device_name"],
        serial_payload=row["serial_payload"],
        raw_announcement=row["raw_announcement"],
        state=row["state"],
        error_note=row["error_note"],
        flash_count=row["flash_count"],
        first_seen=row["first_seen"],
        last_seen=row["last_seen"],
        last_probe=row["last_probe"],
    )


class Store:
    """The persistent device database -- one record per device, keyed by
    ``uid``, never deleted.

    ``db_path`` defaults to :data:`DEFAULT_DB_PATH`; every test overrides it
    to a ``tmp_path`` file. Parent directories are created if missing (mirrors
    ``mbdeploy``'s ``save_devices``'s ``config_path.parent.mkdir(parents=True,
    exist_ok=True)``). The schema is created on first use
    (``CREATE TABLE IF NOT EXISTS`` -- no migration framework needed for a v1
    single-table schema).

    ``now_fn`` is a test-only escape hatch, mirroring the injectable seams
    ``mbtools.registry.identity.probe``'s ``serial_factory`` and
    ``mbtools.registry.usbwatch.PollingPortWatcher``'s ``comports_fn``
    already establish: a test can pass a deterministic clock instead of
    depending on real wall-clock time to assert on ``first_seen``/
    ``last_seen``/``last_probe``. Production code leaves it at its default
    (``time.time``).
    """

    def __init__(
        self,
        db_path: str | Path | None = None,
        *,
        now_fn: Callable[[], float] | None = None,
    ) -> None:
        self.db_path = Path(db_path) if db_path is not None else DEFAULT_DB_PATH
        self._now = now_fn if now_fn is not None else time.time
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(self.db_path))
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute(_SCHEMA)
        self._conn.commit()

    def close(self) -> None:
        """Close the underlying SQLite connection."""
        self._conn.close()

    # -- writes ---------------------------------------------------------

    def upsert_attached(self, uid: str, port: str, vid_pid: str) -> DeviceRecord:
        """Record ``uid`` as currently attached on ``port``.

        Creates a new ``attached_unprobed`` record for a uid the store has
        never seen, or refreshes ``port``/``last_seen`` on an existing one
        -- without touching announcement fields (``role``/``common_name``/
        ``device_name``/``serial_payload``/``raw_announcement``) either
        way, per the "preserve existing announcement fields unchanged"
        rule this module ports from ``mbdeploy``/``mbrelay``.

        A *new* record, and an existing record whose ``state`` was
        ``disconnected`` (a genuine reattach), also get ``state`` reset to
        ``attached_unprobed`` and ``last_probe`` reset to ``0.0`` -- the
        prior probe session ended when the device went away, so
        :meth:`needs_probe` must report "needs probing" again. An existing
        record already in ``attached_unprobed``/``connected``/
        ``connected_no_firmware`` (e.g. the daemon restarting and
        rescanning a device that was attached the whole time) keeps its
        ``state`` and ``last_probe`` untouched -- see the module docstring.
        """
        now = self._now()
        existing = self.get(uid)
        if existing is None:
            self._conn.execute(
                """
                INSERT INTO device (
                    uid, short_uid, port, vid_pid, state,
                    flash_count, first_seen, last_seen, last_probe
                ) VALUES (?, ?, ?, ?, ?, 0, ?, ?, 0.0)
                """,
                (uid, short_uid(uid), port, vid_pid, STATE_ATTACHED_UNPROBED, now, now),
            )
        elif existing.state == STATE_DISCONNECTED:
            self._conn.execute(
                """
                UPDATE device
                SET port = ?, vid_pid = ?, state = ?, last_seen = ?, last_probe = 0.0
                WHERE uid = ?
                """,
                (port, vid_pid, STATE_ATTACHED_UNPROBED, now, uid),
            )
        else:
            self._conn.execute(
                "UPDATE device SET port = ?, vid_pid = ?, last_seen = ? WHERE uid = ?",
                (port, vid_pid, now, uid),
            )
        self._conn.commit()
        record = self.get(uid)
        assert record is not None  # just written
        return record

    def apply_probe_result(self, uid: str, result: ProbeResult | None) -> DeviceRecord:
        """Apply the outcome of an identity probe to ``uid``'s record.

        On a successful probe (``result`` is a :class:`ProbeResult` --
        including the malformed-announcement case, where its fields are
        blank but a line *was* received, per ``identity.probe``'s own
        docstring): updates ``role``/``common_name``/``device_name``/
        ``serial_payload``/``raw_announcement`` from ``result``, sets
        ``state="connected"``, clears any stale ``error_note``, and
        updates ``last_probe``/``last_seen``.

        On ``None`` (no announcement arrived within the probe window):
        sets ``state="connected_no_firmware"`` and an ``error_note``, but
        leaves any previously-known announcement fields untouched --
        mirrors ``mbdeploy``'s "preserve existing announcement fields
        unchanged" rule. ``last_probe``/``last_seen`` are still updated --
        a probe was attempted, so this uid is no longer "never probed".

        Raises :class:`KeyError` if ``uid`` has no existing record -- the
        pipeline always calls :meth:`upsert_attached` before probing.
        """
        if self.get(uid) is None:
            raise KeyError(f"store.apply_probe_result: no record for uid {uid!r}")
        now = self._now()
        if result is not None:
            self._conn.execute(
                """
                UPDATE device
                SET role = ?, common_name = ?, device_name = ?, serial_payload = ?,
                    raw_announcement = ?, state = ?, error_note = NULL,
                    last_probe = ?, last_seen = ?
                WHERE uid = ?
                """,
                (
                    result.role,
                    result.common_name,
                    result.device_name,
                    result.serial,
                    result.raw,
                    STATE_CONNECTED,
                    now,
                    now,
                    uid,
                ),
            )
        else:
            self._conn.execute(
                """
                UPDATE device
                SET state = ?, error_note = ?, last_probe = ?, last_seen = ?
                WHERE uid = ?
                """,
                (STATE_CONNECTED_NO_FIRMWARE, _ERROR_NOTE_NO_ANNOUNCEMENT, now, now, uid),
            )
        self._conn.commit()
        record = self.get(uid)
        assert record is not None
        return record

    def mark_disconnected(self, uid: str) -> DeviceRecord:
        """Mark ``uid`` as ``disconnected`` and update ``last_seen``.

        The record is never deleted -- per this module's "never delete,
        always update in place" precedent -- so a later
        :meth:`list_devices` call still returns it (UC-004's "gone, not
        silently dropped" requirement).

        Raises :class:`KeyError` if ``uid`` has no existing record.
        """
        if self.get(uid) is None:
            raise KeyError(f"store.mark_disconnected: no record for uid {uid!r}")
        now = self._now()
        self._conn.execute(
            "UPDATE device SET state = ?, last_seen = ? WHERE uid = ?",
            (STATE_DISCONNECTED, now, uid),
        )
        self._conn.commit()
        record = self.get(uid)
        assert record is not None
        return record

    def increment_flash_count(self, uid: str) -> DeviceRecord:
        """Bump ``flash_count`` by one -- called by ticket 007's flash op
        on completion.

        Raises :class:`KeyError` if ``uid`` has no existing record.
        """
        if self.get(uid) is None:
            raise KeyError(f"store.increment_flash_count: no record for uid {uid!r}")
        self._conn.execute(
            "UPDATE device SET flash_count = flash_count + 1 WHERE uid = ?", (uid,)
        )
        self._conn.commit()
        record = self.get(uid)
        assert record is not None
        return record

    # -- reads ------------------------------------------------------------

    def needs_probe(self, uid: str) -> bool:
        """Does the store-data half of the re-probe rule say ``uid`` needs
        probing?

        True for a never-probed device (``last_probe == 0.0``), false for
        an already-probed device. The "was it reattached" trigger is
        already folded into ``last_probe`` by :meth:`upsert_attached`
        (which resets it to ``0.0`` on a genuine reattach); the "was it
        flashed" trigger is ticket 006/007's job -- this method only
        answers the store-data half of the rule, per the ticket's own
        scoping.

        Raises :class:`KeyError` if ``uid`` has no existing record.
        """
        record = self.get(uid)
        if record is None:
            raise KeyError(f"store.needs_probe: no record for uid {uid!r}")
        return record.last_probe == 0.0

    def get(self, uid: str) -> DeviceRecord | None:
        """Exact lookup by ``uid``. ``None`` if no such device."""
        row = self._conn.execute(
            "SELECT * FROM device WHERE uid = ?", (uid,)
        ).fetchone()
        return _row_to_record(row) if row is not None else None

    def find(self, token: str) -> DeviceRecord | None:
        """Resolve a user-supplied ``token`` to a device record.

        Precedence -- the same family as ``mbdeploy``'s ``resolve_target``,
        minus its numeric-enum and port-path cases, which don't apply at
        the store layer (no ``enum`` field here; a port path is resolved
        by the caller against a live scan, per ``resolve_target``'s own
        docstring, not looked up in the store):

        1. Exact match against ``uid``.
        2. Exact match against ``short_uid``. Note ``short_uid`` is *not*
           guaranteed unique across every board on a bench (two boards can
           share the interface-chip tail that ``short_uid`` derives from
           -- see ``identity.short_uid``'s own docstring); an ambiguous
           match returns whichever row SQLite happens to return first,
           same "good enough for a bench" spirit as the rest of this
           sprint's scale assumptions.
        3. Case-insensitive match against ``device_name``.

        ``None`` if none of the three match.
        """
        row = self._conn.execute(
            "SELECT * FROM device WHERE uid = ?", (token,)
        ).fetchone()
        if row is None:
            row = self._conn.execute(
                "SELECT * FROM device WHERE short_uid = ?", (token,)
            ).fetchone()
        if row is None:
            row = self._conn.execute(
                "SELECT * FROM device WHERE device_name = ? COLLATE NOCASE", (token,)
            ).fetchone()
        return _row_to_record(row) if row is not None else None

    def list_devices(self) -> list[DeviceRecord]:
        """Every record, including ``disconnected`` ones (UC-004's "gone,
        not silently dropped" requirement).

        Iteration is not required to be performant beyond "all records fit
        in memory," per spec §3.4.
        """
        rows = self._conn.execute("SELECT * FROM device").fetchall()
        return [_row_to_record(row) for row in rows]
