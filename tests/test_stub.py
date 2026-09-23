from __future__ import annotations

from mbtools._stub import stub_main


def test_stub_main_returns_nonzero_status():
    assert stub_main("mbdeploy", "sprint 002") == 1


def test_stub_main_prints_program_and_location_to_stderr(capsys):
    stub_main("mbrelay", "sprint 004")
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "mbrelay" in captured.err
    assert "sprint 004" in captured.err
    assert "not yet implemented" in captured.err
