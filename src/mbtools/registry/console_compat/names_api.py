"""mbtools.registry.console_compat.names_api — the ``GET/PUT/DELETE
/names/<name>`` HTTP listener (sprint 004, ticket 007, architecture
Decision 1).

Per sprint.md's Step 3 module table ("Serve the ``/names/<name>`` HTTP
contract robot-console already expects") and SUC-003, this is what lets
robot-console (unmodified -- `packages/host/src/mbrelayRegistry.ts`)
resolve a robot's ``(channel, group)`` against ``mbregistry`` instead of
the retiring ``mbrelay`` daemon's own HTTP port. robot-console only ever
issues a single ``GET`` per resolution -- ``mbrelayRegistry.ts``'s own
module docstring documents this as an *enforced policy*, not an
accident, precisely because ``GET`` here **mutates** the registry on a
miss (see below) -- but the issue behind this ticket
(``mbrelay-relay-protocol-client-over-mbregistry.md``) asks for full
``PUT``/``DELETE`` too, to preserve parity with legacy ``mbrelay``'s own
``httpapi.py`` contract for admin/tooling use (this project's own
``mbrelay names set``/``names clear`` do **not** go through this module
-- see "Not the CLI's own write path" below).

**The write-on-read trap, from the server side** (mirrors
``mbrelayRegistry.ts``'s own doc comment, the client-side half of this
same contract): ``GET /names/<name>`` always answers ``200``, deriving
and persisting a ``source="derived"`` row the first time any client asks
about a well-formed name nobody has mentioned before
(:meth:`~mbtools.registry.store.Store.resolve`, ticket 001) -- there is
no way to "peek" over HTTP without writing, by design (``Store`` also
has a non-creating :meth:`~mbtools.registry.store.Store.get_name`, which
this module uses only to detect *whether* a ``GET`` is about to create a
row, never to serve the response itself).

**Response shape, exact** (cross-checked field-by-field against
``mbrelayRegistry.ts``'s own ``parseResolvedAddress``, not assumed from
the brief): ``{"channel": int, "group": int, "source": str}`` for every
one of ``GET``/``PUT``/``DELETE`` -- ``mbrelayRegistry.ts`` only ever
recognizes ``source`` values ``"config"``/``"registry"``/``"derived"``;
this project never produces ``"config"`` (architecture Decision 7 drops
that tier), so only ``"derived"``/``"registry"`` are ever sent, both of
which robot-console's client already recognizes as real (non-fallback)
answers.

**Every write replicates, exactly like ticket 005's local-socket ops**
(``registry._api_base.BaseAPIServer``'s ``_op_names_set``/
``_op_names_clear``, the established "the component that owns the write
fires the callback, once the write has committed, outside the lock"
shape): a ``PUT`` (:meth:`~mbtools.registry.store.Store.set`) always
publishes via ``name_set_callback``; a ``DELETE``
(:meth:`~mbtools.registry.store.Store.clear`) always publishes via
``name_clear_callback``; a ``GET`` publishes via ``name_set_callback``
**only** when it actually derived-and-persisted a new row this call
(sprint.md SUC-003's own postcondition: "the possible derive-and-persist
side effect... also triggers replication" -- a repeat ``GET`` for an
already-known name has no side effect at all, replication included, per
this ticket's own "no unbounded growth or side effect beyond the first
derive" acceptance criterion). Detecting "did this call just create the
row" is a plain :meth:`~mbtools.registry.store.Store.get_name` check
before calling :meth:`~mbtools.registry.store.Store.resolve` -- not
perfectly atomic across two threads racing the very same unseen name,
but harmless if it isn't (sprint.md: "harmless even if two hosts derive
it independently and concurrently, since derivation is a pure
deterministic function of the name").

**``DELETE`` re-derives and returns the fresh value**, matching legacy
``mbrelay``'s own ``NameRegistry.clear() -> self.resolve(name)``
(``microbit-radio-relay/server/src/mbrelay/registry.py``) exactly: this
ticket's own Approach section states one uniform response shape for
"all three" verbs, and a ``DELETE`` that returns nothing to shape would
have no ``channel``/``group``/``source`` to send. So a ``DELETE``
publishes *two* events -- a clear for the row it just dropped, then a
set for the row :meth:`~mbtools.registry.store.Store.resolve`
immediately re-derives-and-persists in its place (always a fresh create,
since the row was just deleted) -- both fired outside the lock, same as
every other write here. The acceptance criterion "a subsequent GET
re-derives" holds either way (that GET is idempotent regardless of
whether the DELETE itself already re-derived), so this is a design
choice for response-shape completeness, not something the tests
distinguish from the alternative of a bare "cleared" reply.

**Malformed name or out-of-range channel/group on PUT is ``400``, never
a silent clamp** (this ticket's own acceptance criterion). ``Store``'s
own name-registry methods already raise ``ValueError`` for a malformed
*name* (:func:`mbtools.relay.naming.validate`, applied uniformly on
``GET``/``PUT``/``DELETE`` here, not just ``PUT``) -- but nothing in
``mbtools`` yet validates a ``(channel, group)`` *pair* against what the
relay firmware's ``!CG`` command actually accepts, so this module ports
that one check, conceptually (not verbatim -- the legacy function lived
on a module this project does not otherwise depend on) from legacy
``mbrelay``'s own ``registry.validate_pair``
(``microbit-radio-relay/server/src/mbrelay/registry.py``, itself
commented "what ``!CG <ch> <group>`` accepts (RadioRelay.cpp)"):
``CHANNEL_MIN, CHANNEL_MAX = 0, 83`` and ``GROUP_MIN, GROUP_MAX = 0,
255``. This is deliberately **not** ``mbtools.relay.naming``'s own
``CHANNEL_MIN``/``CHANNEL_MAX``/``GROUP_MIN``/``GROUP_MAX`` (``11-83``/
``15-255``) -- that module's constants describe only the *derived*
bijection's own narrower subrange (naming.py's own docstring: certain
low values are "never emitted" by derivation, precisely so a hand-set
registry override can be told apart on sight); "a registry override is
under no such constraint: it may use anything ``!CG`` accepts" (legacy
``registry.py``'s own module docstring), so this module's ``PUT``
validation intentionally uses the wider firmware-accepted range, not
``naming``'s narrower derived-only one.

**No auth, deliberately, matching ticket 006** (sprint.md's Migration
Concerns: "robot-console has no mechanism to send one, matching legacy
``mbrelay``'s own documented no-auth, internal-LAN-service posture for
this exact surface"): this module never checks ``--auth-token``, and
never will.

**Not the CLI's own write path**: ``mbrelay names set``/``names
clear``/``connect``'s own name lookup (ticket 005) go through new
``names_get``/``names_set``/``names_clear`` ops on the registry
daemon's *local Unix-socket* API (``registry._api_base.BaseAPIServer``)
instead of this HTTP module -- see ticket 005's own Implementation Notes
for why (a CLI process on the same host as the daemon has a faster, already
-authenticated-by-filesystem-permissions path available, and there is no
reason to round-trip through HTTP to talk to a service running in the
same process tree). This module exists purely for the one external
consumer (robot-console) that has no other way in, plus admin/tooling
parity with the legacy HTTP contract (this ticket's own Description).

**Shares ``relay_pool``'s own port, never redeclares it**
(ticket 006's own Implementation Notes: "ticket 007's ``names_api``
module should reuse ``DEFAULT_NAMES_API_PORT`` from this module rather
than re-declaring its own default, so the port this module advertises
and the port ``names_api`` actually binds never drift apart") --
:data:`DEFAULT_PORT` here is simply
:data:`~mbtools.registry.console_compat.relay_pool.DEFAULT_NAMES_API_PORT`
re-exported under a locally-scoped name, not a second literal ``7445``.
"""

from __future__ import annotations

import json
import logging
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Callable
from urllib.parse import unquote

from mbtools.registry.console_compat.relay_pool import DEFAULT_NAMES_API_PORT
from mbtools.registry.store import Entry, Store

__all__ = [
    "NamesAPI",
    "DEFAULT_PORT",
    "CHANNEL_MIN",
    "CHANNEL_MAX",
    "GROUP_MIN",
    "GROUP_MAX",
]

logger = logging.getLogger(__name__)

#: See the module docstring's "Shares relay_pool's own port" section.
DEFAULT_PORT = DEFAULT_NAMES_API_PORT

#: What ``!CG <channel> <group>`` accepts (RadioRelay.cpp) -- see the
#: module docstring's own "Malformed name or out-of-range..." section for
#: why this is deliberately wider than ``mbtools.relay.naming``'s own
#: derived-only range.
CHANNEL_MIN, CHANNEL_MAX = 0, 83
GROUP_MIN, GROUP_MAX = 0, 255

#: A PUT body is two small integers; this is already an absurd ceiling,
#: matching legacy ``httpapi.py``'s own ``MAX_BODY`` comment verbatim in
#: spirit.
MAX_BODY = 64 * 1024

_NAME_PREFIX = "/names/"


def _entry_payload(entry: Entry) -> "dict[str, Any]":
    """The exact wire shape ``mbrelayRegistry.ts`` parses -- see the
    module docstring's "Response shape, exact" section. Never the fuller
    ``_api_base._entry_dict`` shape (``name``/``updated``/``conflict``/
    ``channel_conflict``) -- robot-console's own parser rejects anything
    it doesn't recognize by falling back to a locally-derived address, so
    extra fields are harmless to it, but this module still sends exactly
    what the contract documents, per this ticket's own acceptance
    criterion ("cross-checked field-by-field... not assumed from the
    brief alone")."""
    return {"channel": entry.channel, "group": entry.group, "source": entry.source}


def _error(message: str, *, code: str = "bad_request") -> "dict[str, Any]":
    return {"error": {"code": code, "message": message}}


def _parse_pair(body: bytes) -> "tuple[int, int]":
    """``PUT`` body -> ``(channel, group)``, or ``ValueError`` with a
    message meant to be shown to whoever sent the malformed request --
    mirrors legacy ``httpapi.py``'s own ``_pair_from_body`` shape (JSON
    object, ``"channel"``/``"group"`` keys, whole numbers), plus this
    module's own range check (see the module docstring)."""
    try:
        data = json.loads(body or b"{}")
    except ValueError as exc:
        raise ValueError(f"body is not JSON: {exc}") from None
    if not isinstance(data, dict):
        raise ValueError('body must be an object, e.g. {"channel": 12, "group": 4}')
    if "channel" not in data or "group" not in data:
        raise ValueError('body needs "channel" and "group", e.g. {"channel": 12, "group": 4}')
    try:
        channel, group = int(data["channel"]), int(data["group"])
    except (TypeError, ValueError):
        raise ValueError("channel and group must be whole numbers") from None
    if not CHANNEL_MIN <= channel <= CHANNEL_MAX:
        raise ValueError(f"channel {channel} is outside {CHANNEL_MIN}-{CHANNEL_MAX}")
    if not GROUP_MIN <= group <= GROUP_MAX:
        raise ValueError(f"group {group} is outside {GROUP_MIN}-{GROUP_MAX}")
    return channel, group


class NamesAPI:
    """The ``GET/PUT/DELETE /names/<name>`` HTTP listener -- see the
    module docstring for the full contract.

    ``store`` is an injected reference, the same instance a caller
    (``registry.cli``'s ``mbregistry run`` assembly, or a test) already
    constructed for the rest of the daemon -- this class never constructs
    its own, mirroring every other network-facing module in this package.

    ``lock`` is the shared ``threading.RLock`` ``registry.cli.
    assemble_registry`` already threads through ``daemon``/``api``/
    ``remote_api``/``peering``/``relay_pool`` -- every ``store`` write
    this class makes is wrapped in it, the same reason
    ``_api_base.BaseAPIServer``'s ``_op_names_set``/``_op_names_clear``
    do; defaults to a private ``RLock`` of this instance's own when
    omitted (every test in this module that constructs a ``NamesAPI``
    standalone).

    ``host``/``port`` default to every interface and :data:`DEFAULT_PORT`
    (:data:`~mbtools.registry.console_compat.relay_pool.DEFAULT_NAMES_API_PORT`);
    a test passes ``port=0`` to let the OS choose an ephemeral port (see
    :attr:`bound_port`).

    ``name_set_callback``/``name_clear_callback`` are the replication
    hooks (see the module docstring's "Every write replicates" section)
    -- ``registry.cli``'s assembly wires
    ``PeerDiscovery.publish_name_set``/``publish_name_clear`` here, the
    same objects it already hands to ``RegistryAPIServer`` for ticket
    005's local-socket ops. Both default to ``None`` (no-op), so a
    standalone test that doesn't care about replication can omit them.
    """

    def __init__(
        self,
        *,
        store: Store,
        host: str = "0.0.0.0",
        port: int = DEFAULT_PORT,
        lock: "threading.RLock | None" = None,
        name_set_callback: "Callable[[Entry], None] | None" = None,
        name_clear_callback: "Callable[[str], None] | None" = None,
    ) -> None:
        self._store = store
        self._host = host
        self._port = port
        self._lock = lock if lock is not None else threading.RLock()
        self._name_set_callback = name_set_callback
        self._name_clear_callback = name_clear_callback

        self._httpd: "_Server | None" = None
        self._thread: "threading.Thread | None" = None
        self._started = False

    @property
    def bound_port(self) -> int:
        """The actual bound TCP port -- useful when constructed with
        ``port=0`` for a test to discover the real ephemeral port the OS
        chose."""
        assert self._httpd is not None
        return self._httpd.server_address[1]

    # -- lifecycle -----------------------------------------------------

    def start(self) -> None:
        """Bind the HTTP listener and start serving requests on a
        background thread. Returns immediately. Idempotent."""
        if self._started:
            return
        self._httpd = _Server((self._host, self._port), _NamesAPIHandler)
        self._httpd.names_api = self  # type: ignore[attr-defined]
        self._port = self._httpd.server_address[1]
        self._thread = threading.Thread(
            target=self._httpd.serve_forever, name="mbtools-names-api", daemon=True
        )
        self._thread.start()
        self._started = True

    def stop(self) -> None:
        """Stop serving new requests and join the serving thread before
        returning -- a caller that tears down ``store`` immediately after
        ``stop()`` returns must not race a not-yet-finished request
        handler (same reasoning as every other network-facing module in
        this package). An in-flight request is allowed to finish (``
        HTTPServer.shutdown()``'s own documented behavior). Idempotent.
        """
        if not self._started:
            return
        assert self._httpd is not None
        self._httpd.shutdown()
        self._httpd.server_close()
        if self._thread is not None:
            self._thread.join(timeout=2.0)
            self._thread = None
        self._httpd = None
        self._started = False

    def __enter__(self) -> "NamesAPI":
        self.start()
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.stop()

    # -- request handling (called by _NamesAPIHandler) ------------------

    def handle_get(self, name: str) -> "tuple[int, dict[str, Any]]":
        """``GET /names/<name>`` -- see the module docstring's
        "write-on-read trap" and "Every write replicates" sections."""
        try:
            with self._lock:
                existing = self._store.get_name(name)
                entry = self._store.resolve(name)
        except ValueError as exc:
            return 400, _error(str(exc))
        if existing is None and self._name_set_callback is not None:
            self._name_set_callback(entry)
        return 200, _entry_payload(entry)

    def handle_put(self, name: str, body: bytes) -> "tuple[int, dict[str, Any]]":
        """``PUT /names/<name>`` -- explicit assignment, always
        replicated (see the module docstring)."""
        try:
            channel, group = _parse_pair(body)
        except ValueError as exc:
            return 400, _error(str(exc))
        try:
            with self._lock:
                entry = self._store.set(name, channel, group)
        except ValueError as exc:
            return 400, _error(str(exc))
        if self._name_set_callback is not None:
            self._name_set_callback(entry)
        return 200, _entry_payload(entry)

    def handle_delete(self, name: str) -> "tuple[int, dict[str, Any]]":
        """``DELETE /names/<name>`` -- clears, then re-derives, per the
        module docstring's "DELETE re-derives and returns the fresh
        value" section. Both resulting writes are replicated."""
        try:
            with self._lock:
                self._store.clear(name)
        except ValueError as exc:
            return 400, _error(str(exc))
        if self._name_clear_callback is not None:
            self._name_clear_callback(name)
        with self._lock:
            entry = self._store.resolve(name)
        if self._name_set_callback is not None:
            self._name_set_callback(entry)
        return 200, _entry_payload(entry)


class _Server(ThreadingHTTPServer):
    """One thread per request, all daemon threads (never blocks process
    exit), address reuse on so a rapid test restart on the same
    ``port=0``-chosen port never sees ``Address already in use``."""

    daemon_threads = True
    allow_reuse_address = True

    names_api: NamesAPI  # set by NamesAPI.start() right after construction


class _NamesAPIHandler(BaseHTTPRequestHandler):
    """Translates raw HTTP into :class:`NamesAPI` calls. ``HTTP/1.0``
    (the ``BaseHTTPRequestHandler`` default) -- one request per
    connection, no keep-alive state machine to get wrong, matching legacy
    ``httpapi.py``'s own deliberate ``Connection: close`` posture (module
    docstring's own "No auth, deliberately" neighbor decision: this is
    also an internal, unauthenticated LAN service, so the same "keep the
    scope honest" posture applies)."""

    server_version = "mbtools-names-api/1"

    def do_GET(self) -> None:
        self._dispatch("GET")

    def do_PUT(self) -> None:
        self._dispatch("PUT")

    def do_DELETE(self) -> None:
        self._dispatch("DELETE")

    def _dispatch(self, method: str) -> None:
        names_api: NamesAPI = self.server.names_api  # type: ignore[attr-defined]
        name = self._extract_name()
        if name is None:
            self._respond(404, _error(f"no such route: {self.path}", code="not_found"))
            return
        try:
            if method == "GET":
                status, payload = names_api.handle_get(name)
            elif method == "PUT":
                body, error_response = self._read_body()
                if error_response is not None:
                    self._respond(*error_response)
                    return
                status, payload = names_api.handle_put(name, body)
            else:
                status, payload = names_api.handle_delete(name)
        except Exception:
            logger.exception("names_api: unhandled error for %s %s", method, self.path)
            status, payload = 500, _error("internal error", code="internal_error")
        self._respond(status, payload)

    def _extract_name(self) -> "str | None":
        path = self.path.split("?", 1)[0]
        if not path.startswith(_NAME_PREFIX):
            return None
        raw = path[len(_NAME_PREFIX):]
        if not raw or "/" in raw:
            return None
        return unquote(raw)

    def _read_body(self) -> "tuple[bytes, tuple[int, dict[str, Any]] | None]":
        try:
            length = int(self.headers.get("Content-Length", "0") or "0")
        except ValueError:
            return b"", (400, _error("bad Content-Length"))
        if length < 0:
            return b"", (400, _error("bad Content-Length"))
        if length > MAX_BODY:
            return b"", (413, _error("body too large", code="too_large"))
        return self.rfile.read(length) if length else b"", None

    def _respond(self, status: int, payload: "dict[str, Any]") -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def log_message(self, format: str, *args: object) -> None:  # noqa: A002
        logger.debug("names_api: " + format, *args)
