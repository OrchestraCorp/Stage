from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="STAGE_", env_file=".env", extra="ignore")

    env: str = "dev"
    app_name: str = "orchestra-stage"
    database_url: str = "mysql+aiomysql://stage:stage@127.0.0.1:3306/stage"
    log_level: str = "INFO"


settings = Settings()
