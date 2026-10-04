import asyncio
import os
import random

import pytest
from httpx import AsyncClient

from app.auth import create_user_token
from tests.concurrency.helpers import background_invariant_poller, create_concurrency_show
from tests.invariants import assert_invariants

# Allow overriding stampede count via env for quick dev runs vs full verification
TOTAL_STAMPEDE_REQUESTS = int(os.environ.get("STAMPEDE_REQUESTS", "20000"))


@pytest.mark.concurrency
async def test_stampede_concurrency(client: AsyncClient, db_conn):
    """Stampede of 20,000 requests through client semaphore 500.

    1,000 seats, Zipf-weighted choice, 20% same-key retries.
    Assert zero 5xx, invariants hold, confirmed seats == sum of 201s' seat counts.
    """
    show_id = await create_concurrency_show(client, seats_count=1000, per_user_limit=4)
    seat_labels = [f"C{i}" for i in range(1, 1001)]

    # Zipf weights (a = 1.3)
    weights = [1.0 / (i**1.3) for i in range(1, 1001)]

    client_semaphore = asyncio.Semaphore(500)
    confirmed_201_seats = 0

    # Pre-generate requests
    requests_data = []
    prev_req = None

    for i in range(TOTAL_STAMPEDE_REQUESTS):
        # 20% same-key retries
        if prev_req and (i % 5 == 0):
            requests_data.append(prev_req)
        else:
            user_id = f"stampede_user_{i}"
            seat = random.choices(seat_labels, weights=weights, k=1)[0]
            req = (user_id, [seat], f"stampede-key-{i}")
            requests_data.append(req)
            prev_req = req

    async def _send_request(user_id: str, seats: list, key: str):
        async with client_semaphore:
            token = create_user_token(user_id)
            return await client.post(
                "/reservations",
                json={
                    "show_id": show_id,
                    "seats": seats,
                    "idempotency_key": key,
                },
                headers={"Authorization": f"Bearer {token}"},
            )

    async with background_invariant_poller(client, show_id, poll_interval_ms=100):
        tasks = [_send_request(u, s, k) for u, s, k in requests_data]
        responses = await asyncio.gather(*tasks)

    status_counts = {}
    for r in responses:
        status_counts[r.status_code] = status_counts.get(r.status_code, 0) + 1
        if r.status_code == 201:
            confirmed_201_seats += len(r.json()["seats"])

    assert status_counts.get(500, 0) == 0, f"500 errors observed: {status_counts}"
    assert status_counts.get(201, 0) > 0, "Expected some winners"

    # Verify confirmed seats == sum of 201s' seat counts
    state_resp = await client.get(f"/shows/{show_id}")
    state = state_resp.json()
    assert state["confirmed"] == confirmed_201_seats, (
        f"Mismatch: show.confirmed={state['confirmed']}, 201_seats={confirmed_201_seats}"
    )

    await assert_invariants(db_conn, show_id)
