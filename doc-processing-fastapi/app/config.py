"""Settings, read from environment variables (or a local .env file). See .env.example."""

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    database_url: str = "sqlite:///./data/app.db"
    redis_url: str = "redis://localhost:6379/0"
    queue_name: str = "docproc:jobs"

    upload_dir: str = "./data/uploads"
    max_upload_size: int = 10 * 1024 * 1024  # bytes

    job_max_attempts: int = 3
    job_retry_backoff_seconds: float = 5  # 5s, 10s, 20s ...
    job_stale_after_seconds: float = 300

    worker_poll_timeout_seconds: int = 2
    worker_sweep_interval_seconds: float = 30

    # Dotted path to an app.processing.DocumentProcessor implementation.
    document_processor: str = "app.processing.LocalDocumentProcessor"
    ocr_language: str = "eng"

    log_level: str = "INFO"


settings = Settings()
