import asyncio

import pytest
from httpx import AsyncClient

from app.auth import create_user_token
from tests.concurrency.helpers import background_invariant_poller, create_concurrency_show
from tests.invariants import assert_invariants

ITERATIONS = 20


@pytest.mark.concurrency
async def test_per_user_limit_race(client: AsyncClient, db_conn):
    """1 user x 10 parallel reserves of different seats, limit 4.

    Exactly 4 x 201 Created and 6 x 409 per_user_limit. Quota row == 4.
    Runs 20 iterations to hunt race condition flakes.
    """
    for iteration in range(ITERATIONS):
        show_id = await create_concurrency_show(client, seats_count=10, per_user_limit=4)
        user_id = f"single_user_quota_{iteration}"
        token = create_user_token(user_id)

        async def _reserve_single_seat(
            seat_idx: int,
            cur_it=iteration,
            cur_sid=show_id,
            cur_tok=token,
        ):
            return await client.post(
                "/reservations",
                json={
                    "show_id": cur_sid,
                    "seats": [f"C{seat_idx}"],
                    "idempotency_key": f"key-quota-{cur_it}-{seat_idx}",
                },
                headers={"Authorization": f"Bearer {cur_tok}"},
            )

        async with background_invariant_poller(client, show_id):
            tasks = [_reserve_single_seat(i) for i in range(1, 11)]
            responses = await asyncio.gather(*tasks)

        status_counts = {}
        for r in responses:
            status_counts[r.status_code] = status_counts.get(r.status_code, 0) + 1

        assert status_counts.get(500, 0) == 0, f"Iteration {iteration}: 500 status observed"
        assert status_counts.get(201, 0) == 4, (
            f"Iteration {iteration}: Expected 4 winners, got {status_counts.get(201, 0)}"
        )
        assert status_counts.get(409, 0) == 6, (
            f"Iteration {iteration}: Expected 6 declines, got {status_counts.get(409, 0)}"
        )

        for r in responses:
            if r.status_code == 409:
                assert r.json()["error"]["code"] == "per_user_limit"

        # Check quota row directly
        quota_held = await db_conn.fetchval(
            """
            SELECT held FROM user_show_quota WHERE show_id = $1 AND user_id = $2
            """,
            show_id,
            user_id,
        )
        assert quota_held == 4, f"Iteration {iteration}: Expected quota held == 4, got {quota_held}"

        await assert_invariants(db_conn, show_id)
