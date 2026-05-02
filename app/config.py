from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    DATABASE_URL: str = "postgresql+asyncpg://cueapi:cueapi@localhost:5432/cueapi"
    DATABASE_POOL_SIZE: int = 5
    DATABASE_MAX_OVERFLOW: int = 5
    REDIS_URL: str = "redis://localhost:6379/0"
    HOST: str = "0.0.0.0"
    PORT: int = 8000
    ENV: str = "development"
    WEBHOOK_TIMEOUT_SECONDS: int = 30
    POLLER_INTERVAL_SECONDS: int = 5
    POLLER_BATCH_SIZE: int = 500
    WEBHOOK_CONCURRENCY_PER_WORKER: int = 50
    EXECUTION_STALE_AFTER_SECONDS: int = 300
    # Per-user concurrent delivery cap shared across cue webhooks +
    # messaging push deliveries. Prevents one user with thousands of
    # subscribers from monopolizing the worker pool.
    MAX_CONCURRENT_DELIVERIES_PER_USER: int = 50
    # Messaging push delivery: how long a "delivering" message can sit
    # before stale-recovery transitions it back to retry_ready.
    MESSAGE_DELIVERY_STALE_AFTER_SECONDS: int = 300
    BASE_URL: str = "http://localhost:8000"
    ALLOW_REGISTER: bool = True
    RESEND_API_KEY: str = ""
    RESEND_FROM_EMAIL: str = "CueAPI <noreply@cueapi.ai>"
    WORKER_HEARTBEAT_TIMEOUT_SECONDS: int = 180
    WORKER_CLAIM_TIMEOUT_SECONDS: int = 900
    WORKER_UNCLAIMED_TIMEOUT_SECONDS: int = 900
    POLLER_HEARTBEAT_TTL_SECONDS: int = 120
    POLLER_LEADER_LOCK_TTL_SECONDS: int = 30
    SESSION_SECRET: str = ""

    @property
    def async_database_url(self) -> str:
        """Convert postgresql:// to postgresql+asyncpg://."""
        url = self.DATABASE_URL
        if url.startswith("postgresql://"):
            url = url.replace("postgresql://", "postgresql+asyncpg://", 1)
        return url

    model_config = {"env_file": ".env", "env_file_encoding": "utf-8"}


settings = Settings()
