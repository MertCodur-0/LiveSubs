import re

import livesubs


def test_version_is_pep440_like() -> None:
    assert re.fullmatch(r"\d+\.\d+\.\d+(\.dev\d+)?", livesubs.__version__)


def test_app_module_imports() -> None:
    from livesubs import app

    assert callable(app.main)
