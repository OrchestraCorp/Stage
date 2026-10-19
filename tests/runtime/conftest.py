import os

import pytest

from app.core.config import Settings


@pytest.fixture(autouse=True)
def isolated_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    """Runtime tests must never inherit a developer's real database credentials."""

    for name in tuple(os.environ):
        if name.startswith("STAGE_"):
            monkeypatch.delenv(name)
    monkeypatch.setitem(Settings.model_config, "env_file", None)
