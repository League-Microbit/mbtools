"""Tests for mbtools.registry.console_compat.names_api (sprint 004,
ticket 007) -- the ``GET/PUT/DELETE /names/<name>`` HTTP listener.

Mirrors ``tests/registry/console_compat/test_relay_pool.py``'s own
precedent ("network services with an existing precedent" -- sprint.md's
Test Strategy): driven over a real loopback HTTP connection
(``port=0``, an OS-chosen ephemeral port via ``bound_port``), against a
real ``Store`` backed by a ``tmp_path`` SQLite file -- no fake/mock
storage layer, same convention as ``tests/registry/store/
test_store_name_registry.py``.

Field-by-field response-shape assertions are checked directly against
``packages/host/src/mbrelayRegistry.ts``'s own ``parseResolvedAddress``
(exact keys ``channel``/``group``/``source``, ``channel``/``group`` as
JSON numbers, ``source`` a recognized string) -- this ticket's own
acceptance criterion, not just this test file's own assumption.
"""

from __future__ import annotations

import http.client
import json

import pytest

from mbtools.relay import naming
from mbtools.registry.console_compat.names_api import (
    CHANNEL_MAX,
    CHANNEL_MIN,
    GROUP_MAX,
    GROUP_MIN,
    NamesAPI,
)
from mbtools.registry.store import SOURCE_DERIVED, SOURCE_REGISTRY, Store

# Well-formed names (CVCVC over zvgpt/uoiea), matching
# tests/registry/store/test_store_name_registry.py's own picks -- their
# derived addresses are all distinct, so any conflict is one a test
# deliberately creates, never one derivation itself could produce.
NAME_A = "zuzuz"
NAME_B = "tatat"
NAME_UNSEEN = "vevov"

MALFORMED_NAME = "not-a-name"


# ---------------------------------------------------------------------------
# fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def store(tmp_path):
    s = Store(tmp_path / "devices.db")
    yield s
    s.close()


class _Recorder:
    """Captures every ``name_set_callback``/``name_clear_callback``
    invocation, in order -- what the tests use to assert "every write
    replicates, exactly once" (module docstring)."""

    def __init__(self) -> None:
        self.set_calls: list = []
        self.clear_calls: list[str] = []

    def on_set(self, entry) -> None:
        self.set_calls.append(entry)

    def on_clear(self, name: str) -> None:
        self.clear_calls.append(name)


@pytest.fixture
def recorder() -> _Recorder:
    return _Recorder()


@pytest.fixture
def api(store, recorder):
    server = NamesAPI(
        store=store,
        host="127.0.0.1",
        port=0,
        name_set_callback=recorder.on_set,
        name_clear_callback=recorder.on_clear,
    )
    server.start()
    yield server
    server.stop()


def _request(
    api: NamesAPI, method: str, name: str, *, raw_body: "bytes | None" = None,
    headers: "dict[str, str] | None" = None,
) -> "tuple[int, object]":
    conn = http.client.HTTPConnection("127.0.0.1", api.bound_port, timeout=2)
    try:
        conn.request(method, f"/names/{name}", body=raw_body, headers=headers or {})
        resp = conn.getresponse()
        raw = resp.read()
        body = json.loads(raw) if raw else None
        return resp.status, body
    finally:
        conn.close()


def _put(api: NamesAPI, name: str, channel: int, group: int) -> "tuple[int, object]":
    return _request(
        api, "PUT", name, raw_body=json.dumps({"channel": channel, "group": group}).encode()
    )


# ---------------------------------------------------------------------------
# GET -- write-on-read / derive-and-persist
# ---------------------------------------------------------------------------


def test_get_unseen_name_derives_and_persists(api, recorder):
    channel, group = naming.name_to_radio(NAME_UNSEEN)

    status, body = _request(api, "GET", NAME_UNSEEN)
    assert status == 200
    assert body == {"channel": channel, "group": group, "source": SOURCE_DERIVED}
    assert len(recorder.set_calls) == 1
    assert recorder.set_calls[0].name == NAME_UNSEEN

    # Repeat GET: identical value, not recomputed, and -- this ticket's
    # own "no unbounded growth or side effect beyond the first derive"
    # acceptance criterion -- no second replication publish.
    status2, body2 = _request(api, "GET", NAME_UNSEEN)
    assert status2 == 200
    assert body2 == body
    assert len(recorder.set_calls) == 1


def test_get_idempotent_under_repeated_polling(api, recorder):
    """Several GETs in a row (simulating robot-console re-polling within
    its cache TTL) never grow the row count or fire more than one
    publish."""
    for _ in range(5):
        status, _body = _request(api, "GET", NAME_UNSEEN)
        assert status == 200
    assert len(recorder.set_calls) == 1
    assert [e.name for e in api._store.listing()] == [NAME_UNSEEN]


# ---------------------------------------------------------------------------
# PUT -- explicit assignment
# ---------------------------------------------------------------------------


def test_put_valid_persists_registry_source(api, recorder):
    status, body = _put(api, NAME_A, 20, 30)
    assert status == 200
    assert body == {"channel": 20, "group": 30, "source": SOURCE_REGISTRY}
    assert len(recorder.set_calls) == 1
    assert recorder.set_calls[0].name == NAME_A
    assert recorder.set_calls[0].source == SOURCE_REGISTRY

    # A subsequent GET returns the explicit value unchanged, and -- since
    # nothing was derived-and-persisted this time -- fires no further
    # publish.
    status2, body2 = _request(api, "GET", NAME_A)
    assert status2 == 200
    assert body2 == body
    assert len(recorder.set_calls) == 1


@pytest.mark.parametrize(
    "payload",
    [
        b"not json",
        b"[]",
        b'"a string"',
        json.dumps({"channel": 5}).encode(),
        json.dumps({"group": 5}).encode(),
        json.dumps({"channel": "x", "group": 5}).encode(),
        json.dumps({"channel": 5, "group": "y"}).encode(),
        json.dumps({"channel": CHANNEL_MIN - 1, "group": 5}).encode(),
        json.dumps({"channel": CHANNEL_MAX + 1, "group": 5}).encode(),
        json.dumps({"channel": 5, "group": GROUP_MIN - 1}).encode(),
        json.dumps({"channel": 5, "group": GROUP_MAX + 1}).encode(),
    ],
)
def test_put_malformed_body_400_not_clamped(api, recorder, payload):
    status, body = _request(api, "PUT", NAME_A, raw_body=payload)
    assert status == 400
    assert body["error"]["message"]
    # No silent clamp: nothing was written or published.
    assert recorder.set_calls == []
    assert api._store.get_name(NAME_A) is None


def test_put_accepts_full_firmware_range(api):
    """The boundary values themselves (0/0 and 83/255) are valid -- this
    module's own range is ``!CG``'s, not ``naming``'s narrower
    derived-only subrange (module docstring)."""
    status, body = _put(api, NAME_A, CHANNEL_MIN, GROUP_MIN)
    assert status == 200
    assert body == {"channel": CHANNEL_MIN, "group": GROUP_MIN, "source": SOURCE_REGISTRY}

    status2, body2 = _put(api, NAME_B, CHANNEL_MAX, GROUP_MAX)
    assert status2 == 200
    assert body2 == {"channel": CHANNEL_MAX, "group": GROUP_MAX, "source": SOURCE_REGISTRY}


# ---------------------------------------------------------------------------
# DELETE -- clear, then re-derive
# ---------------------------------------------------------------------------


def test_delete_clears_explicit_entry_and_rederives(api, recorder):
    _put(api, NAME_A, 20, 30)
    assert len(recorder.set_calls) == 1

    status, body = _request(api, "DELETE", NAME_A)
    assert status == 200
    channel, group = naming.name_to_radio(NAME_A)
    assert body == {"channel": channel, "group": group, "source": SOURCE_DERIVED}
    assert recorder.clear_calls == [NAME_A]
    # Both writes this DELETE caused are replicated: the clear, and the
    # fresh derive-and-persist that immediately follows it (module
    # docstring's "DELETE re-derives and returns the fresh value").
    assert len(recorder.set_calls) == 2

    # A subsequent GET re-derives to the identical value -- this
    # ticket's own acceptance criterion -- with no further publish
    # (DELETE already persisted it).
    status2, body2 = _request(api, "GET", NAME_A)
    assert status2 == 200
    assert body2 == body
    assert len(recorder.set_calls) == 2


def test_delete_unknown_name_is_not_an_error(api, recorder):
    """Clearing a name with no row is a no-op on the store side (Store.
    clear's own docstring), but this endpoint still re-derives and
    returns a value, and still publishes both events -- same as any
    other DELETE."""
    channel, group = naming.name_to_radio(NAME_B)
    status, body = _request(api, "DELETE", NAME_B)
    assert status == 200
    assert body == {"channel": channel, "group": group, "source": SOURCE_DERIVED}
    assert recorder.clear_calls == [NAME_B]
    assert len(recorder.set_calls) == 1


# ---------------------------------------------------------------------------
# Response shape, field-by-field
# ---------------------------------------------------------------------------


def test_response_shape_matches_mbrelayregistry_contract(api):
    """Cross-checked directly against ``mbrelayRegistry.ts``'s own
    ``parseResolvedAddress``: exactly ``channel``/``group``/``source``,
    ``channel``/``group`` JSON numbers, ``source`` one of the strings it
    recognizes as a real (non-fallback) answer."""
    status, body = _request(api, "GET", NAME_UNSEEN)
    assert status == 200
    assert set(body.keys()) == {"channel", "group", "source"}
    assert isinstance(body["channel"], int)
    assert isinstance(body["group"], int)
    assert isinstance(body["source"], str)
    assert body["source"] in ("registry", "derived")  # never "config" -- Decision 7


# ---------------------------------------------------------------------------
# Malformed name
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("method", ["GET", "PUT", "DELETE"])
def test_malformed_name_400(api, recorder, method):
    raw_body = json.dumps({"channel": 5, "group": 5}).encode() if method == "PUT" else None
    status, body = _request(api, method, MALFORMED_NAME, raw_body=raw_body)
    assert status == 400
    assert body["error"]["message"]
    assert recorder.set_calls == []
    assert recorder.clear_calls == []


# ---------------------------------------------------------------------------
# Routing
# ---------------------------------------------------------------------------


def test_unknown_route_404(api):
    conn = http.client.HTTPConnection("127.0.0.1", api.bound_port, timeout=2)
    try:
        conn.request("GET", "/status")
        resp = conn.getresponse()
        assert resp.status == 404
        resp.read()
    finally:
        conn.close()


def test_empty_name_404(api):
    conn = http.client.HTTPConnection("127.0.0.1", api.bound_port, timeout=2)
    try:
        conn.request("GET", "/names/")
        resp = conn.getresponse()
        assert resp.status == 404
        resp.read()
    finally:
        conn.close()


def test_nested_path_404(api):
    conn = http.client.HTTPConnection("127.0.0.1", api.bound_port, timeout=2)
    try:
        conn.request("GET", f"/names/{NAME_A}/extra")
        resp = conn.getresponse()
        assert resp.status == 404
        resp.read()
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# No auth (ticket 006/007's shared exemption)
# ---------------------------------------------------------------------------


def test_no_auth_token_required(api):
    """robot-console has no mechanism to send one (sprint.md's Migration
    Concerns) -- this listener must not even look for one. A bogus
    Authorization header is accepted exactly like no header at all."""
    status, body = _request(
        api, "GET", NAME_UNSEEN, headers={"Authorization": "Bearer not-a-real-token"}
    )
    assert status == 200
    assert body["source"] == SOURCE_DERIVED
