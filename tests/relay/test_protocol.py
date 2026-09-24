"""Tests for ``mbtools.relay.protocol`` (sprint 004, ticket 003).

Everything here runs against :class:`FakeByteChannel`, an in-memory
``ByteChannel`` -- no serial hardware, no registry, mirroring how the
legacy ``microbit-radio-relay`` repo tested ``relay.py``/``session.py``
against its own fake channel. All timing knobs passed to
:class:`~mbtools.relay.protocol.RelayControl` in this file are small
(milliseconds, not the ~seconds legacy defaults) purely to keep the suite
fast -- the *sequencing* under test is otherwise exactly the production
path.
"""

from __future__ import annotations

import inspect
from typing import Callable

import pytest

from mbtools.relay import protocol
from mbtools.relay.protocol import (
    DEFAULT_CFG,
    BannerInfo,
    ByteChannel,
    ChannelFactory,
    NORMALIZE_STEPS,
    Reader,
    RelayControl,
    RelayError,
)

# A relay board's banner in the current, colon-delimited dialect.
RADIOBRIDGE_BANNER = b"DEVICE:RADIOBRIDGE:relay:togov:1234\n"
# The older relay family's space-delimited dialect (IDENTITY_RE's second
# alternative).
RADIORELAY_BANNER = b"device RADIORELAY relay getez 1784514240\n"

# Replies for the five NORMALIZE_STEPS commands, each satisfying its own
# acknowledgement pattern.
NORMALIZE_ACKS: dict[bytes, bytes] = {
    b"!MODE RAW250\n": b"# mode: RAW250\n",
    b"!FRAG OFF\n": b"# frag: OFF\n",
    b"!ECHO OFF\n": b"# echo: OFF\n",
    b"!P 7\n": b"# channel: 0 group: 10 mode: RAW250 power: 7\n",
    b"!C 0\n": b"# channel: 0 group: 10\n",
}
# The standalone `?` verification query's reply, confirming DEFAULT_CFG.
DEFAULT_QUERY_REPLY = b"# channel: 0 group: 10 mode: RAW250 power: 7\n"


class FakeByteChannel:
    """A scripted, in-memory :class:`ByteChannel`.

    ``script`` maps a written command to either a fixed reply (bytes) or a
    callable ``(channel) -> bytes | None`` for stateful behavior (e.g. "only
    answer HELLO after a break", "only answer on the Nth attempt"). Replies
    are delivered synchronously, from inside :meth:`write_nowait`, exactly
    as a same-thread fake needs to for :class:`Reader.wait_for` to see them
    without an actual wait.
    """

    def __init__(self, script: "dict[bytes, bytes | Callable[[FakeByteChannel], bytes | None]] | None" = None) -> None:
        self.script = dict(script or {})
        self.written: list[bytes] = []
        self.break_calls = 0
        self.opened = False
        self.closed = False
        self._on_data: Callable[[bytes], None] = lambda _: None
        self._on_error: Callable[[object], None] = lambda _: None

    def open(self) -> None:
        self.opened = True

    def close(self) -> None:
        self.closed = True

    def start_reading(self, on_data, on_error) -> None:
        self._on_data, self._on_error = on_data, on_error

    def stop_reading(self) -> None:
        self._on_data = lambda _: None
        self._on_error = lambda _: None

    def write_nowait(self, data: bytes) -> None:
        self.written.append(data)
        handler = self.script.get(data)
        if handler is None:
            return
        reply = handler(self) if callable(handler) else handler
        if reply:
            self._on_data(reply)

    def send_break(self, duration: float = 0.4) -> None:
        self.break_calls += 1

    def drain(self, timeout: float = 2.0) -> None:
        pass

    @property
    def pending_bytes(self) -> int:
        return 0

    def set_watermarks(self, on_high, on_low) -> None:
        pass


def make_control(**overrides) -> RelayControl:
    """A RelayControl with fast timeouts, for a responsive test suite."""
    kwargs = dict(open_settle=0.0, hello_timeout=0.05, hello_attempts=2,
                  post_close_settle=0.0, break_duration=0.0, break_settle=0.01)
    kwargs.update(overrides)
    return RelayControl(**kwargs)


def normalize_script(query_reply: bytes = DEFAULT_QUERY_REPLY,
                     hello_reply: bytes = RADIOBRIDGE_BANNER) -> dict:
    """A full script for reset_and_normalize(): HELLO, !VER? (reset_and_normalize's
    own firmware-version query), then the normalize batch and its verification
    query."""
    script = dict(NORMALIZE_ACKS)
    script[b"?\n"] = query_reply
    script[b"HELLO\n"] = hello_reply
    script[b"!VER?\n"] = b"# version: 1.2.3\n"
    return script


# ---------------------------------------------------------------------------
# BannerInfo.parse -- both announcement dialects
# ---------------------------------------------------------------------------

def test_bannerinfo_parse_radiobridge_colon_dialect():
    info = BannerInfo.parse(RADIOBRIDGE_BANNER)
    assert info is not None
    assert info.role == "RADIOBRIDGE"
    assert info.device_name == "togov"
    assert info.serial == "1234"


def test_bannerinfo_parse_radiorelay_space_dialect():
    info = BannerInfo.parse(RADIORELAY_BANNER)
    assert info is not None
    assert info.role == "RADIORELAY"
    assert info.device_name == "getez"
    assert info.serial == "1784514240"


def test_bannerinfo_parse_returns_none_for_unrelated_data():
    assert BannerInfo.parse(b"garbage\n") is None


# ---------------------------------------------------------------------------
# hello()
# ---------------------------------------------------------------------------

def test_hello_succeeds_on_first_attempt():
    channel = FakeByteChannel({b"HELLO\n": RADIOBRIDGE_BANNER})
    control = make_control()
    reader = Reader(channel)

    info = control.hello(channel, reader)

    assert info.device_name == "togov"
    assert channel.written.count(b"HELLO\n") == 1
    assert channel.break_calls == 0


def test_hello_retries_per_hello_attempts_before_succeeding():
    control = make_control(hello_attempts=3)
    calls = {"n": 0}

    def responder(ch: FakeByteChannel):
        calls["n"] += 1
        return RADIOBRIDGE_BANNER if calls["n"] >= 3 else None

    channel = FakeByteChannel({b"HELLO\n": responder})
    reader = Reader(channel)

    info = control.hello(channel, reader)

    assert info.device_name == "togov"
    assert channel.written.count(b"HELLO\n") == 3
    assert channel.break_calls == 0


def test_hello_falls_back_to_break_when_nothing_answers():
    """A board stuck in the data plane never answers HELLO until BREAK."""
    control = make_control(hello_attempts=1)

    def responder(ch: FakeByteChannel):
        return RADIOBRIDGE_BANNER if ch.break_calls >= 1 else None

    channel = FakeByteChannel({b"HELLO\n": responder})
    reader = Reader(channel)

    info = control.hello(channel, reader)

    assert info.device_name == "togov"
    assert channel.break_calls == 1
    # one failed attempt pre-break, one successful attempt post-break
    assert channel.written.count(b"HELLO\n") == 2


def test_hello_raises_relay_error_when_break_does_not_recover_board():
    control = make_control(hello_attempts=1)
    channel = FakeByteChannel({})  # never answers, even after break
    reader = Reader(channel)

    with pytest.raises(RelayError):
        control.hello(channel, reader)

    assert channel.break_calls == 1


# ---------------------------------------------------------------------------
# normalize() / query()
# ---------------------------------------------------------------------------

def test_normalize_sends_steps_in_order_and_verifies_with_standalone_query():
    channel = FakeByteChannel(normalize_script())
    control = make_control()
    reader = Reader(channel)

    control.normalize(channel, reader)

    step_commands = [cmd for cmd, _ in NORMALIZE_STEPS]
    # every step command was sent, in order, followed by the verification query
    sent = [w for w in channel.written if w in {*step_commands, b"?\n"}]
    assert sent == [*step_commands, b"?\n"]


def test_normalize_raises_relay_error_if_verification_never_confirms_defaults():
    wrong_reply = b"# channel: 5 group: 20 mode: RAW250 power: 7\n"
    channel = FakeByteChannel(normalize_script(query_reply=wrong_reply))
    control = make_control()
    reader = Reader(channel)

    with pytest.raises(RelayError):
        control.normalize(channel, reader, retries=0)


def test_query_reports_default_cfg_tuple_fields():
    channel = FakeByteChannel({b"?\n": DEFAULT_QUERY_REPLY})
    control = make_control()
    reader = Reader(channel)

    match = control.query(channel, reader)

    assert match is not None
    channel_no, group, mode, power = match.groups()
    assert (int(channel_no), int(group), mode, int(power)) == DEFAULT_CFG


# ---------------------------------------------------------------------------
# clear_stored_config()
# ---------------------------------------------------------------------------

def test_clear_stored_config_sends_defaults_command():
    channel = FakeByteChannel({b"!DEFAULTS\n": b"# stored config cleared\n"})
    control = make_control()
    reader = Reader(channel)

    control.clear_stored_config(channel, reader)

    assert channel.written == [b"!DEFAULTS\n"]


# ---------------------------------------------------------------------------
# reset_and_normalize() -- the full acquire/release sequence
# ---------------------------------------------------------------------------

def test_reset_and_normalize_runs_full_sequence_in_order():
    script = normalize_script()
    script[b"!DEFAULTS\n"] = b"# stored config cleared\n"
    channel = FakeByteChannel(script)
    control = make_control()

    info = control.reset_and_normalize(channel)

    assert info.device_name == "togov"
    assert info.firmware == "1.2.3"
    assert channel.opened is True
    assert channel.closed is True
    step_commands = [cmd for cmd, _ in NORMALIZE_STEPS]
    assert channel.written == [
        b"HELLO\n", b"!VER?\n", *step_commands, b"?\n", b"!DEFAULTS\n",
    ]


def test_reset_and_normalize_skips_clear_stored_when_requested():
    script = normalize_script()
    channel = FakeByteChannel(script)
    control = make_control()

    control.reset_and_normalize(channel, clear_stored=False)

    assert b"!DEFAULTS\n" not in channel.written


def test_reset_and_normalize_closes_channel_even_on_failure():
    channel = FakeByteChannel({})  # HELLO never answered -> hello() raises
    control = make_control(hello_attempts=1)

    with pytest.raises(RelayError):
        control.reset_and_normalize(channel)

    assert channel.opened is True
    assert channel.closed is True


# ---------------------------------------------------------------------------
# probe()
# ---------------------------------------------------------------------------

def test_probe_returns_none_when_nothing_answers():
    channel = FakeByteChannel({})
    control = make_control(hello_attempts=1)

    assert control.probe(channel) is None
    assert channel.closed is True


def test_probe_fetches_firmware_version_for_radiobridge():
    script = {
        b"HELLO\n": RADIOBRIDGE_BANNER,
        b"!VER?\n": b"# version: 1.2.3\n",
    }
    channel = FakeByteChannel(script)
    control = make_control()

    info = control.probe(channel)

    assert info is not None
    assert info.role == "RADIOBRIDGE"
    assert info.firmware == "1.2.3"


def test_probe_does_not_query_firmware_for_old_radiorelay_family():
    channel = FakeByteChannel({b"HELLO\n": RADIORELAY_BANNER})
    control = make_control()

    info = control.probe(channel)

    assert info is not None
    assert info.role == "RADIORELAY"
    assert info.firmware == ""
    assert b"!VER?\n" not in channel.written


def test_probe_asks_robot_version_for_non_relay_role():
    script = {
        b"HELLO\n": b"device NEZHA2 robot tovez 2314287040\n",
        b"VER\n": b"ver 4.5.6\n",
    }
    channel = FakeByteChannel(script)
    control = make_control()

    info = control.probe(channel)

    assert info is not None
    assert info.role == "NEZHA2"
    assert info.firmware == "4.5.6"


# ---------------------------------------------------------------------------
# module boundary -- no coupling to inventory.py/firmware.py/admin.py or any
# mDNS-advertiser code (acceptance criterion; enforced here so a later, less
# careful edit can't silently reintroduce the coupling this ticket removed).
# ---------------------------------------------------------------------------

def test_module_has_no_forbidden_imports():
    """AC: no import of inventory.py/firmware.py/admin.py/any mDNS-advertiser
    code. Checked against the parsed import graph, not the source text --
    the module legitimately uses the plain English word "firmware" (e.g.
    ``firmware_version``, the ``firmware`` field) without importing
    ``firmware.py``.
    """
    import ast

    tree = ast.parse(inspect.getsource(protocol))
    imported_names: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported_names.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported_names.append(node.module)

    forbidden = ("inventory", "firmware", "admin", "mdns", "advertiser")
    for name in imported_names:
        for term in forbidden:
            assert term not in name, f"unexpected import of {name!r}"


def test_byte_channel_and_channel_factory_are_protocols():
    # Ported "as-is (interface only)" per the ticket's Approach -- sanity check
    # both are still typing.Protocol classes, not accidentally concretized.
    assert getattr(ByteChannel, "_is_protocol", False) is True
    assert getattr(ChannelFactory, "_is_protocol", False) is True
