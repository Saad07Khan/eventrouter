from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """All configuration comes from environment variables (or a .env file).

    Reading config from the environment (never hard-coded) is what lets the
    same code run on your laptop and in production without edits.
    """

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # SQLAlchemy async URL. The "+asyncpg" part picks the async driver.
    database_url: str = "postgresql+asyncpg://localhost/event_pipeline"


settings = Settings()
