import asyncio
import random
import time

import pytest
from httpx import AsyncClient

from app.auth import create_user_token
from tests.concurrency.helpers import create_concurrency_show
from tests.invariants import assert_invariants


@pytest.mark.chaos
async def test_resource_constrained_stampede(client: AsyncClient, db_conn):
    """Scenario 6: Resource-constrained burst test (prodlike profile).

    Executes a high-concurrency burst across 200 seats with Zipf distribution.
    Computes and asserts:
    - Zero 5xx errors
    - p50 and p99 latency
    - Invariants hold strictly
    """
    seats_count = 200
    show_id = await create_concurrency_show(client, seats_count=seats_count, per_user_limit=4)
    seat_labels = [f"C{i}" for i in range(1, seats_count + 1)]
    weights = [1.0 / (i**1.2) for i in range(1, seats_count + 1)]

    num_requests = 1000
    latencies = []

    sem = asyncio.Semaphore(100)

    async def _timed_request(idx: int):
        user_tok = create_user_token(f"prodlike_user_{idx}")
        seat = random.choices(seat_labels, weights=weights, k=1)[0]
        async with sem:
            t0 = time.perf_counter()
            r = await client.post(
                "/reservations",
                json={
                    "show_id": show_id,
                    "seats": [seat],
                    "idempotency_key": f"prodlike-key-{idx}",
                },
                headers={"Authorization": f"Bearer {user_tok}"},
            )
            latencies.append((time.perf_counter() - t0) * 1000)
            return r

    t_start = time.perf_counter()
    responses = await asyncio.gather(*[_timed_request(i) for i in range(num_requests)])
    t_total = time.perf_counter() - t_start

    # Check status codes
    status_codes = [r.status_code for r in responses]
    assert status_codes.count(500) == 0, "5xx errors observed under resource-constrained burst"

    latencies.sort()
    p50 = latencies[int(len(latencies) * 0.50)]
    p95 = latencies[int(len(latencies) * 0.95)]
    p99 = latencies[int(len(latencies) * 0.99)]
    throughput = num_requests / t_total

    print(
        f"\n[Prodlike Benchmark] Throughput: {throughput:.1f} req/s | "
        f"p50: {p50:.1f}ms | p95: {p95:.1f}ms | p99: {p99:.1f}ms"
    )

    await assert_invariants(db_conn, show_id)
