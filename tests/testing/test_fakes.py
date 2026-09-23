"""Tests for mbtools.testing.fakes — the fakes themselves need coverage
since every later ticket's test suite depends on them scripting exactly
as promised (sprint.md's Test Strategy)."""

from __future__ import annotations

import pytest

from mbtools.common import PortInfo
from mbtools.testing.fakes import DAPLINK_VID_PID, FakeSerial, FakeUSBSource

MATCHING = PortInfo(uid="abc123", port="/dev/ttyACM0", vid=0x0D28, pid=0x0204)
NON_MATCHING = PortInfo(uid="deadbeef", port="/dev/ttyUSB0", vid=0x1234, pid=0x5678)


class TestFakeUSBSource:
    def test_empty_scan(self):
        source = FakeUSBSource([[]])
        assert source.scan() == {}

    def test_scan_with_one_matching_device(self):
        source = FakeUSBSource([[MATCHING]])
        result = source.scan()
        assert result == {"abc123": MATCHING}

    def test_scan_with_one_non_matching_device(self):
        source = FakeUSBSource([[NON_MATCHING]])
        assert source.scan() == {}

    def test_matching_and_non_matching_in_same_snapshot(self):
        source = FakeUSBSource([[MATCHING, NON_MATCHING]])
        result = source.scan()
        assert result == {"abc123": MATCHING}

    def test_scripted_sequence_attach_then_detach(self):
        source = FakeUSBSource([[], [MATCHING], []])
        assert source.scan() == {}
        assert source.scan() == {"abc123": MATCHING}
        assert source.scan() == {}
        assert source.call_count == 3

    def test_exhausted_sequence_repeats_last_snapshot_by_default(self):
        source = FakeUSBSource([[MATCHING]])
        assert source.scan() == {"abc123": MATCHING}
        # No more scripted snapshots -- repeats the last one.
        assert source.scan() == {"abc123": MATCHING}
        assert source.scan() == {"abc123": MATCHING}

    def test_exhausted_sequence_raises_when_configured(self):
        source = FakeUSBSource([[]], exhaust_raises=True)
        source.scan()
        with pytest.raises(IndexError):
            source.scan()

    def test_vid_pid_constant_matches_daplink(self):
        assert DAPLINK_VID_PID == (0x0D28, 0x0204)


class TestFakeSerial:
    def test_scripted_announcement_colon_dialect(self):
        line = "DEVICE:RADIOBRIDGE:relay:getez:1779042496"
        ser = FakeSerial(announcement=line)
        ser.port = "/dev/ttyACM0"
        ser.open()
        assert ser.is_open
        first = ser.readline()
        assert first == (line + "\n").encode("utf-8")
        # Only one scripted line -- subsequent reads are silence.
        assert ser.readline() == b""

    def test_scripted_announcement_space_dialect(self):
        line = "device NEZHA2 robot vevov 1198504156"
        ser = FakeSerial(announcement=line)
        ser.open()
        assert ser.readline() == (line + "\n").encode("utf-8")

    def test_silence_until_timeout(self):
        ser = FakeSerial()
        ser.open()
        assert ser.readline() == b""
        assert ser.readline() == b""

    def test_busy_port_raises_on_open(self):
        ser = FakeSerial(busy=True)
        with pytest.raises(OSError):
            ser.open()
        assert not ser.is_open

    def test_cannot_script_both_announcement_and_busy(self):
        with pytest.raises(ValueError):
            FakeSerial(announcement="device X robot a 1", busy=True)

    def test_dtr_rts_write_and_reset_input_buffer_recorded(self):
        ser = FakeSerial(announcement="device NEZHA2 robot vevov 1198504156")
        ser.dtr = False
        ser.rts = False
        ser.open()
        ser.reset_input_buffer()
        ser.write(b"HELLO\n")
        assert ser.dtr is False
        assert ser.rts is False
        assert ser.written == [b"HELLO\n"]
        assert ser.reset_input_buffer_calls == 1

    def test_close_records_call_and_clears_is_open(self):
        ser = FakeSerial(announcement="device NEZHA2 robot vevov 1198504156")
        ser.open()
        ser.close()
        assert ser.close_calls == 1
        assert not ser.is_open

    def test_announcement_after_writes_gates_on_write_count(self):
        # ticket 004's bounded-retry scenario: silent until a second
        # write() has actually happened, then answers.
        ser = FakeSerial(
            announcement="device NEZHA2 robot vevov 1198504156",
            announcement_after_writes=2,
        )
        ser.open()
        assert ser.readline() == b""  # no write() yet
        ser.write(b"HELLO\n")
        assert ser.readline() == b""  # only one write() so far
        ser.write(b"HELLO\n")
        assert ser.readline() == b"device NEZHA2 robot vevov 1198504156\n"
        # Only one scripted line -- subsequent reads are silence again.
        assert ser.readline() == b""

    def test_announcement_after_writes_defaults_to_immediate(self):
        # Default (0) preserves the original FakeSerial contract: no
        # write() is required before the first readline() answers.
        ser = FakeSerial(announcement="device NEZHA2 robot vevov 1198504156")
        ser.open()
        assert ser.readline() == b"device NEZHA2 robot vevov 1198504156\n"

    def test_accepts_pyserial_style_constructor_kwargs(self):
        ser = FakeSerial(
            announcement="device NEZHA2 robot vevov 1198504156",
            baudrate=115200,
            timeout=0.12,
            dsrdtr=False,
            rtscts=False,
        )
        assert ser.baudrate == 115200
        assert ser.timeout == 0.12
