import asyncio

import pytest
from httpx import AsyncClient

from app.auth import create_user_token
from tests.concurrency.helpers import background_invariant_poller, create_concurrency_show
from tests.invariants import assert_invariants

ITERATIONS = 20


@pytest.mark.concurrency
async def test_hot_seat_concurrency(client: AsyncClient, db_conn):
    """500 distinct users competing for the same single seat.

    Exactly 1 x 201 Created, 499 x 409 seat_taken, zero 5xx.
    Runs across 20 iterations on fresh shows to hunt flakes.
    """
    for iteration in range(ITERATIONS):
        show_id = await create_concurrency_show(client, seats_count=1)

        async def _attempt_booking(user_idx: int, cur_it=iteration, cur_sid=show_id):
            user_id = f"hot_user_{cur_it}_{user_idx}"
            token = create_user_token(user_id)
            return await client.post(
                "/reservations",
                json={
                    "show_id": cur_sid,
                    "seats": ["C1"],
                    "idempotency_key": f"key-{cur_it}-{user_idx}",
                },
                headers={"Authorization": f"Bearer {token}"},
            )

        async with background_invariant_poller(client, show_id):
            tasks = [_attempt_booking(i) for i in range(500)]
            responses = await asyncio.gather(*tasks)

        status_counts = {}
        for r in responses:
            status_counts[r.status_code] = status_counts.get(r.status_code, 0) + 1

        assert status_counts.get(500, 0) == 0, f"Iteration {iteration}: 500 status observed"
        assert status_counts.get(201, 0) == 1, (
            f"Iteration {iteration}: Expected exactly 1 winner, got {status_counts.get(201, 0)}"
        )
        assert status_counts.get(409, 0) == 499, (
            f"Iteration {iteration}: Expected 499 conflicts, got {status_counts.get(409, 0)}"
        )

        await assert_invariants(db_conn, show_id)


@pytest.mark.concurrency
async def test_hot_set_concurrency(client: AsyncClient, db_conn):
    """2000 users competing for 10 hot seats (each requests 1 random or round-robin seat).

    Exactly 10 winners (one per seat), rest 409. Zero 5xx.
    """
    iterations = 5  # Thorough flake hunt
    for iteration in range(iterations):
        show_id = await create_concurrency_show(client, seats_count=10)

        async def _attempt_booking(user_idx: int, cur_it=iteration, cur_sid=show_id):
            user_id = f"set_user_{cur_it}_{user_idx}"
            chosen_seat = f"C{(user_idx % 10) + 1}"
            token = create_user_token(user_id)
            return await client.post(
                "/reservations",
                json={
                    "show_id": cur_sid,
                    "seats": [chosen_seat],
                    "idempotency_key": f"key-set-{cur_it}-{user_idx}",
                },
                headers={"Authorization": f"Bearer {token}"},
            )

        async with background_invariant_poller(client, show_id):
            tasks = [_attempt_booking(i) for i in range(2000)]
            responses = await asyncio.gather(*tasks)

        status_counts = {}
        for r in responses:
            status_counts[r.status_code] = status_counts.get(r.status_code, 0) + 1

        assert status_counts.get(500, 0) == 0, f"Iteration {iteration}: 500 status observed"
        assert status_counts.get(201, 0) == 10, (
            f"Iteration {iteration}: Expected 10 winners, got {status_counts.get(201, 0)}"
        )
        assert status_counts.get(409, 0) == 1990, (
            f"Iteration {iteration}: Expected 1990 conflicts, got {status_counts.get(409, 0)}"
        )

        await assert_invariants(db_conn, show_id)
