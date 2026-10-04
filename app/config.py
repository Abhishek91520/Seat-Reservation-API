from typing import Optional
from urllib.parse import urlparse

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
    db_pool_size: Optional[int] = None
    db_pool_min_size: int = 15
    db_pool_max_size: int = 15
    db_semaphore_size: Optional[int] = None
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
    show_state_cache_ms: int = 500

    @property
    def effective_db_url(self) -> str:
        # Normalize postgres:// to postgresql:// if needed for asyncpg
        url = self.database_url
        if url.startswith("postgres://"):
            url = "postgresql://" + url[len("postgres://") :]
        # Auto-normalize IPv6-only direct Supabase host to IPv4 Supavisor pooler
        # to prevent Linux '[Errno 101] Network is unreachable' on IPv4 platforms (Render)
        direct_host = "db.ucqugbwkvwurxbtwlrmi.supabase.co"
        pooler_host = "aws-0-ap-northeast-2.pooler.supabase.com:6543"
        if direct_host in url:
            url = url.replace(f"{direct_host}:5432", pooler_host)
            url = url.replace(direct_host, pooler_host)
            if "postgres.ucqugbwkvwurxbtwlrmi" not in url and "postgres:" in url:
                url = url.replace("postgres:", "postgres.ucqugbwkvwurxbtwlrmi:", 1)
            if "sslmode=require" not in url and "ssl=" not in url:
                separator = "&" if "?" in url else "?"
                url = f"{url}{separator}sslmode=require"
        return url

    @property
    def effective_pool_size(self) -> int:
        if self.db_pool_size is not None:
            return self.db_pool_size
        return self.db_pool_max_size

    @property
    def effective_semaphore_size(self) -> int:
        if self.db_semaphore_size is not None:
            return self.db_semaphore_size
        return self.effective_pool_size

    @property
    def pooler_mode(self) -> str:
        try:
            parsed = urlparse(self.effective_db_url)
            port = parsed.port or 5432
            hostname = parsed.hostname or ""
            if port == 6543:
                return "supavisor_transaction_mode"
            if port == 6432:
                return "pgbouncer_transaction_mode"
            if "supabase" in hostname:
                return "supavisor_session_mode"
            return "direct_postgres"
        except Exception:
            return "direct_postgres"

    @property
    def effective_statement_cache_size(self) -> int:
        if self.pooler_mode in ("supavisor_transaction_mode", "pgbouncer_transaction_mode"):
            return 0
        return self.db_statement_cache_size

    @property
    def ssl_required(self) -> bool:
        try:
            parsed = urlparse(self.effective_db_url)
            hostname = parsed.hostname or ""
            query = parsed.query or ""
            if "sslmode=require" in query or "ssl=require" in query:
                return True
            if "supabase.co" in hostname or "supabase.com" in hostname:
                return True
            return False
        except Exception:
            return False


settings = Settings()
