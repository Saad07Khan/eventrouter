from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """All configuration comes from environment variables (or a .env file).

    Reading config from the environment (never hard-coded) is what lets the
    same code run on your laptop and in production without edits.
    """

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # SQLAlchemy async URL. The "+asyncpg" part picks the async driver.
    database_url: str = "postgresql+asyncpg://localhost/event_pipeline"

    # Cloud Postgres (Neon, RDS, ...) requires SSL; a local/dev Postgres in
    # Docker usually has no TLS at all and refuses the connection if we insist.
    # Set DATABASE_SSL=false for local Postgres.
    database_ssl: bool = True

    # --- Worker tuning (env-overridable; tests use fast values) ---
    retry_base_seconds: float = 10.0      # first retry delay; doubles each attempt
    retry_cap_seconds: float = 3600.0     # never wait longer than this between attempts
    retry_max_attempts: int = 8           # after this many failed attempts -> dead
    poll_interval_seconds: float = 5.0    # how long to sleep when there's no work
    claim_batch: int = 50                 # max deliveries claimed per loop
    claim_timeout_seconds: float = 60.0   # a 'delivering' row older than this is reclaimed

    # The /demo page writes real sources, destinations and events, so a public
    # deploy hands anyone a button that fills the database. Set DEMO_ENABLED=false
    # to serve a 404 instead.
    demo_enabled: bool = True

    # --- Backpressure (Step 7) ---
    worker_concurrency: int = 16          # max deliveries in flight at once (global)
    destination_concurrency: int = 5      # max in flight to any ONE destination
    circuit_fail_threshold: int = 10      # consecutive failures before a circuit opens
    circuit_cooldown_seconds: float = 300.0  # how long a destination stays "cold"


settings = Settings()
