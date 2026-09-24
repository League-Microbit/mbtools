"""Tests for mbtools.registry.universal_hex -- flashing micro:bit Universal
Hex files (V1 + V2 images in one file, as the official out-of-box
experience firmware ships)."""

from __future__ import annotations

import os

import intelhex
import pytest

from mbtools.registry import flashlogic
from mbtools.registry.universal_hex import (
    UniversalHexError,
    board_ids_for_mcu,
    extract_for_mcu,
    is_universal_hex,
    normalized_hex,
)


def _record(rtype: int, address: int, data: bytes) -> str:
    raw = bytes([len(data), (address >> 8) & 0xFF, address & 0xFF, rtype]) + data
    return ":" + raw.hex().upper() + f"{(-sum(raw)) & 0xFF:02X}"


ELA0 = _record(0x04, 0, b"\x00\x00")
EOF = ":00000001FF"


def _universal(v1_data: bytes, v2_data: bytes) -> str:
    """Section-format Universal Hex: V1 section (0x9900, data type 0x00)
    then V2 section (0x9903, data carried as custom-data type 0x0D)."""
    return "\n".join(
        [
            ELA0,
            _record(0x0A, 0, bytes.fromhex("9900C0DE")),
            _record(0x00, 0x0000, v1_data),
            _record(0x0B, 0, b"\xff" * 4),
            ELA0,
            _record(0x0A, 0, bytes.fromhex("9903C0DE")),
            _record(0x0D, 0x0000, v2_data),
            _record(0x0C, 0x0010, b"\xff" * 4),
            _record(0x0B, 0, b"\xff" * 4),
            EOF,
        ]
    ) + "\n"


V1 = bytes(range(16))
V2 = bytes(range(100, 116))


def _load(text: str) -> intelhex.IntelHex:
    ih = intelhex.IntelHex()
    ih.loadhex(__import__("io").StringIO(text))
    return ih


def test_detects_universal_hex():
    assert is_universal_hex(_universal(V1, V2))
    assert not is_universal_hex("\n".join([ELA0, _record(0x00, 0, V1), EOF]))


def test_intelhex_rejects_the_raw_universal_hex():
    """The bug this module fixes: 'Record ... has invalid record type'."""
    with pytest.raises(intelhex.IntelHexError):
        _load(_universal(V1, V2))


def test_extracts_v2_image_for_nrf52833():
    ih = _load(extract_for_mcu(_universal(V1, V2), "nrf52833"))
    assert ih.tobinstr(0, 15) == V2


def test_extracts_v1_image_for_nrf51():
    ih = _load(extract_for_mcu(_universal(V1, V2), "nrf51822_16"))
    assert ih.tobinstr(0, 15) == V1


def test_output_has_single_eof_and_no_block_records():
    out = extract_for_mcu(_universal(V1, V2), "nrf52833").splitlines()
    types = [line[7:9] for line in out]
    assert types.count("01") == 1 and out[-1] == EOF
    assert not {"0A", "0B", "0C", "0D", "0E"} & set(types)


def test_block_format_interleaved_blocks():
    """Block format: V1 and V2 blocks alternate; each is picked by board ID."""
    text = "\n".join(
        [
            ELA0, _record(0x0A, 0, bytes.fromhex("9900C0DE")), _record(0x00, 0x0000, V1[:8]),
            _record(0x0B, 0, b"\xff" * 4),
            ELA0, _record(0x0A, 0, bytes.fromhex("9903C0DE")), _record(0x0D, 0x0000, V2[:8]),
            _record(0x0B, 0, b"\xff" * 4),
            ELA0, _record(0x0A, 0, bytes.fromhex("9900C0DE")), _record(0x00, 0x0008, V1[8:]),
            _record(0x0B, 0, b"\xff" * 4),
            ELA0, _record(0x0A, 0, bytes.fromhex("9903C0DE")), _record(0x0D, 0x0008, V2[8:]),
            _record(0x0B, 0, b"\xff" * 4),
            EOF,
        ]
    )
    assert _load(extract_for_mcu(text, "nrf52833")).tobinstr(0, 15) == V2


def test_missing_image_for_target_raises():
    text = "\n".join(
        [ELA0, _record(0x0A, 0, bytes.fromhex("9900C0DE")), _record(0x00, 0, V1), EOF]
    )
    with pytest.raises(UniversalHexError, match="0x9900"):
        extract_for_mcu(text, "nrf52833")


def test_unknown_target_raises():
    with pytest.raises(UniversalHexError):
        board_ids_for_mcu("stm32f103")


def test_normalized_hex_passes_plain_hex_through(tmp_path):
    plain = tmp_path / "plain.hex"
    plain.write_text("\n".join([ELA0, _record(0x00, 0, V1), EOF]) + "\n")
    with normalized_hex(str(plain), "nrf52833") as path:
        assert path == str(plain)


def test_normalized_hex_writes_and_removes_temp_file(tmp_path):
    uh = tmp_path / "oob.hex"
    uh.write_text(_universal(V1, V2))
    with normalized_hex(str(uh), "nrf52833") as path:
        assert path != str(uh) and os.path.exists(path)
        ih = intelhex.IntelHex()
        ih.loadhex(path)
        assert ih.tobinstr(0, 15) == V2
    assert not os.path.exists(path)


def test_flashlogic_flashes_the_extracted_image(tmp_path, monkeypatch):
    uh = tmp_path / "oob.hex"
    uh.write_text(_universal(V1, V2))
    seen = {}

    def fake_flash(uid, hex_path, target_mcu, log, board_name, port, timeout):
        ih = intelhex.IntelHex()
        ih.loadhex(hex_path)  # must be a plain, parseable Intel hex
        seen["data"] = ih.tobinstr(0, 15)
        return 0

    monkeypatch.setattr(flashlogic, "_flash_hex", fake_flash)
    logs: list[str] = []
    assert flashlogic.flash_hex("UID", str(uh), "nrf52833", logs.append) == 0
    assert seen["data"] == V2
    assert any("Universal Hex" in line for line in logs)


def test_flashlogic_reports_missing_image(tmp_path):
    uh = tmp_path / "v1only.hex"
    uh.write_text(
        "\n".join([ELA0, _record(0x0A, 0, bytes.fromhex("9900C0DE")), _record(0x00, 0, V1), EOF])
    )
    logs: list[str] = []
    assert flashlogic.flash_hex("UID", str(uh), "nrf52833", logs.append) == 1
    assert any("no image for nrf52833" in line for line in logs)
