import asyncio

import pytest
from httpx import AsyncClient

from app.auth import create_user_token
from tests.concurrency.helpers import background_invariant_poller, create_concurrency_show
from tests.invariants import assert_invariants

ITERATIONS = 5


@pytest.mark.concurrency
async def test_multi_seat_opposite_order_deadlock_free(client: AsyncClient, db_conn):
    """Users A requests [S1,S2] while B requests [S2,S1], 200 pairs concurrently.

    Verifies deadlock freedom (0 x 40P01, 0 x 5xx) and atomic all-or-nothing consistency.
    """
    for iteration in range(ITERATIONS):
        # 200 distinct pairs -> 400 seats total
        show_id = await create_concurrency_show(client, seats_count=400, per_user_limit=4)

        async def _book_pair(
            pair_idx: int,
            reverse: bool,
            cur_it=iteration,
            cur_sid=show_id,
        ):
            user_suffix = "b" if reverse else "a"
            user_id = f"pair_user_{cur_it}_{pair_idx}_{user_suffix}"
            token = create_user_token(user_id)
            s1 = f"C{pair_idx * 2 - 1}"
            s2 = f"C{pair_idx * 2}"
            requested_seats = [s2, s1] if reverse else [s1, s2]

            resp = await client.post(
                "/reservations",
                json={
                    "show_id": cur_sid,
                    "seats": requested_seats,
                    "idempotency_key": f"key-pair-{cur_it}-{pair_idx}-{user_suffix}",
                },
                headers={"Authorization": f"Bearer {token}"},
            )
            return pair_idx, reverse, resp.status_code, resp.json()

        tasks = []
        for i in range(1, 201):
            tasks.append(_book_pair(i, reverse=False))
            tasks.append(_book_pair(i, reverse=True))

        async with background_invariant_poller(client, show_id):
            results = await asyncio.gather(*tasks)

        # Group by pair index
        pair_outcomes = {}
        for pair_idx, reverse, status_code, body in results:
            assert status_code != 500, f"500 internal error encountered for pair {pair_idx}"
            pair_outcomes.setdefault(pair_idx, []).append((reverse, status_code, body))

        # Check all-or-nothing per pair
        for pair_idx, outcomes in pair_outcomes.items():
            assert len(outcomes) == 2, f"Expected 2 attempts for pair {pair_idx}"
            statuses = [o[1] for o in outcomes]
            assert 201 in statuses, f"Pair {pair_idx} had no winner: {statuses}"
            assert 409 in statuses, f"Pair {pair_idx} did not have a conflict: {statuses}"
            assert statuses.count(201) == 1, (
                f"Pair {pair_idx} had multiple winners! Both got seats: {statuses}"
            )

        await assert_invariants(db_conn, show_id)
