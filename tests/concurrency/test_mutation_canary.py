import asyncio

import pytest
from httpx import AsyncClient

import app.services.reserve as reserve_module
from app.auth import create_user_token
from tests.concurrency.helpers import create_concurrency_show


@pytest.mark.canary
async def test_mutation_canary_catches_double_booking(client: AsyncClient, db_conn):
    """Canary Test: Temporarily mutates reservation logic to remove FOR UPDATE locking.

    Proves that without row-level locking, race conditions immediately occur
    and the concurrency test suite reliably catches double-selling.
    """
    # Define a mutated reserve operation that simulates naive read-then-write
    orig_reserve_seats = reserve_module.reserve_seats

    async def mutated_reserve_seats(user_id, show_id_str, seats, idempotency_key):
        # Naive implementation: read without FOR UPDATE, update without atomic check
        import uuid

        from app.db import db_connection

        show_uuid = uuid.UUID(show_id_str)
        async with db_connection() as conn:
            async with conn.transaction():
                # 1. Plain read WITHOUT FOR UPDATE
                seat_row = await conn.fetchrow(
                    "SELECT label, status FROM seats WHERE show_id = $1 AND label = $2",
                    show_uuid,
                    seats[0],
                )
                # Introduce a tiny context-switch yield to amplify the race window
                await asyncio.sleep(0.001)

                if seat_row and seat_row["status"] == "available":
                    res_id = uuid.uuid4()
                    # 2. Blind overwrite
                    await conn.execute(
                        """
                        UPDATE seats SET status = 'confirmed', reservation_id = $3, user_id = $4
                        WHERE show_id = $1 AND label = $2
                        """,
                        show_uuid,
                        seats[0],
                        res_id,
                        user_id,
                    )
                    await conn.execute(
                        """
                        INSERT INTO reservations (id, show_id, user_id, seats, amount_paise, status)
                        VALUES ($1, $2, $3, $4, 10000, 'confirmed')
                        """,
                        res_id,
                        show_uuid,
                        user_id,
                        seats,
                    )
                    return {"id": str(res_id), "seats": seats}, 201, False
                else:
                    return {"error": "seat_taken"}, 409, False

    import app.routes.reservations as res_route

    orig_reserve_seats = res_route.reserve_seats
    res_route.reserve_seats = mutated_reserve_seats

    try:
        show_id = await create_concurrency_show(client, seats_count=1)

        async def _contender(idx: int):
            user_id = f"canary_user_{idx}"
            token = create_user_token(user_id)
            return await client.post(
                "/reservations",
                json={
                    "show_id": show_id,
                    "seats": ["C1"],
                    "idempotency_key": f"canary-key-{idx}",
                },
                headers={"Authorization": f"Bearer {token}"},
            )

        # 50 contenders race on 1 seat without locks
        tasks = [_contender(i) for i in range(50)]
        responses = await asyncio.gather(*tasks)

        winner_count = sum(1 for r in responses if r.status_code == 201)

        # PROOF: Without locking, more than 1 contender received 201 Created for the same seat!
        assert winner_count > 1, (
            f"Canary mutation failed to induce race condition: winner_count was {winner_count}"
        )
        print(f"\n[CANARY VERIFIED]: Naive read-then-write produced {winner_count} double-sellers!")

    finally:
        # Restore original implementation
        res_route.reserve_seats = orig_reserve_seats
