import time

import pytest
from httpx import AsyncClient

import app.db as db_module


@pytest.mark.chaos
async def test_cold_start_time_to_readyz(client: AsyncClient):
    """Scenario 4: Cold start measurement.

    Measures elapsed time from process startup (/healthz responsive)
    to database readiness (/readyz responsive).
    """
    # Simulate fresh cold start by re-initializing database pool
    if db_module.pool:
        await db_module.pool.close()
        db_module.pool = None
    if db_module.reserved_ready_conn:
        try:
            await db_module.reserved_ready_conn.close()
        except Exception:
            pass
        db_module.reserved_ready_conn = None

    t_start = time.perf_counter()

    # 1. Healthz is instantly green
    h_resp = await client.get("/healthz")
    assert h_resp.status_code == 200
    t_healthz = time.perf_counter()

    # 2. Reinitialize database
    await db_module.init_db()

    # 3. Readyz is green
    r_resp = await client.get("/readyz")
    assert r_resp.status_code == 200
    t_readyz = time.perf_counter()

    time_to_healthz_ms = (t_healthz - t_start) * 1000
    time_to_readyz_ms = (t_readyz - t_start) * 1000

    assert time_to_healthz_ms < 500, f"Healthz took too long: {time_to_healthz_ms}ms"
    assert time_to_readyz_ms < 2000, f"Readyz took too long: {time_to_readyz_ms}ms"
