import asyncio

import pytest
from httpx import AsyncClient

import app.db as db_module
from app.auth import create_user_token
from tests.concurrency.helpers import background_invariant_poller, create_concurrency_show
from tests.invariants import assert_invariants


@pytest.mark.concurrency
async def test_pool_exhaustion_queuing(client: AsyncClient, db_conn):
    """Run with semaphore=2 and pool constrained, then fire 1000 concurrent requests.

    Assert zero 5xx, all requests queue without error, and rate of 429 is 0 under 25s wait.
    """
    orig_semaphore = db_module.db_semaphore
    # Artificially constrain concurrency to 2 to test deep queuing behavior
    db_module.db_semaphore = asyncio.Semaphore(2)

    try:
        show_id = await create_concurrency_show(client, seats_count=50)

        async def _request_seat(idx: int):
            user_id = f"queue_user_{idx}"
            token = create_user_token(user_id)
            # Pick a seat round-robin among 50
            seat_lbl = f"C{(idx % 50) + 1}"
            return await client.post(
                "/reservations",
                json={
                    "show_id": show_id,
                    "seats": [seat_lbl],
                    "idempotency_key": f"q-key-{idx}",
                },
                headers={"Authorization": f"Bearer {token}"},
            )

        async with background_invariant_poller(client, show_id, poll_interval_ms=100):
            tasks = [_request_seat(i) for i in range(1000)]
            responses = await asyncio.gather(*tasks)

        status_counts = {}
        for r in responses:
            status_counts[r.status_code] = status_counts.get(r.status_code, 0) + 1

        assert status_counts.get(500, 0) == 0, f"Observed 500 responses: {status_counts}"
        assert status_counts.get(429, 0) == 0, (
            f"Observed 429 overloaded under 25s queue: {status_counts}"
        )
        assert status_counts.get(201, 0) == 50, (
            f"Expected exactly 50 winners, got {status_counts.get(201, 0)}"
        )
        assert status_counts.get(409, 0) == 950, (
            f"Expected 950 declines, got {status_counts.get(409, 0)}"
        )

        await assert_invariants(db_conn, show_id)
    finally:
        db_module.db_semaphore = orig_semaphore
