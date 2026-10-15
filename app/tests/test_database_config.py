from core.config import Settings


def test_migration_database_url_is_independent_and_uses_asyncpg() -> None:
    settings = Settings(
        _env_file=None,  # type: ignore[call-arg]
        database_url="postgresql://runtime:runtime@db:5432/knowbase",
        migration_database_url="postgresql://migrator:migrator@db:5432/knowbase",
    )

    assert settings.database_url.startswith("postgresql+asyncpg://runtime:")
    assert settings.migration_database_url is not None
    assert settings.migration_database_url.startswith("postgresql+asyncpg://migrator:")
