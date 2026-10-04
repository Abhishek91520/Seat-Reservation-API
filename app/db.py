import asyncio
import os
import random
from contextlib import asynccontextmanager
from typing import AsyncGenerator, Awaitable, Callable, Optional, TypeVar

import asyncpg
import structlog

from app.config import settings
from app.errors import DatabaseUnavailableException, OverloadedException
from app.metrics import db_pool_in_use, db_retries_total, db_semaphore_waiting

logger = structlog.get_logger(__name__)

T = TypeVar("T")

MIGRATION_ADVISORY_LOCK_ID = 714294821

# Global state
pool: Optional[asyncpg.Pool] = None
reserved_ready_conn: Optional[asyncpg.Connection] = None
db_semaphore: Optional[asyncio.Semaphore] = None
is_db_connected: bool = False


async def get_reserved_ready_conn() -> asyncpg.Connection:
    global reserved_ready_conn
    if reserved_ready_conn is None or reserved_ready_conn.is_closed():
        kwargs = {
            "statement_cache_size": settings.effective_statement_cache_size,
            "timeout": 5.0,
        }
        if settings.ssl_required:
            kwargs["ssl"] = "require"
        reserved_ready_conn = await asyncpg.connect(
            settings.effective_db_url,
            **kwargs,
        )
    return reserved_ready_conn


async def run_migrations(conn: asyncpg.Connection) -> None:
    migration_path = os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        "migrations",
        "001_init.sql",
    )
    if not os.path.exists(migration_path):
        logger.warning("migration_file_not_found", path=migration_path)
        return

    with open(migration_path, "r", encoding="utf-8") as f:
        sql = f.read()

    async with conn.transaction():
        await conn.execute(f"SELECT pg_advisory_xact_lock({MIGRATION_ADVISORY_LOCK_ID})")
        await conn.execute(sql)
    logger.info("migrations_applied_successfully")


async def connect_with_backoff(max_wait_seconds: float = 60.0) -> asyncpg.Pool:
    start_time = asyncio.get_event_loop().time()
    backoff = 0.5
    last_error: Optional[Exception] = None

    pool_max = settings.effective_pool_size
    if settings.ssl_required:
        # Cap pool size to 6 for Supabase free-tier session mode (hard 15 connection cap)
        # allowing 2 containers during rolling deploys (6 + 6 + 2 = 14 <= 15)
        pool_max = min(pool_max, 6)
        pool_min = min(1, pool_max)
    else:
        pool_min = min(settings.db_pool_min_size, pool_max)

    pool_kwargs = {
        "min_size": pool_min,
        "max_size": pool_max,
        "statement_cache_size": settings.effective_statement_cache_size,
        "command_timeout": 15.0,
    }
    if settings.ssl_required:
        pool_kwargs["ssl"] = "require"

    while (asyncio.get_event_loop().time() - start_time) < max_wait_seconds:
        candidate_pool: Optional[asyncpg.Pool] = None
        try:
            candidate_pool = await asyncpg.create_pool(
                dsn=settings.effective_db_url,
                **pool_kwargs,
            )
            # Test connection
            async with candidate_pool.acquire() as conn:
                await conn.fetchval("SELECT 1")
            return candidate_pool
        except Exception as e:
            if candidate_pool is not None:
                try:
                    await asyncio.wait_for(candidate_pool.close(), timeout=1.0)
                except Exception:
                    pass
            last_error = e
            logger.warning(
                "db_connect_failed_retrying",
                error=str(e),
                backoff=backoff,
            )
            await asyncio.sleep(backoff)
            backoff = min(backoff * 1.5, 5.0)

    raise DatabaseUnavailableException(
        f"Failed to connect to database within {max_wait_seconds}s: {last_error}"
    )


async def init_db() -> None:
    global pool, db_semaphore, is_db_connected
    effective_max = settings.effective_pool_size
    if settings.ssl_required:
        effective_max = min(effective_max, 6)
    sem_size = min(settings.effective_semaphore_size, effective_max)
    db_semaphore = asyncio.Semaphore(sem_size)
    try:
        pool = await connect_with_backoff(max_wait_seconds=settings.db_connect_retry_timeout)
        is_db_connected = True
        logger.info(
            "db_pool_initialized",
            pool_size=effective_max,
            semaphore_size=sem_size,
            pooler_mode=settings.pooler_mode,
            statement_cache_size=settings.effective_statement_cache_size,
            ssl_required=settings.ssl_required,
        )
        async with pool.acquire() as conn:
            await run_migrations(conn)
        # Initialize reserved connection
        try:
            await get_reserved_ready_conn()
        except Exception as e:
            logger.warning("reserved_ready_conn_init_failed", error=str(e))
    except Exception as e:
        is_db_connected = False
        logger.error("db_init_failed", error=str(e))
        # Keep process up so /healthz can report 200 while /readyz reports 503


async def close_db() -> None:
    global pool, reserved_ready_conn, is_db_connected
    if reserved_ready_conn and not reserved_ready_conn.is_closed():
        try:
            await reserved_ready_conn.close()
        except Exception:
            pass
        reserved_ready_conn = None

    if pool is not None:
        await pool.close()
        pool = None
    is_db_connected = False
    logger.info("db_pool_closed")


async def check_db_ready() -> bool:
    global pool, db_semaphore, is_db_connected
    if pool is None or getattr(pool, "_closed", False):
        try:
            pool = await connect_with_backoff(max_wait_seconds=3.0)
            is_db_connected = True
            if db_semaphore is None:
                effective_max = settings.effective_pool_size
                if settings.ssl_required:
                    effective_max = min(effective_max, 6)
                sem_size = min(settings.effective_semaphore_size, effective_max)
                db_semaphore = asyncio.Semaphore(sem_size)
            async with pool.acquire() as conn:
                await run_migrations(conn)
        except Exception:
            return False
    try:
        conn = await get_reserved_ready_conn()
        res = await asyncio.wait_for(conn.fetchval("SELECT 1"), timeout=1.0)
        return res == 1
    except Exception as e:
        logger.warning("readyz_check_failed", error=str(e))
        # Fallback to borrowing an idle connection from pool if reserved conn slot was unavailable
        if pool is not None and not getattr(pool, "_closed", False):
            try:
                async with pool.acquire(timeout=0.5) as pool_conn:
                    return await pool_conn.fetchval("SELECT 1") == 1
            except Exception:
                pass
        return False


@asynccontextmanager
async def db_connection() -> AsyncGenerator[asyncpg.Connection, None]:
    if pool is None or db_semaphore is None:
        raise DatabaseUnavailableException("Database pool is not initialized")

    db_semaphore_waiting.inc()
    try:
        await asyncio.wait_for(db_semaphore.acquire(), timeout=settings.db_semaphore_timeout)
    except asyncio.TimeoutError:
        raise OverloadedException(retry_after=2) from None
    finally:
        db_semaphore_waiting.dec()

    try:
        db_pool_in_use.inc()
        async with pool.acquire() as conn:
            yield conn
    finally:
        db_pool_in_use.dec()
        db_semaphore.release()


async def execute_in_transaction_with_retry(
    operation: Callable[[asyncpg.Connection], Awaitable[T]],
    max_retries: int = 3,
) -> T:
    last_error: Optional[Exception] = None

    for attempt in range(max_retries + 1):
        try:
            async with db_connection() as conn:
                async with conn.transaction():
                    await conn.execute(f"SET LOCAL lock_timeout='{settings.db_lock_timeout}';")
                    await conn.execute(
                        f"SET LOCAL statement_timeout='{settings.db_statement_timeout}';"
                    )
                    timeout_val = settings.db_idle_in_transaction_session_timeout
                    await conn.execute(
                        f"SET LOCAL idle_in_transaction_session_timeout='{timeout_val}';"
                    )
                    return await operation(conn)
        except (asyncpg.DeadlockDetectedError, asyncpg.SerializationError) as e:
            sqlstate = getattr(e, "sqlstate", "unknown")
            db_retries_total.labels(sqlstate=sqlstate).inc()
            last_error = e
            if attempt < max_retries:
                jitter = random.uniform(0.005, 0.025) * (2**attempt)
                logger.warning(
                    "transient_db_error_retrying",
                    attempt=attempt,
                    sqlstate=sqlstate,
                    jitter=jitter,
                )
                await asyncio.sleep(jitter)
                continue
            raise
        except Exception:
            raise

    if last_error:
        raise last_error
    raise DatabaseUnavailableException("Transaction failed without explicit exception")
