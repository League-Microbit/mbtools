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
``attached_unprobed``/``connected``/``attached_no_announce``/
``connected_no_firmware`` (e.g. the
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

**Sprint 003 addition — peers and remote-owned devices.** Per sprint.md's
Step 5 ("What Changed") and its ERD, this module also persists
``registry.peering``'s view of the fleet: a nullable ``device.host``
column (``NULL`` means this row is locally owned; otherwise the owning
peer's hostname), two display-only cache columns
(``remote_lock_kind``/``remote_lock_display``) a remote-owned row's
lock state is mirrored into (never consulted for a local row, which
always reads live ``LockManager`` state instead — Decision 3), and a new
``peer`` table tracking every discovered registry's endpoint and
reachability (Decision 5: a vanished peer is marked unreachable, never
deleted, matching this module's own "never delete" precedent one level
up). :meth:`Store.find` gains an optional ``name@host`` suffix and a new
:class:`AmbiguousNameError` for a bare name that collides across hosts,
per the stakeholder's explicit ``name@host`` disambiguation requirement
(SUC-002).
"""

from __future__ import annotations

import logging
import sqlite3
import threading
import time
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Callable

from mbtools.registry.identity import ProbeResult, short_uid
from mbtools.registry.paths import default_db_path
from mbtools.relay import naming

logger = logging.getLogger(__name__)

__all__ = [
    "AmbiguousNameError",
    "DeviceRecord",
    "Entry",
    "PeerRecord",
    "Store",
    "STATE_ATTACHED_NO_ANNOUNCE",
    "STATE_ATTACHED_UNPROBED",
    "STATE_CONNECTED",
    "STATE_CONNECTED_NO_FIRMWARE",
    "STATE_DISCONNECTED",
    "SOURCE_DERIVED",
    "SOURCE_REGISTRY",
    "DEFAULT_DB_PATH",
    "format_vid_pid",
]

#: Production default for the SQLite file — under ``/var/lib/mbregistry/``
#: on Linux/macOS, under ``%ProgramData%\mbregistry\`` on Windows (an
#: ASSUMPTION, not a ratified decision — see ``registry.paths``'s module
#: docstring) per sprint.md's Design Rationale "file layout (ASSUMPTION)":
#: identity worth keeping across reboots. Every test overrides this to a
#: ``tmp_path`` (see sprint.md's Test Strategy: "store CRUD runs against a
#: real SQLite file in ``tmp_path``").
DEFAULT_DB_PATH = default_db_path()

# Device lifecycle states, per sprint.md's ERD §4.
STATE_ATTACHED_UNPROBED = "attached_unprobed"
STATE_CONNECTED = "connected"
#: Sprint 007, ticket 001: "we probed and got nothing back" -- the board
#: plausibly has *some* firmware, it just doesn't announce over serial
#: (blank student/robot firmware with no ``HELLO`` handler, or a firmware
#: bug). Set by :meth:`Store.apply_probe_result` when ``result`` is
#: ``None``. Distinct from :data:`STATE_CONNECTED_NO_FIRMWARE`, which is
#: now reserved for the case this store can actually *assert* the board
#: is blank (see that constant's own docstring below) -- see sprint.md's
#: Design Rationale "repurpose STATE_CONNECTED_NO_FIRMWARE for 'known
#: blank'".
STATE_ATTACHED_NO_ANNOUNCE = "attached_no_announce"
#: From sprint 007 forward, this means *only* "known blank" -- a case
#: this store can actually assert, principally the flash-triggered
#: re-probe that still gets nothing after a mass erase (see
#: :meth:`Store.apply_known_blank`). Before sprint 007 this value also
#: covered an ordinary silent probe with no assertion behind it; that
#: broader meaning is now :data:`STATE_ATTACHED_NO_ANNOUNCE` instead. A
#: pre-sprint-007 row already sitting at this value is not retroactively
#: reclassified -- it relabels correctly on its next real probe event
#: (sprint.md's Migration Concerns).
STATE_CONNECTED_NO_FIRMWARE = "connected_no_firmware"
STATE_DISCONNECTED = "disconnected"

#: error_note text set by apply_probe_result(uid, None) — a probe that
#: opened the port (or tried to) but got no announcement within the probe
#: window. Kept as one constant so the API layer (ticket 008) and any test
#: asserting on it can't drift out of sync with each other.
_ERROR_NOTE_NO_ANNOUNCEMENT = "no announcement received during probe"

#: error_note text set by :meth:`Store.apply_known_blank` -- a probe that
#: is *known* to have found the board blank (principally a flash-triggered
#: re-probe still getting nothing after a mass erase), as opposed to the
#: plain "didn't hear anything" case :data:`_ERROR_NOTE_NO_ANNOUNCEMENT`
#: covers.
_ERROR_NOTE_KNOWN_BLANK = "board confirmed blank (no firmware) after re-probe"

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

#: The three sprint-003 ``device`` columns (per the ERD), added in place to
#: an existing sprint-1/2 database. Deliberately *not* folded into
#: ``_SCHEMA`` above -- ``_SCHEMA``'s ``CREATE TABLE IF NOT EXISTS`` is a
#: no-op against an already-existing table, so a new column there would
#: never reach an upgraded database; keeping ``_SCHEMA`` at its original
#: sprint-1/2 shape and adding these via :meth:`Store._migrate_schema`
#: unconditionally (see that method's docstring) is what makes a fresh
#: database and an upgraded one converge on the same schema through the
#: same code path, per this ticket's acceptance criteria.
_NEW_DEVICE_COLUMNS: list[tuple[str, str]] = [
    ("host", "TEXT"),
    ("remote_lock_kind", "TEXT"),
    ("remote_lock_display", "TEXT"),
]

#: Sprint 007, ticket 001: the chip-identity cache -- a board's real name
#: and decimal serial as read from ``FICR.DEVICEID[1]`` over SWD (ticket
#: 003), independent of the announcement-derived ``device_name`` (which
#: goes blank again on a reflash to non-announcing firmware). A separate
#: list from ``_NEW_DEVICE_COLUMNS`` above -- that one is documented as
#: specifically "the three sprint-003 columns" -- added to an existing
#: ``device`` table via the same in-place, idempotent
#: ``ALTER TABLE ... ADD COLUMN`` pattern (see :meth:`Store._migrate_schema`),
#: so a pre-existing ``devices.db`` from before this ticket migrates
#: cleanly, same as sprint 003's own columns did.
_CHIP_IDENTITY_COLUMNS: list[tuple[str, str]] = [
    ("chip_identity_name", "TEXT"),
    ("chip_identity_serial", "INTEGER"),
]

_PEER_SCHEMA = """
CREATE TABLE IF NOT EXISTS peer (
    host TEXT PRIMARY KEY,
    endpoint TEXT NOT NULL,
    last_seen REAL NOT NULL,
    reachable INTEGER NOT NULL DEFAULT 0
)
"""

#: Sprint 004, ticket 001: the robot name -> (channel, group) mapping, ported
#: from ``microbit-radio-relay``'s ``names.json``-backed ``NameRegistry`` to a
#: table alongside ``device``/``peer`` -- a brand new table, so (like
#: ``_PEER_SCHEMA``) ``CREATE TABLE IF NOT EXISTS`` alone is idempotent on
#: both a fresh database and an existing sprint-1/2/3-shape one; no per-column
#: migration is needed (contrast ``_NEW_DEVICE_COLUMNS``, which adds columns
#: to an *existing* table). The column is named ``radio_group`` rather than
#: ``group`` only to keep every call site a plain, unquoted identifier --
#: ``group`` is a SQL keyword; the :class:`Entry` field stays ``group``, per
#: sprint.md's ERD and this ticket's Approach.
_NAME_REGISTRY_SCHEMA = """
CREATE TABLE IF NOT EXISTS name_registry (
    name TEXT PRIMARY KEY,
    channel INTEGER NOT NULL,
    radio_group INTEGER NOT NULL,
    source TEXT NOT NULL,
    updated REAL NOT NULL
)
"""

#: ``name_registry.source`` values -- sprint.md Decision 7 keeps only these
#: two tiers, dropping ``microbit-radio-relay``'s third ``pins`` (TOML)
#: tier: no per-host config-pin concept exists in ``mbtools``.
SOURCE_DERIVED = "derived"
SOURCE_REGISTRY = "registry"


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

    ``host`` is ``None`` for a device this registry itself owns (attached
    to a local port); otherwise it is the owning peer's hostname and this
    row was written by ``registry.peering`` applying a remote snapshot or
    event (sprint.md Step 5). ``remote_lock_kind``/``remote_lock_display``
    are a display-only cache of a remote-owned row's lock state, set only
    by :meth:`Store.apply_remote_lock_state` -- for a local row (``host``
    is ``None``) they are always ``None`` and must not be consulted;
    ``list``/``render`` read live ``LockManager`` state for a local row
    instead (Decision 3).

    ``chip_identity_name``/``chip_identity_serial`` (sprint 007, ticket
    001) are the board's real five-letter name and decimal serial, read
    once over SWD from ``FICR.DEVICEID[1]`` (ticket 003) and cached
    forever -- ``None`` until that read succeeds. Unlike every other
    field on this record, they are *never* derived from the serial
    announcement and are never cleared or overwritten by
    :meth:`Store.apply_probe_result`/:meth:`Store.apply_remote_probe`,
    even across a reflash that blanks ``device_name`` again -- a chip's
    physical identity doesn't change when its firmware does. Set only by
    :meth:`Store.set_chip_identity`.
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
    host: str | None
    remote_lock_kind: str | None
    remote_lock_display: str | None
    chip_identity_name: str | None
    chip_identity_serial: int | None
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
        host=row["host"],
        remote_lock_kind=row["remote_lock_kind"],
        remote_lock_display=row["remote_lock_display"],
        chip_identity_name=row["chip_identity_name"],
        chip_identity_serial=row["chip_identity_serial"],
        flash_count=row["flash_count"],
        first_seen=row["first_seen"],
        last_seen=row["last_seen"],
        last_probe=row["last_probe"],
    )


@dataclass(frozen=True)
class PeerRecord:
    """One ``peer`` row, per sprint.md's ERD §4 -- a registry this host has
    discovered (via mDNS or ``--peer``), tracked for
    ``registry.peering``'s event-bus subscription and reconnect logic.

    ``endpoint`` is ``host:port`` for the peer's ``remote_api`` TCP
    listener. ``reachable`` is ``False`` for a peer whose live link has
    dropped (Decision 5: marked unreachable, never deleted -- a
    reconnect's next :meth:`Store.record_peer_seen` naturally supersedes
    the stale flag).
    """

    host: str
    endpoint: str
    last_seen: float
    reachable: bool


def _row_to_peer_record(row: sqlite3.Row) -> PeerRecord:
    return PeerRecord(
        host=row["host"],
        endpoint=row["endpoint"],
        last_seen=row["last_seen"],
        reachable=bool(row["reachable"]),
    )


@dataclass(frozen=True)
class Entry:
    """One ``name_registry`` row -- a robot name's ``(channel, group)``,
    per sprint.md's ERD and this ticket's Approach.

    ``source`` is :data:`SOURCE_DERIVED` (computed from ``name`` by
    :mod:`mbtools.relay.naming` and persisted on first ask -- see
    :meth:`Store.resolve`) or :data:`SOURCE_REGISTRY` (explicitly set via
    :meth:`Store.set`).

    ``conflict``/``channel_conflict`` are populated only by
    :meth:`Store.listing` -- every other method that returns an
    :class:`Entry` leaves them at their default, empty tuple. They name the
    *other* robots sharing this entry's exact link (error severity) or its
    channel in a different group (warning severity); see
    :meth:`Store.conflicts`/:meth:`Store.channel_conflicts` for the
    fleet-wide view these are drawn from.
    """

    name: str
    channel: int
    group: int
    source: str
    updated: float
    conflict: tuple[str, ...] = ()
    channel_conflict: tuple[str, ...] = ()


def _row_to_entry(row: sqlite3.Row) -> Entry:
    return Entry(
        name=row["name"],
        channel=row["channel"],
        group=row["radio_group"],
        source=row["source"],
        updated=row["updated"],
    )


class AmbiguousNameError(Exception):
    """Raised by :meth:`Store.find` when a bare ``device_name`` token (no
    ``@host`` suffix) matches more than one distinct ``host`` value --
    including the local ``NULL`` host as one candidate -- per the
    stakeholder's explicit requirement that a name colliding across hosts
    must be disambiguated with ``name@host`` (sprint.md SUC-002's
    postcondition). A ``uid``/``short_uid`` match is never ambiguous by
    definition (``uid`` is globally unique) and never raises this.
    """

    def __init__(self, token: str, hosts: list[str | None]) -> None:
        self.token = token
        self.hosts = hosts
        display_hosts = ", ".join("local" if h is None else h for h in hosts)
        super().__init__(
            f"device name {token!r} is ambiguous across hosts: {display_hosts} "
            "-- use 'name@host' to disambiguate"
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

    **Thread safety.** ``Store`` is shared across the daemon's own poll
    thread, every ``RegistryAPIServer`` per-connection handler thread,
    and (in some tests) the test's own main thread -- all against one
    ``sqlite3.Connection`` opened with ``check_same_thread=False``. An
    earlier revision of this docstring argued that was sufficient on its
    own ("sqlite3's default (serialized) build-time threading mode
    already protects the underlying connection handle"); in practice,
    concurrent full-suite runs intermittently hit
    ``sqlite3.InterfaceError``/``IndexError`` inside
    :func:`_row_to_record` -- two threads interleaving a write-then-read
    sequence (e.g. one thread's ``UPDATE ... commit()`` landing between
    another thread's own ``execute`` and ``fetchone()``) can hand a
    half-updated or cursor-invalidated row back to :func:`_row_to_record`.
    ``self._lock`` (a ``threading.RLock``, reentrant so one public method
    can call another, e.g. :meth:`upsert_attached` calling :meth:`get`,
    without deadlocking itself) now serializes every public method's use
    of ``self._conn`` -- every method below acquires it for its whole
    body, not just the final ``commit()``, so a reader can never observe
    another thread's write mid-flight. This is *this class's own*
    internal guard; it does not replace ``api.py``'s separate shared
    ``threading.RLock`` (ticket 009), which still serializes
    check-then-act sequences that span *multiple* calls into
    ``store``/``locks`` together (e.g. "check lock status, then write") --
    a concern this class's own per-call lock cannot address by itself.
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
        # Reentrant so a public method can call another public method on
        # `self` (e.g. upsert_attached -> get) without deadlocking on its
        # own lock -- see the class docstring's "Thread safety" note.
        self._lock = threading.RLock()
        # check_same_thread=False: ticket 008's api module calls into this
        # same Store instance from its connection-handler threads, which
        # are never the thread that constructed it. Safe now that every
        # public method below serializes its own use of self._conn
        # through self._lock (see the class docstring) -- sqlite3's own
        # serialized threading mode protects the connection handle from
        # corruption, but does not by itself make a multi-statement
        # execute+commit+fetch sequence atomic across threads, which is
        # what this class's own lock adds.
        self._conn = sqlite3.connect(str(self.db_path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute(_SCHEMA)
        self._migrate_schema()
        self._conn.commit()

    def _migrate_schema(self) -> None:
        """Add the sprint-003 ``device`` columns, the sprint-007
        chip-identity columns, the ``peer`` table, and (sprint 004,
        ticket 001) the ``name_registry`` table, in place -- never a
        destructive rebuild, per this module's "never delete, always
        update in place" precedent (module docstring; sprint.md's
        Migration Concerns).

        Runs unconditionally, every construction, right after ``_SCHEMA``'s
        ``CREATE TABLE IF NOT EXISTS device`` -- which only ever creates
        the *original* sprint-1/2 shape, since ``_SCHEMA`` itself was left
        unchanged (see ``_NEW_DEVICE_COLUMNS``'s docstring). That means a
        brand-new database and an already-deployed sprint-1/2 database
        both arrive here with a ``device`` table missing the three new
        columns, and both are brought up to the full sprint-003 schema by
        this one code path -- not a separate "fresh vs. upgrade" branch.
        Each column is added only if :func:`PRAGMA table_info` doesn't
        already report it, since SQLite's ``ADD COLUMN`` has no
        ``IF NOT EXISTS`` on every SQLite version this project's Python
        targets bundle. ``peer`` uses ``CREATE TABLE IF NOT EXISTS``
        directly -- already idempotent, no per-column migration needed.
        """
        existing_columns = {
            row["name"] for row in self._conn.execute("PRAGMA table_info(device)")
        }
        for column, decl in _NEW_DEVICE_COLUMNS + _CHIP_IDENTITY_COLUMNS:
            if column not in existing_columns:
                self._conn.execute(f"ALTER TABLE device ADD COLUMN {column} {decl}")
        self._conn.execute(_PEER_SCHEMA)
        # Sprint 004, ticket 001: another brand-new table, same idempotent
        # CREATE TABLE IF NOT EXISTS path as _PEER_SCHEMA above -- no
        # per-column migration needed.
        self._conn.execute(_NAME_REGISTRY_SCHEMA)

    def close(self) -> None:
        """Close the underlying SQLite connection."""
        with self._lock:
            self._conn.close()

    # -- writes ---------------------------------------------------------

    def upsert_attached(self, uid: str, port: str, vid_pid: str) -> DeviceRecord:
        """Record ``uid`` as currently attached on ``port``, as a locally
        owned device (``host`` stays ``NULL``).

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
        return self._upsert_device(uid, None, port, vid_pid)

    def upsert_remote_attached(
        self, uid: str, host: str, port: str, vid_pid: str
    ) -> DeviceRecord:
        """Remote counterpart to :meth:`upsert_attached` -- records ``uid``
        as attached on peer ``host``, per a snapshot or attach event
        ``registry.peering`` (ticket 005) applies into this store.

        Same identity fields and the same reattach/probe-eligibility
        semantics as :meth:`upsert_attached` (new record or a
        ``disconnected`` -> reattach transition resets ``state``/
        ``last_probe``; an already-attached remote row's announcement
        fields are left untouched) -- the only difference is that
        ``host`` is set to the owning peer's hostname instead of staying
        ``NULL``.
        """
        return self._upsert_device(uid, host, port, vid_pid)

    def _upsert_device(
        self, uid: str, host: str | None, port: str, vid_pid: str
    ) -> DeviceRecord:
        with self._lock:
            now = self._now()
            existing = self.get(uid)
            if existing is None:
                self._conn.execute(
                    """
                    INSERT INTO device (
                        uid, short_uid, port, vid_pid, state, host,
                        flash_count, first_seen, last_seen, last_probe
                    ) VALUES (?, ?, ?, ?, ?, ?, 0, ?, ?, 0.0)
                    """,
                    (
                        uid,
                        short_uid(uid),
                        port,
                        vid_pid,
                        STATE_ATTACHED_UNPROBED,
                        host,
                        now,
                        now,
                    ),
                )
            elif (
                host is not None
                and existing.host is None
                and existing.state != STATE_DISCONNECTED
            ):
                # Local ownership wins over a stale/conflicting remote claim
                # (sprint 005 ticket 011): a remote snapshot or live event
                # (``host`` is the sending peer's hostname, never ``None``)
                # must never steal a uid this host currently has attached
                # and connected itself -- e.g. a peer that tested the same
                # board in the past and still holds it as a stale local row
                # must not overwrite the host that has it plugged in right
                # now. A *disconnected* local row is a different story --
                # that's a genuine hand-over, and falls through to the
                # branch below instead. Logged and dropped, record
                # returned unchanged.
                logger.warning(
                    "store: rejecting remote claim of uid %s by host %s -- "
                    "locally owned and connected (state=%s)",
                    uid,
                    host,
                    existing.state,
                )
                return existing
            elif existing.state == STATE_DISCONNECTED:
                self._conn.execute(
                    """
                    UPDATE device
                    SET port = ?, vid_pid = ?, state = ?, host = ?, last_seen = ?,
                        last_probe = 0.0
                    WHERE uid = ?
                    """,
                    (port, vid_pid, STATE_ATTACHED_UNPROBED, host, now, uid),
                )
            else:
                self._conn.execute(
                    "UPDATE device SET port = ?, vid_pid = ?, host = ?, last_seen = ? "
                    "WHERE uid = ?",
                    (port, vid_pid, host, now, uid),
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
        sets ``state=STATE_ATTACHED_NO_ANNOUNCE`` (sprint 007, ticket
        001 -- "we asked and got nothing back", not an assertion that the
        board is blank) and an ``error_note``, but leaves any
        previously-known announcement fields untouched -- mirrors
        ``mbdeploy``'s "preserve existing announcement fields unchanged"
        rule. ``last_probe``/``last_seen`` are still updated -- a probe
        was attempted, so this uid is no longer "never probed". Use
        :meth:`apply_known_blank` instead of passing ``None`` here for a
        call site that can actually assert the board is blank (e.g. a
        flash-triggered re-probe that still gets nothing after a mass
        erase).

        Raises :class:`KeyError` if ``uid`` has no existing record -- the
        pipeline always calls :meth:`upsert_attached` before probing.
        """
        with self._lock:
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
                    (STATE_ATTACHED_NO_ANNOUNCE, _ERROR_NOTE_NO_ANNOUNCEMENT, now, now, uid),
                )
            self._conn.commit()
            record = self.get(uid)
            assert record is not None
            return record

    def apply_known_blank(self, uid: str) -> DeviceRecord:
        """Sibling to :meth:`apply_probe_result` (mirroring the existing
        :meth:`apply_remote_probe` sibling-method precedent) for a call
        site that can actually *assert* the board is blank, rather than
        merely "didn't hear anything" -- principally ticket 003's
        flash-triggered re-probe that still gets nothing after a mass
        erase.

        Sets ``state=STATE_CONNECTED_NO_FIRMWARE`` and an ``error_note``
        distinct from :meth:`apply_probe_result`'s didn't-announce note,
        leaving any previously-known announcement fields untouched --
        same "preserve existing announcement fields" rule as
        :meth:`apply_probe_result`. ``last_probe``/``last_seen`` are
        updated -- a probe was attempted.

        Raises :class:`KeyError` if ``uid`` has no existing record.
        """
        with self._lock:
            if self.get(uid) is None:
                raise KeyError(f"store.apply_known_blank: no record for uid {uid!r}")
            now = self._now()
            self._conn.execute(
                """
                UPDATE device
                SET state = ?, error_note = ?, last_probe = ?, last_seen = ?
                WHERE uid = ?
                """,
                (STATE_CONNECTED_NO_FIRMWARE, _ERROR_NOTE_KNOWN_BLANK, now, now, uid),
            )
            self._conn.commit()
            record = self.get(uid)
            assert record is not None
            return record

    def set_chip_identity(self, uid: str, name: str, serial: int) -> DeviceRecord:
        """Persist ``uid``'s chip-identity cache (name + decimal serial)
        -- a fixed physical property of the target chip read once over
        SWD (ticket 003's ``identity.read_chip_identity``), independent
        of the announcement-derived ``device_name``.

        Write-once per uid: a uid that already has a cached
        ``chip_identity_name`` is left untouched and the existing record
        is returned unchanged -- once read, the value can never
        legitimately change for a given physical chip (sprint.md SUC-001's
        "cached forever ... never re-read"), so a second call (e.g. a
        retried read racing an already-successful one) must not clobber
        it. The primary "don't call SWD twice" gate lives in
        ``registry.daemon`` (it checks the cache before ever calling the
        SWD read); this is the store's own second line of defense.

        Raises :class:`KeyError` if ``uid`` has no existing record.
        """
        with self._lock:
            existing = self.get(uid)
            if existing is None:
                raise KeyError(f"store.set_chip_identity: no record for uid {uid!r}")
            if existing.chip_identity_name is not None:
                return existing
            self._conn.execute(
                "UPDATE device SET chip_identity_name = ?, chip_identity_serial = ? "
                "WHERE uid = ?",
                (name, serial, uid),
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
        with self._lock:
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
        with self._lock:
            if self.get(uid) is None:
                raise KeyError(f"store.increment_flash_count: no record for uid {uid!r}")
            self._conn.execute(
                "UPDATE device SET flash_count = flash_count + 1 WHERE uid = ?", (uid,)
            )
            self._conn.commit()
            record = self.get(uid)
            assert record is not None
            return record

    def mark_remote_detached(self, uid: str) -> DeviceRecord:
        """Remote counterpart to :meth:`mark_disconnected` --
        ``registry.peering`` (ticket 005) calls this when a peer's detach
        event arrives for one of its remote-owned rows.

        Identical effect to :meth:`mark_disconnected` (``state`` ->
        ``disconnected``, ``last_seen`` updated, the record never
        deleted); kept as a separately named method so a call site's
        intent (a local ``usbwatch`` detach vs. a remote peer event) stays
        explicit even though the store-side write is the same.

        Raises :class:`KeyError` if ``uid`` has no existing record.
        """
        return self.mark_disconnected(uid)

    def apply_remote_probe(self, uid: str, result: ProbeResult | None) -> DeviceRecord:
        """Remote counterpart to :meth:`apply_probe_result` -- applies a
        peer-reported probe outcome (from a snapshot or identity-change
        event) to one of this store's remote-owned rows.

        Identical semantics to :meth:`apply_probe_result`: a successful
        ``result`` updates the announcement fields and sets
        ``state="connected"``; ``None`` sets
        ``state="connected_no_firmware"`` and leaves previously-known
        announcement fields untouched.

        Raises :class:`KeyError` if ``uid`` has no existing record.
        """
        return self.apply_probe_result(uid, result)

    def apply_remote_lock_state(
        self, uid: str, kind: str | None, display: str | None
    ) -> DeviceRecord:
        """Set (or clear, passing ``None``/``None``) the display-only
        ``remote_lock_kind``/``remote_lock_display`` cache columns on
        ``uid``'s row.

        Per Decision 3, this is a pure cache write driven by
        ``registry.peering``'s replicated lock-acquire/lock-release
        *display* events -- it never calls into ``registry.locks``
        (``LockManager`` isn't even constructed in this module) and never
        touches the real lock state, which only the owning host's
        ``remote_api``/``LockManager`` is authoritative over.

        Raises :class:`KeyError` if ``uid`` has no existing record.
        """
        with self._lock:
            if self.get(uid) is None:
                raise KeyError(f"store.apply_remote_lock_state: no record for uid {uid!r}")
            self._conn.execute(
                "UPDATE device SET remote_lock_kind = ?, remote_lock_display = ? WHERE uid = ?",
                (kind, display, uid),
            )
            self._conn.commit()
            record = self.get(uid)
            assert record is not None
            return record

    def record_peer_seen(self, host: str, endpoint: str) -> PeerRecord:
        """Record ``host`` as discovered/reconnected at ``endpoint``,
        creating its ``peer`` row if new, and mark it reachable.

        Called by ``registry.peering`` (ticket 004/005) on mDNS discovery,
        an explicit ``--peer``, or a successful snapshot/reconnect --
        every case where this host has just proven it can reach ``host``.
        """
        with self._lock:
            now = self._now()
            existing = self.get_peer(host)
            if existing is None:
                self._conn.execute(
                    "INSERT INTO peer (host, endpoint, last_seen, reachable) "
                    "VALUES (?, ?, ?, 1)",
                    (host, endpoint, now),
                )
            else:
                self._conn.execute(
                    "UPDATE peer SET endpoint = ?, last_seen = ?, reachable = 1 WHERE host = ?",
                    (endpoint, now, host),
                )
            self._conn.commit()
            record = self.get_peer(host)
            assert record is not None
            return record

    def mark_peer_unreachable(self, host: str) -> PeerRecord:
        """Set ``host``'s ``peer.reachable`` to false -- the losing side of
        a dropped ZMQ link calls this (Decision 5), never deleting the
        peer row or touching any of its devices' ``state`` (only
        ``registry.render``'s reachability-derived display changes; a
        real disconnect the owning host itself observed is a separate
        concern from losing contact with that host).

        Raises :class:`KeyError` if ``host`` has no existing ``peer`` row.
        """
        return self._set_peer_reachable(host, False)

    def mark_peer_reachable(self, host: str) -> PeerRecord:
        """Set ``host``'s ``peer.reachable`` to true, without touching
        ``endpoint``/``last_seen`` -- use :meth:`record_peer_seen` when a
        fresh endpoint/last-seen timestamp is also available.

        Raises :class:`KeyError` if ``host`` has no existing ``peer`` row.
        """
        return self._set_peer_reachable(host, True)

    def _set_peer_reachable(self, host: str, reachable: bool) -> PeerRecord:
        with self._lock:
            if self.get_peer(host) is None:
                raise KeyError(f"store._set_peer_reachable: no record for host {host!r}")
            self._conn.execute(
                "UPDATE peer SET reachable = ? WHERE host = ?",
                (1 if reachable else 0, host),
            )
            self._conn.commit()
            record = self.get_peer(host)
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
        with self._lock:
            record = self.get(uid)
            if record is None:
                raise KeyError(f"store.needs_probe: no record for uid {uid!r}")
            return record.last_probe == 0.0

    def get(self, uid: str) -> DeviceRecord | None:
        """Exact lookup by ``uid``. ``None`` if no such device."""
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM device WHERE uid = ?", (uid,)
            ).fetchone()
            return _row_to_record(row) if row is not None else None

    def find(self, token: str) -> DeviceRecord | None:
        """Resolve a user-supplied ``token`` to a device record.

        An ``@host`` suffix (e.g. ``"zavaz@loki"``) is checked first: when
        present, ``token`` is split on the last ``@`` and resolved to the
        device whose ``device_name`` matches case-insensitively *and*
        whose ``host`` matches case-insensitively (a bare hostname, not
        ``host:port``) -- no ``uid``/``short_uid`` check is attempted for
        an ``@``-suffixed token, since neither ever contains ``@``.

        Without a suffix, precedence is the same family as ``mbdeploy``'s
        ``resolve_target``, minus its numeric-enum and port-path cases,
        which don't apply at the store layer (no ``enum`` field here; a
        port path is resolved by the caller against a live scan, per
        ``resolve_target``'s own docstring, not looked up in the store):

        1. Exact match against ``uid``.
        2. Exact match against ``short_uid``. Note ``short_uid`` is *not*
           guaranteed unique across every board on a bench (two boards can
           share the interface-chip tail that ``short_uid`` derives from
           -- see ``identity.short_uid``'s own docstring); an ambiguous
           match returns whichever row SQLite happens to return first,
           same "good enough for a bench" spirit as the rest of this
           sprint's scale assumptions.
        3. Case-insensitive match against ``device_name``. If more than
           one distinct ``host`` value has a device with this name
           (including the local ``NULL`` host as one candidate), raises
           :class:`AmbiguousNameError` instead of picking one -- per the
           stakeholder's explicit ``name@host`` disambiguation
           requirement (SUC-002). A ``device_name`` collision *within*
           the same host is not this rule's concern and, like the
           ``short_uid`` case, returns whichever row SQLite returns
           first.

        ``None`` if nothing matches.
        """
        with self._lock:
            if "@" in token:
                name, _, host = token.rpartition("@")
                row = self._conn.execute(
                    "SELECT * FROM device WHERE device_name = ? COLLATE NOCASE "
                    "AND host = ? COLLATE NOCASE",
                    (name, host),
                ).fetchone()
                return _row_to_record(row) if row is not None else None

            row = self._conn.execute(
                "SELECT * FROM device WHERE uid = ?", (token,)
            ).fetchone()
            if row is None:
                row = self._conn.execute(
                    "SELECT * FROM device WHERE short_uid = ?", (token,)
                ).fetchone()
            if row is not None:
                return _row_to_record(row)

            matches = self._conn.execute(
                "SELECT * FROM device WHERE device_name = ? COLLATE NOCASE", (token,)
            ).fetchall()
            if not matches:
                return None
            hosts = [m["host"] for m in matches]
            distinct_hosts = {h.lower() if h is not None else None for h in hosts}
            if len(distinct_hosts) > 1:
                # Dedupe for the error message while preserving each
                # distinct host's original casing.
                seen: set[str | None] = set()
                unique_hosts: list[str | None] = []
                for h in hosts:
                    key = h.lower() if h is not None else None
                    if key not in seen:
                        seen.add(key)
                        unique_hosts.append(h)
                raise AmbiguousNameError(token, unique_hosts)
            return _row_to_record(matches[0])

    def list_devices(self) -> list[DeviceRecord]:
        """Every record, including ``disconnected`` ones (UC-004's "gone,
        not silently dropped" requirement).

        Iteration is not required to be performant beyond "all records fit
        in memory," per spec §3.4.
        """
        with self._lock:
            rows = self._conn.execute("SELECT * FROM device").fetchall()
            return [_row_to_record(row) for row in rows]

    def snapshot_local_devices(self) -> list[DeviceRecord]:
        """Every locally-owned, currently-connected row (``host IS NULL
        AND state != 'disconnected'``), shaped for ``registry.peering``'s
        snapshot-exchange payload (ticket 005) -- what this host hands a
        newly-joining peer before it subscribes to the live event stream,
        and what a reconnecting peer's own resync uses too.

        Never includes a row this host itself learned about from some
        *other* peer -- each registry's snapshot is its own devices only,
        so a peer applying it always tags the result with the snapshot's
        source host, never re-propagating a third host's rows.

        Also never includes a disconnected local row (sprint 005 ticket
        011): a uid this host once had attached but no longer does is not
        an active claim of ownership, and must not be advertised as one --
        a peer applying an unfiltered snapshot would otherwise re-tag a
        board that has since moved elsewhere back to this host, exactly
        the stale-reassertion bug this ticket fixes. The row itself is
        never dropped from *this* store -- only excluded from what gets
        sent to peers.
        """
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM device WHERE host IS NULL AND state != ?",
                (STATE_DISCONNECTED,),
            ).fetchall()
            return [_row_to_record(row) for row in rows]

    def list_peers(self) -> list[PeerRecord]:
        """Every known peer, reachable or not."""
        with self._lock:
            rows = self._conn.execute("SELECT * FROM peer").fetchall()
            return [_row_to_peer_record(row) for row in rows]

    def get_peer(self, host: str) -> PeerRecord | None:
        """Exact lookup by ``host``. ``None`` if this host is not a known
        peer."""
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM peer WHERE host = ?", (host,)
            ).fetchone()
            return _row_to_peer_record(row) if row is not None else None

    # -- name registry (sprint 004, ticket 001) ----------------------------
    #
    # Conceptually ports ``microbit-radio-relay/server/src/mbrelay/
    # registry.py``'s ``NameRegistry`` query/conflict-detection semantics
    # onto this table, with the old ``pins`` (TOML) precedence tier dropped
    # (sprint.md Decision 7 -- only SOURCE_DERIVED/SOURCE_REGISTRY exist
    # here). ``mbtools.relay.naming`` is the pure address-derivation module
    # both this class and (ticket 003's) ``relay.protocol`` depend on.
    #
    # Named ``get_name``/``resolve``/``set``/``clear`` rather than a literal
    # ``get`` -- this class already has a ``get(uid)`` for the *device*
    # table, and a method can't be keyed by one argument's runtime type in
    # Python; reusing ``get`` here would silently shadow the device lookup.
    # ``resolve``/``set``/``clear``/``name_for``/``conflicts``/
    # ``channel_conflicts``/``listing`` don't collide with anything already
    # on this class, so those keep the ticket's literal names.

    def resolve(self, name: str) -> Entry:
        """Where is this robot? Always answers for a well-formed name.

        Returns the existing row unchanged if ``name`` is already on
        record (either source). Otherwise derives ``(channel, group)`` via
        :func:`mbtools.relay.naming.name_to_radio`, persists it with
        ``source=SOURCE_DERIVED``, and returns the new row.

        The insert uses ``INSERT OR IGNORE``: two calls for the same
        unseen name always derive the identical pair (a pure function of
        ``name``), so a race that loses the insert simply reads back the
        row the winner just wrote -- no crash, no divergent value.

        Raises ``ValueError`` if ``name`` is not a well-formed micro:bit
        name (:func:`mbtools.relay.naming.validate`).
        """
        with self._lock:
            name = naming.validate(name)
            existing = self._get_name_row(name)
            if existing is not None:
                return existing
            channel, group = naming.name_to_radio(name)
            now = self._now()
            self._conn.execute(
                "INSERT OR IGNORE INTO name_registry "
                "(name, channel, radio_group, source, updated) VALUES (?, ?, ?, ?, ?)",
                (name, channel, group, SOURCE_DERIVED, now),
            )
            self._conn.commit()
            record = self._get_name_row(name)
            assert record is not None  # just written (or raced and lost, same row)
            return record

    def get_name(self, name: str) -> Entry | None:
        """Like :meth:`resolve`, but does not create. ``None`` if ``name``
        has no row yet.

        Raises ``ValueError`` if ``name`` is not a well-formed micro:bit
        name.
        """
        with self._lock:
            name = naming.validate(name)
            return self._get_name_row(name)

    def _get_name_row(self, name: str) -> Entry | None:
        """Exact lookup by already-validated ``name``. Callers hold
        ``self._lock``."""
        row = self._conn.execute(
            "SELECT * FROM name_registry WHERE name = ?", (name,)
        ).fetchone()
        return _row_to_entry(row) if row is not None else None

    def set(self, name: str, channel: int, group: int) -> Entry:
        """Explicitly assign ``name`` to ``(channel, group)``, persisted
        with ``source=SOURCE_REGISTRY`` -- overwrites any existing row
        (derived or previously registered).

        Raises ``ValueError`` if ``name`` is not a well-formed micro:bit
        name.
        """
        with self._lock:
            name = naming.validate(name)
            now = self._now()
            if self._get_name_row(name) is None:
                self._conn.execute(
                    "INSERT INTO name_registry "
                    "(name, channel, radio_group, source, updated) VALUES (?, ?, ?, ?, ?)",
                    (name, channel, group, SOURCE_REGISTRY, now),
                )
            else:
                self._conn.execute(
                    "UPDATE name_registry SET channel = ?, radio_group = ?, "
                    "source = ?, updated = ? WHERE name = ?",
                    (channel, group, SOURCE_REGISTRY, now, name),
                )
            self._conn.commit()
            record = self._get_name_row(name)
            assert record is not None
            return record

    def clear(self, name: str) -> None:
        """Drop ``name``'s row, if any. A later :meth:`resolve` re-derives
        it from scratch (``source=SOURCE_DERIVED`` again).

        Raises ``ValueError`` if ``name`` is not a well-formed micro:bit
        name. Clearing a name with no row is not an error -- a plain
        ``DELETE`` on an absent primary key is already a no-op.
        """
        with self._lock:
            name = naming.validate(name)
            self._conn.execute("DELETE FROM name_registry WHERE name = ?", (name,))
            self._conn.commit()

    def name_for(self, channel: int, group: int) -> str | None:
        """Which robot is on this link? The inverse of :meth:`resolve`.

        An explicit (``source=SOURCE_REGISTRY``) row on this exact pair
        wins. Failing that, the derived bijection answers via
        :func:`mbtools.relay.naming.radio_to_name` -- a name maps to its
        own pair for free -- but NOT when that name's own row has since
        moved it elsewhere, since the address it vacated no longer belongs
        to it. ``None`` if nothing answers.
        """
        with self._lock:
            row = self._conn.execute(
                "SELECT name FROM name_registry WHERE channel = ? AND radio_group = ? "
                "AND source = ?",
                (channel, group, SOURCE_REGISTRY),
            ).fetchone()
            if row is not None:
                return row["name"]
            try:
                derived_name = naming.radio_to_name(channel, group)
            except ValueError:
                return None
            moved = self._get_name_row(derived_name)
            if moved is not None and (moved.channel, moved.group) != (channel, group):
                return None
            return derived_name

    def conflicts(self) -> dict[tuple[int, int], list[str]]:
        """Pairs held by more than one name -- error severity (each robot
        receives the other's packets and acts on the other's commands).
        Reported, never enforced -- a survey is allowed to make a mess
        halfway through."""
        with self._lock:
            return self._conflicts_of(self._all_name_entries())

    def channel_conflicts(self) -> dict[int, list[str]]:
        """Channels holding names in more than one group -- warning
        severity (the group byte filters each other's packets, but both
        still transmit on one frequency and collide). A channel whose
        names all share one group is a :meth:`conflicts` error instead,
        never reported twice."""
        with self._lock:
            return self._channel_conflicts_of(self._all_name_entries())

    def listing(self) -> list[Entry]:
        """Every row, each annotated with its ``conflict``/
        ``channel_conflict`` names (see :class:`Entry`).

        Per-entry, not a filter of :meth:`conflicts`/:meth:`channel_conflicts`
        (those answer a different, fleet-wide question -- "is this channel
        used by more than one group at all?" -- which is not the same set as
        "which *other* names sit on *this* entry's channel in a different
        group"). Ported from ``NameRegistry._annotate``: for each entry,
        every other entry sharing its channel is either on the exact same
        link (``conflict``) or the same channel in a different group
        (``channel_conflict``).
        """
        with self._lock:
            entries = self._all_name_entries()
            annotated = []
            for entry in entries:
                others = [
                    e for e in entries
                    if e.channel == entry.channel and e.name != entry.name
                ]
                same_link = tuple(e.name for e in others if e.group == entry.group)
                same_channel = tuple(e.name for e in others if e.group != entry.group)
                annotated.append(
                    replace(entry, conflict=same_link, channel_conflict=same_channel)
                )
            return annotated

    def _all_name_entries(self) -> list[Entry]:
        """Every ``name_registry`` row, unannotated. Callers hold
        ``self._lock``."""
        rows = self._conn.execute("SELECT * FROM name_registry ORDER BY name").fetchall()
        return [_row_to_entry(row) for row in rows]

    @staticmethod
    def _conflicts_of(entries: list[Entry]) -> dict[tuple[int, int], list[str]]:
        seen: dict[tuple[int, int], list[str]] = {}
        for entry in entries:
            seen.setdefault((entry.channel, entry.group), []).append(entry.name)
        return {pair: names for pair, names in seen.items() if len(names) > 1}

    @staticmethod
    def _channel_conflicts_of(entries: list[Entry]) -> dict[int, list[str]]:
        names: dict[int, list[str]] = {}
        groups: dict[int, set[int]] = {}
        for entry in entries:
            names.setdefault(entry.channel, []).append(entry.name)
            groups.setdefault(entry.channel, set()).add(entry.group)
        return {ch: names[ch] for ch in names if len(groups[ch]) > 1}
