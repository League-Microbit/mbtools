"""Tests for mbtools.registry.identity — the one module that opens a port
to read a board's announcement, and the one that parses it (ticket 002).

Every test here runs against mbtools.testing.fakes.FakeSerial — no real
serial port is opened, per the ticket's acceptance criteria.
"""

from __future__ import annotations

import logging

import pytest

from mbtools.common import DAPLINK_VID_PID as COMMON_VID_PID
from mbtools.registry import identity
from mbtools.testing.fakes import FakeSerial


def _factory(**fake_kwargs):
    """A serial_factory matching probe()'s call convention: factory(baudrate=,
    timeout=, dsrdtr=, rtscts=) -> a FakeSerial scripted with fake_kwargs."""

    def factory(**serial_kwargs):
        return FakeSerial(**fake_kwargs, **serial_kwargs)

    return factory


class _OrderedFakeSerial(FakeSerial):
    """A ``FakeSerial`` that appends every ``send_break``/``write`` call,
    in call order, to a shared ``call_log`` -- lets a test prove BREAK was
    actually sent *before* the first ``HELLO`` write (ticket 001, sprint
    005), not just that both happened somewhere during the probe."""

    def __init__(self, call_log: list[tuple[str, object]], **kwargs: object) -> None:
        super().__init__(**kwargs)
        self._call_log = call_log

    def send_break(self, duration: float = 0.25) -> None:
        self._call_log.append(("break", duration))
        super().send_break(duration)

    def write(self, data: bytes) -> int:
        self._call_log.append(("write", data))
        return super().write(data)


# ---------------------------------------------------------------------------
# probe() — both dialects
# ---------------------------------------------------------------------------

DIALECT_CASES = [
    pytest.param(
        "DEVICE:RADIOBRIDGE:relay:getez:1779042496",
        identity.ProbeResult(
            role="RADIOBRIDGE",
            common_name="relay",
            device_name="getez",
            serial="1779042496",
            raw="DEVICE:RADIOBRIDGE:relay:getez:1779042496",
        ),
        id="colon-dialect-relay",
    ),
    pytest.param(
        # The historical regression case: the robot dialect must parse,
        # not just the colon-delimited one (sprint.md / ticket 002 —
        # a reflashed board silently kept a stale RADIOBRIDGE role
        # because this dialect went unparsed for a period).
        "device NEZHA2 robot vevov 1198504156",
        identity.ProbeResult(
            role="NEZHA2",
            common_name="robot",
            device_name="vevov",
            serial="1198504156",
            raw="device NEZHA2 robot vevov 1198504156",
        ),
        id="robot-dialect-regression",
    ),
    pytest.param(
        # Serial itself contains ':' -- the colon dialect rejoins the tail.
        "DEVICE:RADIORELAY:relay:zavaz:abcd:1234",
        identity.ProbeResult(
            role="RADIORELAY",
            common_name="relay",
            device_name="zavaz",
            serial="abcd:1234",
            raw="DEVICE:RADIORELAY:relay:zavaz:abcd:1234",
        ),
        id="colon-dialect-serial-with-colon",
    ),
]


@pytest.mark.parametrize("line, expected", DIALECT_CASES)
def test_probe_parses_both_dialects(line, expected):
    result = identity.probe(
        "/dev/ttyACM0",
        timeout_s=0.05,
        serial_factory=_factory(announcement=line),
        settle_s=0,
    )
    assert result == expected


def test_probe_sends_hello_and_holds_dtr_rts_low():
    factory = _factory(announcement="device NEZHA2 robot vevov 1198504156")
    fakes: list[FakeSerial] = []

    def capturing_factory(**kwargs):
        ser = factory(**kwargs)
        fakes.append(ser)
        return ser

    identity.probe(
        "/dev/ttyACM0", timeout_s=0.05, serial_factory=capturing_factory, settle_s=0
    )
    assert len(fakes) == 1
    ser = fakes[0]
    assert ser.dtr is False
    assert ser.rts is False
    assert ser.written == [b"HELLO\n"]
    assert ser.reset_input_buffer_calls == 1
    assert ser.close_calls == 1


# ---------------------------------------------------------------------------
# probe() — reset_first (ticket 001, sprint 005): BREAK before HELLO
# ---------------------------------------------------------------------------


def test_probe_reset_first_sends_break_before_hello():
    """reset_first=True asserts a BREAK before HELLO is ever written --
    proven against an ordered call log (BREAK strictly before the first
    write), not just that ``break_calls`` ends up non-empty, per the
    ticket's own acceptance criterion."""
    from mbtools.serial.connect import BREAK_DURATION

    call_log: list[tuple[str, object]] = []
    fakes: list[FakeSerial] = []

    def capturing_factory(**kwargs):
        ser = _OrderedFakeSerial(
            call_log, announcement="device NEZHA2 robot vevov 1198504156", **kwargs
        )
        fakes.append(ser)
        assert ser.break_calls == []  # nothing sent at construction
        return ser

    result = identity.probe(
        "/dev/ttyACM0",
        timeout_s=0.05,
        serial_factory=capturing_factory,
        settle_s=0,
        reset_first=True,
    )
    ser = fakes[0]
    assert ser.break_calls == [BREAK_DURATION]
    assert ser.written == [b"HELLO\n"]
    assert call_log[0] == ("break", BREAK_DURATION)
    assert call_log[1] == ("write", b"HELLO\n")
    assert result is not None
    assert result.role == "NEZHA2"


def test_probe_reset_first_false_default_never_sends_break():
    """reset_first defaults to False -- every existing call site/test is
    unaffected: no send_break call, same read-window/retry logic."""
    fakes: list[FakeSerial] = []

    def capturing_factory(**kwargs):
        ser = FakeSerial(announcement="device NEZHA2 robot vevov 1198504156", **kwargs)
        fakes.append(ser)
        return ser

    result = identity.probe(
        "/dev/ttyACM0",
        timeout_s=0.05,
        serial_factory=capturing_factory,
        settle_s=0,
    )
    assert fakes[0].break_calls == []
    assert result is not None
    assert result.role == "NEZHA2"


def test_probe_reset_first_forwarded_announcement_case():
    """Regression test for the bug this ticket fixes: a relay parked in
    the data plane can have radio-forwarded traffic already flowing, so a
    plain HELLO-based probe might read a fragment of another device's
    announcement before ever writing anything. reset_first=True must
    assert BREAK before the very first write -- proven here by scripting
    a FakeSerial whose readline() would return a (misleading) robot-
    dialect announcement even before any HELLO write (announcement_after
    _writes defaults to 0), and asserting the BREAK happened first."""
    from mbtools.serial.connect import BREAK_DURATION

    call_log: list[tuple[str, object]] = []
    fakes: list[FakeSerial] = []

    def capturing_factory(**kwargs):
        # Radio-forwarded traffic already flowing: readline() would
        # return an announcement-shaped line with zero writes required.
        ser = _OrderedFakeSerial(
            call_log, announcement="device NEZHA2 robot vevov 1198504156", **kwargs
        )
        fakes.append(ser)
        return ser

    identity.probe(
        "/dev/ttyACM0",
        timeout_s=0.05,
        serial_factory=capturing_factory,
        settle_s=0,
        reset_first=True,
    )
    ser = fakes[0]
    # The BREAK was asserted before the first write -- proving the fix
    # intercepts the ambiguous read path before HELLO (and before the
    # reset_input_buffer() that would otherwise just discard, not
    # prevent, the forwarded line).
    assert ser.break_calls == [BREAK_DURATION]
    assert call_log[0] == ("break", BREAK_DURATION)
    assert ("write", b"HELLO\n") in call_log
    assert call_log.index(("break", BREAK_DURATION)) < call_log.index(
        ("write", b"HELLO\n")
    )


# ---------------------------------------------------------------------------
# probe() — timeout and malformed lines
# ---------------------------------------------------------------------------


def test_probe_returns_none_on_timeout():
    result = identity.probe(
        "/dev/ttyACM0", timeout_s=0.02, serial_factory=_factory(), settle_s=0
    )
    assert result is None


def test_probe_malformed_line_does_not_crash_and_keeps_raw():
    line = "garbage not matching either dialect"
    result = identity.probe(
        "/dev/ttyACM0",
        timeout_s=0.02,
        serial_factory=_factory(announcement=line),
        settle_s=0,
    )
    assert result is not None
    assert result.role == ""
    assert result.common_name == ""
    assert result.device_name == ""
    assert result.serial == ""
    assert result.raw == line


def test_probe_short_dialect_line_is_malformed_not_a_crash():
    # Starts like the colon dialect but doesn't have all five fields.
    line = "DEVICE:RADIORELAY:relay"
    result = identity.probe(
        "/dev/ttyACM0",
        timeout_s=0.02,
        serial_factory=_factory(announcement=line),
        settle_s=0,
    )
    assert result is not None
    assert result.raw == line
    assert result.role == ""


# ---------------------------------------------------------------------------
# probe() — busy port
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# probe() — bounded extra HELLO retry (ticket 004)
# ---------------------------------------------------------------------------


def test_probe_retries_hello_once_after_silent_first_window():
    # Silent through the first window, then answers only once a *second*
    # HELLO has actually been written -- proves the retry fires and isn't
    # just re-reading the same window.
    line = "device NEZHA2 robot vevov 1198504156"
    fakes: list[FakeSerial] = []

    def capturing_factory(**kwargs):
        ser = FakeSerial(announcement=line, announcement_after_writes=2, **kwargs)
        fakes.append(ser)
        return ser

    result = identity.probe(
        "/dev/ttyACM0",
        timeout_s=0.02,
        serial_factory=capturing_factory,
        settle_s=0,
    )
    assert len(fakes) == 1
    assert fakes[0].written == [b"HELLO\n", b"HELLO\n"]
    assert result == identity.ProbeResult(
        role="NEZHA2",
        common_name="robot",
        device_name="vevov",
        serial="1198504156",
        raw=line,
    )


def test_probe_still_times_out_after_both_windows_silent():
    # Never answers at all: the retry must fire exactly once (not loop
    # forever) and the outcome must stay the pre-ticket-004 "no firmware"
    # None, not something new.
    fakes: list[FakeSerial] = []

    def capturing_factory(**kwargs):
        ser = FakeSerial(**kwargs)
        fakes.append(ser)
        return ser

    result = identity.probe(
        "/dev/ttyACM0",
        timeout_s=0.02,
        serial_factory=capturing_factory,
        settle_s=0,
    )
    assert result is None
    assert len(fakes) == 1
    assert fakes[0].written == [b"HELLO\n", b"HELLO\n"]


def test_probe_malformed_first_window_retries_and_keeps_malformed_outcome():
    # First window captures an unparseable line (not silence); the second
    # window then comes up empty. The retry still fires exactly once, and
    # the malformed outcome captured in the first window is preserved
    # rather than being discarded because the second window found nothing.
    line = "garbage not matching either dialect"
    fakes: list[FakeSerial] = []

    def capturing_factory(**kwargs):
        ser = FakeSerial(announcement=line, announcement_after_writes=0, **kwargs)
        fakes.append(ser)
        return ser

    result = identity.probe(
        "/dev/ttyACM0",
        timeout_s=0.02,
        serial_factory=capturing_factory,
        settle_s=0,
    )
    assert fakes[0].written == [b"HELLO\n", b"HELLO\n"]
    assert result is not None
    assert result.role == ""
    assert result.raw == line


def test_probe_first_window_success_sends_only_one_hello():
    # Regression-safety (this sprint's Architecture explicitly claims it):
    # a board that answers within the first window is untouched -- no
    # second HELLO, same shape as before ticket 004.
    line = "device NEZHA2 robot vevov 1198504156"
    fakes: list[FakeSerial] = []

    def capturing_factory(**kwargs):
        ser = FakeSerial(announcement=line, **kwargs)
        fakes.append(ser)
        return ser

    result = identity.probe(
        "/dev/ttyACM0",
        timeout_s=0.05,
        serial_factory=capturing_factory,
        settle_s=0,
    )
    assert fakes[0].written == [b"HELLO\n"]
    assert result is not None
    assert result.role == "NEZHA2"


def test_probe_busy_port_returns_none_never_raises(monkeypatch, caplog):
    monkeypatch.setattr(identity, "port_holder", lambda port: "flashtool (pid 4242)")
    with caplog.at_level(logging.WARNING, logger=identity.logger.name):
        result = identity.probe(
            "/dev/ttyACM0", timeout_s=0.02, serial_factory=_factory(busy=True)
        )
    assert result is None
    assert any("flashtool (pid 4242)" in record.message for record in caplog.records)


# ---------------------------------------------------------------------------
# port_holder()
# ---------------------------------------------------------------------------


def test_port_holder_names_the_holding_process(monkeypatch):
    def fake_run(cmd, capture_output, text, timeout):
        if cmd[0] == "lsof":
            return type("R", (), {"stdout": "4242\n"})()
        if cmd[0] == "ps":
            return type("R", (), {"stdout": "mbdeploy flash vevov\n"})()
        raise AssertionError(f"unexpected command: {cmd}")

    monkeypatch.setattr(identity.subprocess, "run", fake_run)
    result = identity.port_holder("/dev/ttyACM0")
    assert result == "mbdeploy flash vevov (pid 4242)"


def test_port_holder_falls_back_when_lsof_finds_nothing(monkeypatch):
    def fake_run(cmd, capture_output, text, timeout):
        return type("R", (), {"stdout": ""})()

    monkeypatch.setattr(identity.subprocess, "run", fake_run)
    assert identity.port_holder("/dev/ttyACM0") == "another program"


def test_port_holder_swallows_subprocess_failures(monkeypatch):
    def raising_run(*args, **kwargs):
        raise OSError("lsof not found")

    monkeypatch.setattr(identity.subprocess, "run", raising_run)
    assert identity.port_holder("/dev/ttyACM0") == "another program"


# ---------------------------------------------------------------------------
# is_relay()
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "role, expected",
    [
        ("RADIORELAY", True),
        ("RADIOBRIDGE", True),
        ("radiobridge", True),
        ("RadioRelay", True),
        ("NEZHA2", False),
        ("", False),
        (None, False),
    ],
)
def test_is_relay(role, expected):
    assert identity.is_relay(role) is expected


# ---------------------------------------------------------------------------
# short_uid()
# ---------------------------------------------------------------------------


def test_short_uid_well_formed():
    uid = "9900" + "0000" + "11112222" + "3333444455556666" + "77778888" + "6e052820"
    assert len(uid) == 48
    assert identity.short_uid(uid) == "3333444455556666"[:8]


def test_short_uid_falls_back_on_short_uid():
    uid = "deadbeef"
    assert identity.short_uid(uid) == "deadbeef"


def test_short_uid_falls_back_last_eight_under_32_chars():
    # Fewer than 32 chars: too short to have a [16:24] middle field, so
    # short_uid falls back to the last 8 characters (mbrelay's own
    # fallback), same as the well-formed slice would land on for a
    # full-length UID.
    uid = "0123456789abcdef1234"
    assert len(uid) < 32
    assert identity.short_uid(uid) == uid[-8:]


# ---------------------------------------------------------------------------
# is_micro_bit_port() / shared VID:PID constant
# ---------------------------------------------------------------------------


def test_daplink_vid_pid_is_the_shared_common_constant():
    assert identity.DAPLINK_VID_PID is COMMON_VID_PID


def test_is_micro_bit_port_matches_daplink_vid_pid():
    vid, pid = COMMON_VID_PID
    assert identity.is_micro_bit_port(vid, pid) is True


def test_is_micro_bit_port_rejects_other_vid_pid():
    assert identity.is_micro_bit_port(0x1234, 0x5678) is False
