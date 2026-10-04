import asyncio

import pytest
from httpx import AsyncClient

from app.auth import create_user_token
from tests.concurrency.helpers import create_concurrency_show
from tests.invariants import assert_invariants


@pytest.mark.chaos
async def test_app_kill_mid_burst(client: AsyncClient, db_conn):
    """Scenario 2: App killed mid-burst.

    Fires a burst of concurrent reservation requests. Mid-flight, the burst is aborted
    abruptly (simulating SIGKILL / container crash).
    Upon inspection, committed state must be 100% consistent:
    a transaction either completely committed or completely rolled back.
    No partial seat allocations, no dangling reservations.
    """
    show_id = await create_concurrency_show(client, seats_count=50, per_user_limit=4)

    # Launch 1,000 reservation tasks
    async def _reserve_worker(idx: int):
        token = create_user_token(f"kill_user_{idx}")
        seat = f"C{(idx % 50) + 1}"
        return await client.post(
            "/reservations",
            json={
                "show_id": show_id,
                "seats": [seat],
                "idempotency_key": f"kill-key-{idx}",
            },
            headers={"Authorization": f"Bearer {token}"},
        )

    tasks = [asyncio.create_task(_reserve_worker(i)) for i in range(1000)]

    # Let a subset of requests begin executing, then cancel all in-flight tasks abruptly
    await asyncio.sleep(0.05)
    for t in tasks:
        if not t.done():
            t.cancel()

    # Wait for cancellation to settle
    await asyncio.gather(*tasks, return_exceptions=True)

    # Invariants must hold strictly without error
    await assert_invariants(db_conn, show_id)
