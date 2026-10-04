import asyncio

import pytest
from httpx import AsyncClient

from app.auth import create_user_token
from tests.concurrency.helpers import background_invariant_poller, create_concurrency_show
from tests.invariants import assert_invariants

ITERATIONS = 5


@pytest.mark.concurrency
async def test_idempotency_race_same_body(client: AsyncClient, db_conn):
    """The same key, same body, x 100 parallel.

    Exactly 1 x 201 Created, 99 x 200 replay, exactly 1 reservation row.
    """
    for iteration in range(ITERATIONS):
        show_id = await create_concurrency_show(client, seats_count=5)
        user_id = f"idem_race_user_{iteration}"
        token = create_user_token(user_id)
        shared_key = f"race-key-{iteration}"

        async def _book_same(cur_sid=show_id, cur_key=shared_key, cur_tok=token):
            return await client.post(
                "/reservations",
                json={
                    "show_id": cur_sid,
                    "seats": ["C1"],
                    "idempotency_key": cur_key,
                },
                headers={"Authorization": f"Bearer {cur_tok}"},
            )

        async with background_invariant_poller(client, show_id):
            tasks = [_book_same() for _ in range(100)]
            responses = await asyncio.gather(*tasks)

        status_counts = {}
        for r in responses:
            status_counts[r.status_code] = status_counts.get(r.status_code, 0) + 1

        assert status_counts.get(500, 0) == 0, f"Iteration {iteration}: 500 status observed"
        assert status_counts.get(201, 0) == 1, (
            f"Iteration {iteration}: Expected exactly 1 x 201, got {status_counts.get(201, 0)}"
        )
        assert status_counts.get(200, 0) == 99, (
            f"Iteration {iteration}: Expected 99 x 200, got {status_counts.get(200, 0)}"
        )

        for r in responses:
            if r.status_code == 200:
                assert r.headers.get("Idempotent-Replayed") == "true"

        # Check DB reservation row count
        res_count = await db_conn.fetchval(
            """
            SELECT count(*) FROM reservations WHERE show_id = $1 AND user_id = $2
            """,
            show_id,
            user_id,
        )
        assert res_count == 1, f"Expected 1 reservation row, got {res_count}"

        await assert_invariants(db_conn, show_id)


@pytest.mark.concurrency
async def test_idempotency_race_different_bodies(client: AsyncClient, db_conn):
    """Same key with 2 different bodies in parallel.

    Exactly one body wins (201/200), the other body gets 409 (idempotency_key_reuse).
    """
    for iteration in range(ITERATIONS):
        show_id = await create_concurrency_show(client, seats_count=5)
        user_id = f"idem_diff_user_{iteration}"
        token = create_user_token(user_id)
        shared_key = f"diff-key-{iteration}"

        async def _book_body(
            seats_choice: list,
            cur_sid=show_id,
            cur_key=shared_key,
            cur_tok=token,
        ):
            return await client.post(
                "/reservations",
                json={
                    "show_id": cur_sid,
                    "seats": seats_choice,
                    "idempotency_key": cur_key,
                },
                headers={"Authorization": f"Bearer {cur_tok}"},
            )

        async with background_invariant_poller(client, show_id):
            tasks = [_book_body(["C1"]) for _ in range(20)] + [
                _book_body(["C2"]) for _ in range(20)
            ]
            responses = await asyncio.gather(*tasks)

        status_counts = {}
        for r in responses:
            status_counts[r.status_code] = status_counts.get(r.status_code, 0) + 1

        assert status_counts.get(500, 0) == 0, f"Iteration {iteration}: 500 status observed"
        assert status_counts.get(201, 0) == 1
        assert status_counts.get(409, 0) > 0
        for r in responses:
            if r.status_code == 409:
                assert r.json()["error"]["code"] == "idempotency_key_reuse"

        await assert_invariants(db_conn, show_id)
