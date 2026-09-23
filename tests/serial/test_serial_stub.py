from __future__ import annotations

import pytest

from mbtools.serial import main


def test_mbserial_stub_exits_nonzero_and_names_sprint(capsys):
    with pytest.raises(SystemExit) as excinfo:
        main()
    assert excinfo.value.code != 0
    assert "mbserial" in capsys.readouterr().err
