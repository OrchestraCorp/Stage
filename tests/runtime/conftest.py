import os

import pytest

from app.core.config import Settings


@pytest.fixture(autouse=True)
def isolated_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    """Runtime tests must never inherit a developer's real database credentials."""

    for name in tuple(os.environ):
        # Preserve the explicit disposable-database reset authorization used by
        # persistence fixtures; it is not an application Settings field.
        if name.startswith("STAGE_") and name != "STAGE_ALLOW_TEST_DATABASE_RESET":
            monkeypatch.delenv(name)
    monkeypatch.setitem(Settings.model_config, "env_file", None)
