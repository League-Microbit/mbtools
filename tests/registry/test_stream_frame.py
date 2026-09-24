"""Tests for mbtools.registry.stream_frame -- pure encode/decode for the
[1-byte type][4-byte big-endian length][payload] frame format sprint.md
Decision 1 defines for registry.remote_api's "stream" sub-protocol
(ticket 007).
"""

from __future__ import annotations

import pytest

from mbtools.registry.stream_frame import (
    FRAME_BREAK,
    FRAME_CLOSE,
    FRAME_DATA,
    FRAME_SET_DTR,
    FRAME_SET_RTS,
    FRAME_TYPES,
    HEADER_SIZE,
    MAX_FRAME_PAYLOAD,
    FrameError,
    encode_frame,
    read_frame,
)


def _reader(data: bytes):
    """A ``read_exact``-shaped callable over a fixed byte string, for
    driving :func:`read_frame` against scripted bytes without a real
    socket."""
    buf = bytearray(data)

    def _read_exact(n: int) -> bytes:
        chunk = bytes(buf[:n])
        del buf[: len(chunk)]
        return chunk

    return _read_exact


# ---------------------------------------------------------------------------
# round-trip, every type
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "frame_type,payload",
    [
        (FRAME_DATA, b"hello world"),
        (FRAME_DATA, b""),
        (FRAME_BREAK, b""),
        (FRAME_SET_DTR, b"\x01"),
        (FRAME_SET_DTR, b"\x00"),
        (FRAME_SET_RTS, b"\x01"),
        (FRAME_SET_RTS, b"\x00"),
        (FRAME_CLOSE, b""),
    ],
)
def test_encode_decode_round_trip(frame_type, payload):
    wire = encode_frame(frame_type, payload)
    assert wire[0] == frame_type
    assert int.from_bytes(wire[1:5], "big") == len(payload)
    assert wire[5:] == payload

    decoded = read_frame(_reader(wire))
    assert decoded == (frame_type, payload)


def test_frame_types_constant_matches_sprint_md_decision_1():
    assert FRAME_TYPES == {FRAME_DATA, FRAME_BREAK, FRAME_SET_DTR, FRAME_SET_RTS, FRAME_CLOSE}
    assert FRAME_DATA == 0x01
    assert FRAME_BREAK == 0x02
    assert FRAME_SET_DTR == 0x03
    assert FRAME_SET_RTS == 0x04
    assert FRAME_CLOSE == 0x05


def test_header_size_is_five_bytes():
    assert HEADER_SIZE == 5


def test_multiple_frames_back_to_back():
    wire = encode_frame(FRAME_DATA, b"one") + encode_frame(FRAME_DATA, b"two")
    read_exact = _reader(wire)
    assert read_frame(read_exact) == (FRAME_DATA, b"one")
    assert read_frame(read_exact) == (FRAME_DATA, b"two")
    assert read_frame(read_exact) is None


# ---------------------------------------------------------------------------
# boundary / fuzz cases (ticket 007 acceptance criterion)
# ---------------------------------------------------------------------------


def test_clean_eof_at_frame_boundary_returns_none():
    assert read_frame(_reader(b"")) is None


def test_truncated_header_raises_frame_error():
    wire = encode_frame(FRAME_DATA, b"payload")
    with pytest.raises(FrameError):
        read_frame(_reader(wire[:3]))  # only 3 of 5 header bytes


def test_truncated_payload_raises_frame_error():
    wire = encode_frame(FRAME_DATA, b"payload")
    # keep the whole header (5 bytes) but cut the payload short
    with pytest.raises(FrameError):
        read_frame(_reader(wire[:7]))


def test_zero_length_data_frame_decodes_to_empty_payload():
    wire = encode_frame(FRAME_DATA, b"")
    assert read_frame(_reader(wire)) == (FRAME_DATA, b"")


def test_oversized_declared_length_raises_frame_error_without_reading_payload():
    # A declared length that exceeds MAX_FRAME_PAYLOAD must be rejected
    # from the header alone -- read_frame must never attempt to read
    # that many payload bytes (which would hang forever against a real
    # socket that never sends them).
    header = bytes((FRAME_DATA,)) + (MAX_FRAME_PAYLOAD + 1).to_bytes(4, "big")
    calls: list[int] = []

    def _read_exact(n: int) -> bytes:
        calls.append(n)
        if n == HEADER_SIZE:
            return header
        raise AssertionError("read_frame must not read payload bytes for an oversized length")

    with pytest.raises(FrameError):
        read_frame(_read_exact)
    assert calls == [HEADER_SIZE]


def test_declared_length_exactly_at_the_limit_is_accepted():
    header = bytes((FRAME_DATA,)) + MAX_FRAME_PAYLOAD.to_bytes(4, "big")
    payload = b"x" * MAX_FRAME_PAYLOAD
    frame_type, decoded_payload = read_frame(_reader(header + payload))
    assert frame_type == FRAME_DATA
    assert decoded_payload == payload


def test_unrecognized_frame_type_still_decodes_cleanly():
    # read_frame itself never validates the type byte -- see its own
    # docstring; rejecting an unknown type is the caller's job.
    wire = bytes((0xFF,)) + (0).to_bytes(4, "big")
    assert read_frame(_reader(wire)) == (0xFF, b"")
