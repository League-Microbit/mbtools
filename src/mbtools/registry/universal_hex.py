"""mbtools.registry.universal_hex — flash micro:bit Universal Hex files.

The micro:bit Foundation ships firmware (including the out-of-box
experience) as a *Universal Hex*: one file carrying a V1 image (nRF51) and
a V2 image (nRF52833). Each image is split into blocks that open with a
Block Start record (type ``0x0A``) naming a board ID — ``0x9900``/``0x9901``
for V1, ``0x9903``–``0x9906`` for V2 — and V2 data is carried in
"custom data" records (type ``0x0D``) so that old V1 DAPLink firmware
skips it. Standard Intel-hex parsers (``intelhex``) and pyOCD reject those
record types.

:func:`normalized_hex` turns such a file into an ordinary Intel hex for the
chip being flashed: it keeps only the blocks for that board, rewrites
``0x0D`` records as data (``0x00``), drops the block bookkeeping records
(``0x0A`` start, ``0x0B`` end, ``0x0C`` padding, ``0x0E`` other data), and
ends with a single EOF record. A plain Intel hex passes through untouched.

Spec: https://tech.microbit.org/software/spec-universal-hex/
"""

from __future__ import annotations

import contextlib
import os
import tempfile
from collections.abc import Iterator

__all__ = [
    "UniversalHexError",
    "is_universal_hex",
    "board_ids_for_mcu",
    "extract_for_mcu",
    "normalized_hex",
]

_BLOCK_START = 0x0A
_BLOCK_END = 0x0B
_PADDED_DATA = 0x0C
_CUSTOM_DATA = 0x0D
_OTHER_DATA = 0x0E
_DATA = 0x00
_EOF = 0x01
_EXT_LINEAR_ADDR = 0x04

_V1_BOARD_IDS = frozenset({0x9900, 0x9901})
_V2_BOARD_IDS = frozenset({0x9903, 0x9904, 0x9905, 0x9906})


class UniversalHexError(ValueError):
    """The file is a Universal Hex but has nothing for the target chip."""


def _record_type(line: str) -> int | None:
    line = line.strip()
    if not line.startswith(":") or len(line) < 11:
        return None
    try:
        return int(line[7:9], 16)
    except ValueError:
        return None


def is_universal_hex(text: str) -> bool:
    """True if the hex text contains any Block Start (``0x0A``) record."""
    return any(_record_type(line) == _BLOCK_START for line in text.splitlines())


def board_ids_for_mcu(target_mcu: str) -> frozenset[int]:
    """Board IDs whose blocks apply to a pyOCD target name."""
    mcu = target_mcu.lower()
    if mcu.startswith("nrf51"):
        return _V1_BOARD_IDS
    if mcu.startswith("nrf52"):
        return _V2_BOARD_IDS
    raise UniversalHexError(f"no micro:bit board IDs known for target {target_mcu!r}")


def _retype(line: str, new_type: int) -> str:
    raw = bytearray.fromhex(line.strip()[1:-2])  # without ':' and checksum
    raw[3] = new_type
    checksum = (-sum(raw)) & 0xFF
    return ":" + raw.hex().upper() + f"{checksum:02X}"


def extract_for_mcu(text: str, target_mcu: str) -> str:
    """Return a plain Intel hex holding only ``target_mcu``'s image."""
    wanted = board_ids_for_mcu(target_mcu)
    lines = [line.strip() for line in text.splitlines() if line.strip()]

    # Group lines into blocks. A block starts at its Block Start record;
    # an Extended Linear Address record immediately before it belongs to
    # the new block (each block re-establishes its own address base).
    blocks: list[tuple[int | None, list[str]]] = []
    current_id: int | None = None
    current: list[str] = []
    for line in lines:
        rtype = _record_type(line)
        if rtype == _BLOCK_START:
            carried: list[str] = []
            if current and _record_type(current[-1]) == _EXT_LINEAR_ADDR:
                carried = [current.pop()]
            blocks.append((current_id, current))
            current_id = int(line[9:13], 16)
            current = carried
            continue
        current.append(line)
    blocks.append((current_id, current))

    found = sorted({bid for bid, _ in blocks if bid is not None})
    out: list[str] = []
    for board_id, block in blocks:
        if board_id not in wanted:
            continue
        for line in block:
            rtype = _record_type(line)
            if rtype == _CUSTOM_DATA:
                out.append(_retype(line, _DATA))
            elif rtype in (_BLOCK_END, _PADDED_DATA, _OTHER_DATA, _EOF, None):
                continue
            else:
                out.append(line)

    if not any(_record_type(line) == _DATA for line in out):
        found_text = ", ".join(f"0x{bid:04X}" for bid in found) or "none"
        raise UniversalHexError(
            f"Universal Hex has no image for {target_mcu} "
            f"(board IDs in file: {found_text})"
        )
    out.append(":00000001FF")
    return "\n".join(out) + "\n"


@contextlib.contextmanager
def normalized_hex(hex_path: str, target_mcu: str) -> Iterator[str]:
    """Yield a path pyOCD/intelhex can read for ``target_mcu``.

    A plain Intel hex yields ``hex_path`` itself. A Universal Hex yields a
    temporary file holding just the target's image, removed afterwards.
    An unreadable file also yields ``hex_path`` unchanged, so the caller's
    own validation reports the problem. Raises :class:`UniversalHexError`
    when a Universal Hex has no image for the target.
    """
    try:
        with open(hex_path, encoding="ascii", errors="replace") as f:
            text = f.read()
    except OSError:
        yield hex_path
        return
    if not is_universal_hex(text):
        yield hex_path
        return

    converted = extract_for_mcu(text, target_mcu)
    fd, tmp_path = tempfile.mkstemp(prefix="mbtools-", suffix=".hex")
    try:
        with os.fdopen(fd, "w", encoding="ascii") as f:
            f.write(converted)
        yield tmp_path
    finally:
        with contextlib.suppress(OSError):
            os.unlink(tmp_path)
