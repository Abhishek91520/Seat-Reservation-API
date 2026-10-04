import os

import asyncpg
import pytest
from httpx import ASGITransport, AsyncClient

from app.config import settings
from app.db import close_db, init_db, run_migrations
from app.main import app

# Ensure test DB is targeted
TEST_DB_URL = os.environ.get(
    "TEST_DATABASE_URL",
    "postgresql://postgres:postgres@localhost:5433/testdb",
)
settings.database_url = TEST_DB_URL
settings.db_statement_cache_size = int(os.environ.get("DB_STATEMENT_CACHE_SIZE", "100"))


@pytest.fixture(scope="session", autouse=True)
def setup_test_env():
    settings.database_url = TEST_DB_URL
    os.environ["DATABASE_URL"] = TEST_DB_URL
    os.environ["JWT_SECRET"] = "test-jwt-secret-key-32-characters"
    os.environ["ADMIN_TOKEN"] = "test-admin-secret-token"
    settings.jwt_secret = "test-jwt-secret-key-32-characters"
    settings.admin_token = "test-admin-secret-token"


@pytest.fixture(scope="session")
async def db_pool():
    # Setup test database and run migrations
    p = await asyncpg.create_pool(
        dsn=TEST_DB_URL,
        min_size=2,
        max_size=10,
        statement_cache_size=settings.db_statement_cache_size,
    )
    async with p.acquire() as conn:
        await run_migrations(conn)
    yield p
    await p.close()


@pytest.fixture(autouse=True)
async def manage_app_lifecycle():
    await init_db()
    yield
    await close_db()


@pytest.fixture
async def client():
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c
