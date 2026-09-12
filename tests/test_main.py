import runpy
from unittest.mock import Mock

import pytest

from pagewatcher import cli


def test_module_entry_point_exits_with_cli_status(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    main = Mock(return_value=17)
    monkeypatch.setattr(cli, "main", main)

    with pytest.raises(SystemExit) as raised:
        runpy.run_module("pagewatcher.__main__", run_name="__main__")

    assert raised.value.code == 17
    main.assert_called_once_with()
