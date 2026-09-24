"""mbtools.registry.stream_frame -- wire format for the framed binary
data+control sub-protocol a TCP connection to ``registry.remote_api``
switches into once it sends ``{"op": "stream", "uid": "..."}`` (sprint
003, ticket 007; sprint.md Decision 1).

Per sprint.md Decision 1: ``[1-byte type][4-byte big-endian
length][payload]``, five types -- ``DATA`` (both directions), ``BREAK``/
``SET_DTR``/``SET_RTS`` (client -> server control), ``CLOSE`` (either
direction). This module is pure encode/decode with no socket or
``threading`` knowledge, so both this ticket's server-side reader/writer
(``registry.remote_api.RemoteAPIServer._handle_stream``) and a future
client (ticket 011's ``registry.remote_client``) share one implementation
of this five-line spec instead of maintaining two copies that could drift
apart.

**Why a plain callable, not a socket, as :func:`read_frame`'s
argument**: this keeps the frame-decoding logic itself testable with a
canned byte string (see this module's own tests) and reusable against
anything that can hand back "the next N bytes, or fewer at EOF" --
a real ``socket.socket`` wrapped in a small ``_read_exact`` closure (the
server side, ticket 007) or an in-memory buffer (a test, or a future
client's own transport).
"""

from __future__ import annotations

from typing import Callable

__all__ = [
    "FRAME_DATA",
    "FRAME_BREAK",
    "FRAME_SET_DTR",
    "FRAME_SET_RTS",
    "FRAME_CLOSE",
    "FRAME_TYPES",
    "HEADER_SIZE",
    "MAX_FRAME_PAYLOAD",
    "FrameError",
    "encode_frame",
    "read_frame",
]

#: Sprint.md Decision 1's five frame types -- the wire values are part of
#: the protocol contract (docs/design/registry-api.md), never renumbered.
FRAME_DATA = 0x01
FRAME_BREAK = 0x02
FRAME_SET_DTR = 0x03
FRAME_SET_RTS = 0x04
FRAME_CLOSE = 0x05

FRAME_TYPES = frozenset((FRAME_DATA, FRAME_BREAK, FRAME_SET_DTR, FRAME_SET_RTS, FRAME_CLOSE))

#: 1-byte type + 4-byte big-endian length, per sprint.md Decision 1.
HEADER_SIZE = 5

#: A generous ceiling on one frame's declared payload length -- large
#: enough that a real DATA frame (small read()/write() chunks, never a
#: whole file in one frame) never comes close, small enough that a
#: corrupt or hostile declared length can never be used to make this
#: process attempt an unbounded read/allocation (ticket 007 acceptance
#: criterion: "an oversized declared length ... rejected without
#: crashing").
MAX_FRAME_PAYLOAD = 1 << 20  # 1 MiB


class FrameError(ValueError):
    """A frame could not be decoded: the connection ended mid-frame
    (header or payload arrived only partially -- a truncated frame), or
    the declared length exceeds :data:`MAX_FRAME_PAYLOAD`. Either is the
    caller's cue to tear the connection down without trying to interpret
    whatever bytes did arrive -- never raised for a well-formed frame,
    regardless of whether its type byte is one of :data:`FRAME_TYPES`
    (an unrecognized type is a well-formed frame the caller may choose to
    reject on its own terms, not a decode error).
    """


def encode_frame(frame_type: int, payload: bytes = b"") -> bytes:
    """``[1-byte type][4-byte big-endian length][payload]`` -- sprint.md
    Decision 1's exact wire shape. ``frame_type`` is written as-is (not
    validated against :data:`FRAME_TYPES`), so a future protocol
    extension's new type can be encoded here before this module's own
    constants learn about it.
    """
    return bytes((frame_type,)) + len(payload).to_bytes(4, "big") + payload


def read_frame(read_exact: Callable[[int], bytes]) -> tuple[int, bytes] | None:
    """Decode exactly one frame, reading bytes via ``read_exact``.

    ``read_exact(n)`` must return exactly ``n`` bytes, or fewer only when
    the underlying stream ended before ``n`` bytes arrived (an empty
    ``b""`` for "ended before anything new arrived at all"). This
    function never calls ``read_exact`` with a size larger than
    :data:`HEADER_SIZE` or the frame's own declared (and validated)
    length, so an implementation backed by a real socket never needs to
    guess a buffer size.

    Returns ``(frame_type, payload)`` for a complete frame, or ``None``
    if the stream ended cleanly *before* a new frame's header began (the
    ordinary way a stream session ends without an explicit ``CLOSE``
    frame -- a plain connection drop). Raises :class:`FrameError` for a
    truncated frame or an oversized declared length -- see that
    exception's own docstring.
    """
    header = read_exact(HEADER_SIZE)
    if not header:
        return None  # clean EOF at a frame boundary
    if len(header) < HEADER_SIZE:
        raise FrameError(
            f"truncated frame header: got {len(header)} of {HEADER_SIZE} bytes"
        )
    frame_type = header[0]
    length = int.from_bytes(header[1:5], "big")
    if length > MAX_FRAME_PAYLOAD:
        raise FrameError(
            f"declared frame length {length} exceeds the {MAX_FRAME_PAYLOAD}-byte limit"
        )
    if length == 0:
        payload = b""
    else:
        payload = read_exact(length)
        if len(payload) < length:
            raise FrameError(
                f"truncated frame payload: got {len(payload)} of {length} bytes"
            )
    return frame_type, payload
