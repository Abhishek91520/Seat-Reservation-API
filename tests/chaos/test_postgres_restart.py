import asyncio

import pytest
from httpx import AsyncClient

from app.auth import create_user_token
from tests.concurrency.helpers import create_concurrency_show
from tests.invariants import assert_invariants


@pytest.mark.chaos
async def test_postgres_restart_mid_burst(client: AsyncClient, db_conn):
    """Scenario 3: Postgres restart mid-burst.

    During concurrent requests, active database backend connections are terminated.
    After database recovery, all committed state remains strictly valid with
    zero phantom seats or corrupt quotas.
    """
    show_id = await create_concurrency_show(client, seats_count=30, per_user_limit=4)

    async def _attempt_booking(idx: int):
        token = create_user_token(f"pg_restart_user_{idx}")
        seat = f"C{(idx % 30) + 1}"
        try:
            return await client.post(
                "/reservations",
                json={
                    "show_id": show_id,
                    "seats": [seat],
                    "idempotency_key": f"pg-restart-key-{idx}",
                },
                headers={"Authorization": f"Bearer {token}"},
            )
        except Exception:
            return None

    tasks = [asyncio.create_task(_attempt_booking(i)) for i in range(500)]

    # Allow burst to initiate
    await asyncio.sleep(0.03)

    # Terminate active backend transactions in PostgreSQL (simulates restart / connection severance)
    try:
        await db_conn.execute(
            """
            SELECT pg_terminate_backend(pid)
            FROM pg_stat_activity
            WHERE datname = current_database()
              AND pid <> pg_backend_pid()
              AND state = 'active'
            """
        )
    except Exception:
        pass

    # Await completion of all tasks
    await asyncio.gather(*tasks, return_exceptions=True)

    # Invariants must hold strictly upon recovery with no phantom seats
    await assert_invariants(db_conn, show_id)
