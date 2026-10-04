import asyncio

import pytest
from httpx import AsyncClient

from app.auth import create_user_token
from tests.concurrency.helpers import background_invariant_poller, create_concurrency_show
from tests.invariants import assert_invariants

ITERATIONS = 5


@pytest.mark.concurrency
async def test_cancel_rebook_race(client: AsyncClient, db_conn):
    """1 holder cancels while 100 users try to book that seat concurrently.

    At most 1 winner among contenders, 0 double-bookings, invariants hold.
    """
    for iteration in range(ITERATIONS):
        show_id = await create_concurrency_show(client, seats_count=1)
        holder_token = create_user_token(f"holder_{iteration}")

        # Holder initially books C1
        init_res = await client.post(
            "/reservations",
            json={
                "show_id": show_id,
                "seats": ["C1"],
                "idempotency_key": f"holder-key-{iteration}",
            },
            headers={"Authorization": f"Bearer {holder_token}"},
        )
        assert init_res.status_code == 201, (
            f"Failed init res: {init_res.status_code}: {init_res.text}"
        )
        res_id = init_res.json()["id"]

        async def _cancel_holder(cur_rid=res_id, cur_tok=holder_token):
            return await client.post(
                f"/reservations/{cur_rid}/cancel",
                headers={"Authorization": f"Bearer {cur_tok}"},
            )

        async def _contender_book(idx: int, cur_sid=show_id, cur_it=iteration):
            c_token = create_user_token(f"contender_{cur_it}_{idx}")
            return await client.post(
                "/reservations",
                json={
                    "show_id": cur_sid,
                    "seats": ["C1"],
                    "idempotency_key": f"k-contender-{cur_it}-{idx}",
                },
                headers={"Authorization": f"Bearer {c_token}"},
            )

        async with background_invariant_poller(client, show_id):
            tasks = [_cancel_holder()] + [_contender_book(i) for i in range(100)]
            responses = await asyncio.gather(*tasks)

        cancel_resp = responses[0]
        assert cancel_resp.status_code == 200

        contender_resps = responses[1:]
        winner_count = sum(1 for r in contender_resps if r.status_code == 201)
        assert winner_count <= 1, f"Expected at most 1 winner, got {winner_count}"

        for r in contender_resps:
            assert r.status_code in (201, 409), f"Unexpected status {r.status_code}"

        await assert_invariants(db_conn, show_id)


@pytest.mark.concurrency
async def test_parallel_double_cancel(client: AsyncClient, db_conn):
    """Parallel cancel of the same reservation: one real release, no quota underflow."""
    for iteration in range(ITERATIONS):
        show_id = await create_concurrency_show(client, seats_count=3, per_user_limit=4)
        user_id = f"double_cancel_user_{iteration}"
        token = create_user_token(user_id)

        # Book 3 seats
        res = await client.post(
            "/reservations",
            json={
                "show_id": show_id,
                "seats": ["C1", "C2", "C3"],
                "idempotency_key": f"book-3-{iteration}",
            },
            headers={"Authorization": f"Bearer {token}"},
        )
        assert res.status_code == 201
        res_id = res.json()["id"]

        async def _cancel(cur_rid=res_id, cur_tok=token):
            return await client.post(
                f"/reservations/{cur_rid}/cancel",
                headers={"Authorization": f"Bearer {cur_tok}"},
            )

        async with background_invariant_poller(client, show_id):
            tasks = [_cancel() for _ in range(20)]
            responses = await asyncio.gather(*tasks)

        # All parallel cancels return 200 OK
        for r in responses:
            assert r.status_code == 200
            assert r.json()["status"] == "cancelled"

        # Check quota is exactly 0, not negative
        quota = await db_conn.fetchval(
            """
            SELECT held FROM user_show_quota WHERE show_id = $1 AND user_id = $2
            """,
            show_id,
            user_id,
        )
        assert quota == 0, f"Expected quota == 0, got {quota}"

        await assert_invariants(db_conn, show_id)
