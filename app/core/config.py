from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="STAGE_", env_file=".env", extra="ignore")

    env: str = "dev"
    app_name: str = "orchestra-stage"
    database_url: str = "sqlite+aiosqlite:///./stage.db"
    log_level: str = "INFO"


settings = Settings()
