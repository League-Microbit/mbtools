from __future__ import annotations

import pytest

from mbtools.deploy import main


def test_mbdeploy_stub_exits_nonzero_and_names_sprint(capsys):
    with pytest.raises(SystemExit) as excinfo:
        main()
    assert excinfo.value.code != 0
    assert "mbdeploy" in capsys.readouterr().err
