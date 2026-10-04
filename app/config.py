from typing import Optional

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    # Database
    database_url: str = "postgresql://postgres:postgres@localhost:5432/seat_reservation"
    test_database_url: Optional[str] = "postgresql://postgres:postgres@localhost:5433/testdb"
    db_pool_min_size: int = 15
    db_pool_max_size: int = 15
    db_semaphore_size: int = 15
    db_semaphore_timeout: float = 25.0
    db_statement_cache_size: int = 100
    db_lock_timeout: str = "8s"
    db_statement_timeout: str = "15s"
    db_idle_in_transaction_session_timeout: str = "15s"
    db_connect_retry_timeout: float = 60.0

    # Application
    port: int = 8000
    host: str = "0.0.0.0"
    environment: str = "production"

    # Security
    jwt_secret: str = "seat-reservation-dev-secret-change-in-prod"
    jwt_algorithm: str = "HS256"
    admin_token: str = "admin-secret-token"

    # Domain defaults
    default_per_user_limit: int = 4

    @property
    def effective_db_url(self) -> str:
        # Normalize postgres:// to postgresql:// if needed for asyncpg
        url = self.database_url
        if url.startswith("postgres://"):
            url = "postgresql://" + url[len("postgres://") :]
        return url


settings = Settings()
