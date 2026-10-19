import pytest

from scripts.check_database import validate_test_database


@pytest.mark.parametrize(
    ("name", "flag"),
    (("production", "1"), ("stage_ci", None), ("company_stage_test", "1"), ("stage_civil", "1")),
)
def test_migration_verification_refuses_unsafe_targets(name: str, flag: str | None) -> None:
    with pytest.raises(ValueError, match="disposable"):
        validate_test_database(f"postgresql+asyncpg://localhost/{name}", flag)


def test_explicit_test_database_is_accepted() -> None:
    validate_test_database("postgresql+asyncpg://localhost/stage_ci", "1")
