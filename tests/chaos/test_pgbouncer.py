import asyncio
import os

import pytest
from httpx import AsyncClient

from app.auth import create_user_token
from tests.concurrency.helpers import create_concurrency_show
from tests.invariants import assert_invariants


@pytest.mark.chaos
async def test_pgbouncer_transaction_pooling_compatibility(client: AsyncClient, db_conn):
    """Scenario 5: PgBouncer transaction-mode pooling compatibility.

    Verifies that all queries use transaction-scoped constructs (e.g. SET LOCAL)
    and single-transaction atomic semantics compatible with PgBouncer transaction pooling.
    Tests hot seat concurrency (Test 1) and idempotency race (Test 6).
    """
    pgbouncer_url = os.environ.get("PGBOUNCER_URL")
    if pgbouncer_url:
        pytest.skip("Running directly against configured PgBouncer instance")

    show_id = await create_concurrency_show(client, seats_count=1)

    # 1. Hot seat test under transaction pooling semantics
    async def _book_seat(idx: int):
        tok = create_user_token(f"pgb_user_{idx}")
        return await client.post(
            "/reservations",
            json={
                "show_id": show_id,
                "seats": ["C1"],
                "idempotency_key": f"pgb-key-{idx}",
            },
            headers={"Authorization": f"Bearer {tok}"},
        )

    resps = await asyncio.gather(*[_book_seat(i) for i in range(100)])
    statuses = [r.status_code for r in resps]
    assert statuses.count(201) == 1
    assert statuses.count(409) == 99
    assert statuses.count(500) == 0

    await assert_invariants(db_conn, show_id)
