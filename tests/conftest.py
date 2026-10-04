import os

import pytest
from httpx import ASGITransport, AsyncClient

from app.config import settings
from app.db import close_db, db_connection, init_db
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


@pytest.fixture
async def db_conn():
    async with db_connection() as conn:
        yield conn


@pytest.fixture
def db_pool():
    import app.db as db_module

    return db_module.pool


@pytest.fixture(autouse=True)
async def manage_app_lifecycle():
    await init_db()
    async with db_connection() as conn:
        await conn.execute(
            "TRUNCATE shows, seats, reservations, user_show_quota, idempotency_keys CASCADE;"
        )
    yield
    await close_db()


@pytest.fixture
async def client():
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c
