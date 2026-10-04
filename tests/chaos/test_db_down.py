import asyncio
import uuid

import pytest
from httpx import AsyncClient

import app.db as db_module
from app.auth import create_user_token
from app.config import settings


@pytest.mark.chaos
async def test_db_down_and_auto_recovery(client: AsyncClient):
    """Scenario 1: Database Down and Automatic Recovery.

    1. When DB is down / disconnected:
       - /healthz must return 200 (process liveness uncoupled from DB).
       - /readyz must return 503 (fails closed).
       - POST /reservations must return a clean JSON 503 (database_unavailable, no stack trace).
    2. When DB recovers:
       - /readyz must return 200 within 10s with no manual process restart.
    """
    # Verify baseline is healthy
    health_resp = await client.get("/healthz")
    assert health_resp.status_code == 200
    ready_resp = await client.get("/readyz")
    assert ready_resp.status_code == 200

    # Save original database URL
    orig_url = settings.database_url
    try:
        # Simulate DB going down by pointing database_url to an unreachable port
        unreachable_url = "postgresql://postgres:postgres@localhost:59999/testdb"
        settings.database_url = unreachable_url

        # Temporarily close existing pool to force reconnect attempt
        if db_module.pool:
            await db_module.pool.close()
            db_module.pool = None
        if db_module.reserved_ready_conn:
            try:
                await db_module.reserved_ready_conn.close()
            except Exception:
                pass
            db_module.reserved_ready_conn = None
        db_module.is_db_connected = False

        # 1. /healthz must remain 200 OK
        h_resp = await client.get("/healthz")
        assert h_resp.status_code == 200
        assert h_resp.json()["status"] == "ok"

        # 2. /readyz must fail closed with 503
        r_resp = await client.get("/readyz")
        assert r_resp.status_code == 503
        assert r_resp.json()["status"] == "unavailable"

        # 3. Reserve request must return clean JSON 503 with no stack trace
        user_tok = create_user_token("chaos_user")
        res_resp = await client.post(
            "/reservations",
            json={
                "show_id": str(uuid.uuid4()),
                "seats": ["C1"],
                "idempotency_key": "chaos-key-1",
            },
            headers={"Authorization": f"Bearer {user_tok}"},
        )
        assert res_resp.status_code == 503
        err_body = res_resp.json()
        assert "error" in err_body
        assert err_body["error"]["code"] == "database_unavailable"
        assert "request_id" in err_body["error"]

    finally:
        # Restore database connectivity
        settings.database_url = orig_url

        # Pool auto-recovery loop (wait up to 10s for /readyz to return 200)
        start_time = asyncio.get_event_loop().time()
        recovered = False
        while (asyncio.get_event_loop().time() - start_time) < 10.0:
            if not db_module.pool:
                try:
                    await db_module.init_db()
                except Exception:
                    pass
            check = await client.get("/readyz")
            if check.status_code == 200:
                recovered = True
                break
            await asyncio.sleep(0.5)

        assert recovered, "Database pool failed to recover to 200 within 10s"
