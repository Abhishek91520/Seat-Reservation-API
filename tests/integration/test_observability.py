import asyncio
import io
import json
import logging
import uuid
from typing import Dict, List, Optional

import pytest
from httpx import AsyncClient
from prometheus_client.parser import text_string_to_metric_families

import app.db as db_module
from app.auth import create_user_token
from app.config import settings


def get_metric_sample_value(
    metrics_text: str,
    metric_name: str,
    labels: Optional[Dict[str, str]] = None,
) -> float:
    """Extracts a metric sample value from the Prometheus text output."""
    for family in text_string_to_metric_families(metrics_text):
        for sample in family.samples:
            if sample.name == metric_name:
                if labels is None or all(sample.labels.get(k) == v for k, v in labels.items()):
                    return float(sample.value)
    return 0.0


@pytest.mark.integration
async def test_metrics_endpoint_parses(client: AsyncClient):
    """The metrics endpoint parses cleanly with prometheus_client.parser."""
    resp = await client.get("/metrics")
    assert resp.status_code == 200
    assert "text/plain" in resp.headers["content-type"]

    families = {f.name: f for f in text_string_to_metric_families(resp.text)}
    expected_families = [
        "reservations_confirmed",
        "seats_confirmed",
        "reservations_declined",
        "reservations_cancelled",
        "http_requests",
        "http_request_duration_seconds",
        "inflight_requests",
        "db_pool_in_use",
        "db_semaphore_waiting",
        "db_retries",
        "seats_available",
        "seats_confirmed",
        "seats_held",
    ]
    for ef in expected_families:
        assert ef in families, f"Expected metric family '{ef}' missing from /metrics"


@pytest.mark.integration
async def test_counters_equal_known_n_operations(client: AsyncClient):
    """After N known operations the Prometheus counters increment by exactly N."""
    show_resp = await client.post(
        "/shows",
        json={
            "name": "Metrics Counter Test Show",
            "price_paise": 5000,
            "per_user_limit": 4,
            "seats": ["M1", "M2", "M3", "M4", "M5"],
        },
        headers={"Authorization": f"Bearer {settings.admin_token}"},
    )
    assert show_resp.status_code == 201
    show_id = show_resp.json()["id"]

    # Initial metric baseline
    init_metrics = (await client.get("/metrics")).text
    init_res_confirmed = get_metric_sample_value(init_metrics, "reservations_confirmed_total")
    init_seats_confirmed = get_metric_sample_value(init_metrics, "seats_confirmed_total")
    init_seat_taken = get_metric_sample_value(
        init_metrics, "reservations_declined_total", {"reason": "seat_taken"}
    )
    init_quota_declined = get_metric_sample_value(
        init_metrics, "reservations_declined_total", {"reason": "per_user_limit"}
    )
    init_cancelled = get_metric_sample_value(init_metrics, "reservations_cancelled_total")

    user_a = "metrics_user_a"
    token_a = create_user_token(user_a)

    # Operation 1: Book 2 seats (M1, M2) -> 1 reservation, 2 seats confirmed
    res1 = await client.post(
        "/reservations",
        json={"show_id": show_id, "seats": ["M1", "M2"], "idempotency_key": "k-m-1"},
        headers={"Authorization": f"Bearer {token_a}"},
    )
    assert res1.status_code == 201
    res1_id = res1.json()["id"]

    # Operation 2: Book 1 seat (M3) -> 1 reservation, 1 seat confirmed
    res2 = await client.post(
        "/reservations",
        json={"show_id": show_id, "seats": ["M3"], "idempotency_key": "k-m-2"},
        headers={"Authorization": f"Bearer {token_a}"},
    )
    assert res2.status_code == 201

    # Operation 3: Decline on seat_taken (M1 already booked)
    user_b = "metrics_user_b"
    token_b = create_user_token(user_b)
    res3 = await client.post(
        "/reservations",
        json={"show_id": show_id, "seats": ["M1"], "idempotency_key": "k-m-3"},
        headers={"Authorization": f"Bearer {token_b}"},
    )
    assert res3.status_code == 409
    assert res3.json()["error"]["code"] == "seat_taken"

    # Operation 4: Decline on per_user_limit (User A already holds 3, limit is 4, requests 2)
    res4 = await client.post(
        "/reservations",
        json={"show_id": show_id, "seats": ["M4", "M5"], "idempotency_key": "k-m-4"},
        headers={"Authorization": f"Bearer {token_a}"},
    )
    assert res4.status_code == 409
    assert res4.json()["error"]["code"] == "per_user_limit"

    # Operation 5: Cancel res1 (frees M1, M2)
    cancel_resp = await client.post(
        f"/reservations/{res1_id}/cancel",
        headers={"Authorization": f"Bearer {token_a}"},
    )
    assert cancel_resp.status_code == 200

    # Scrape metrics post operations
    post_metrics = (await client.get("/metrics")).text
    new_res_confirmed = get_metric_sample_value(post_metrics, "reservations_confirmed_total")
    new_seats_confirmed = get_metric_sample_value(post_metrics, "seats_confirmed_total")
    new_seat_taken = get_metric_sample_value(
        post_metrics, "reservations_declined_total", {"reason": "seat_taken"}
    )
    new_quota_declined = get_metric_sample_value(
        post_metrics, "reservations_declined_total", {"reason": "per_user_limit"}
    )
    new_cancelled = get_metric_sample_value(post_metrics, "reservations_cancelled_total")

    # Assert exactly N deltas
    assert new_res_confirmed - init_res_confirmed == 2  # res1, res2
    assert new_seats_confirmed - init_seats_confirmed == 3  # 2 in res1 + 1 in res2
    assert new_seat_taken - init_seat_taken == 1  # res3
    assert new_quota_declined - init_quota_declined == 1  # res4
    assert new_cancelled - init_cancelled == 1  # cancel_resp


@pytest.mark.integration
async def test_reconciliation_after_mixed_burst(client: AsyncClient, db_conn):
    """Reconciliation test:

    reservations_confirmed_total - reservations_cancelled_total == count of confirmed
    reservations in DB, and seats_available equals GET /shows/{id}.available.
    """
    show_resp = await client.post(
        "/shows",
        json={
            "name": "Reconciliation Show",
            "price_paise": 2000,
            "per_user_limit": 4,
            "seats": [f"R{i}" for i in range(1, 21)],
        },
        headers={"Authorization": f"Bearer {settings.admin_token}"},
    )
    assert show_resp.status_code == 201
    show_id = show_resp.json()["id"]

    created_res_ids: List[str] = []
    tokens: List[str] = []

    # Book 10 individual reservations
    for i in range(10):
        u_id = f"reconcile_user_{i}"
        tok = create_user_token(u_id)
        tokens.append(tok)
        r = await client.post(
            "/reservations",
            json={"show_id": show_id, "seats": [f"R{i + 1}"], "idempotency_key": f"r-key-{i}"},
            headers={"Authorization": f"Bearer {tok}"},
        )
        assert r.status_code == 201
        created_res_ids.append(r.json()["id"])

    # Cancel 4 of the reservations
    for i in range(4):
        c_res = await client.post(
            f"/reservations/{created_res_ids[i]}/cancel",
            headers={"Authorization": f"Bearer {tokens[i]}"},
        )
        assert c_res.status_code == 200

    # Repeat one cancellation to verify idempotent replay does not alter counts
    dup_cancel = await client.post(
        f"/reservations/{created_res_ids[0]}/cancel",
        headers={"Authorization": f"Bearer {tokens[0]}"},
    )
    assert dup_cancel.status_code == 200

    # Trigger a scrape
    metrics_text = (await client.get("/metrics")).text
    conf_total = get_metric_sample_value(metrics_text, "reservations_confirmed_total")
    canc_total = get_metric_sample_value(metrics_text, "reservations_cancelled_total")

    # DB confirmed reservations count
    db_confirmed_count = await db_conn.fetchval(
        "SELECT count(*) FROM reservations WHERE status = 'confirmed'"
    )
    assert conf_total - canc_total == db_confirmed_count, (
        f"Reconciliation mismatch: {conf_total} - {canc_total} != {db_confirmed_count}"
    )

    # Check seats_available gauge vs GET /shows/{id}
    state_resp = await client.get(f"/shows/{show_id}")
    assert state_resp.status_code == 200
    state_avail = state_resp.json()["available"]

    gauge_avail = get_metric_sample_value(
        metrics_text,
        "seats_available",
        {"show_id": show_id},
    )
    assert gauge_avail == state_avail, (
        f"Gauge seats_available ({gauge_avail}) != show state ({state_avail})"
    )


@pytest.mark.integration
async def test_x_request_id_and_structured_logs(client: AsyncClient):
    """Every response has an X-Request-ID, client ID is echoed, and logs carry context."""
    # 1. Server generates UUID when missing
    resp1 = await client.get("/healthz")
    assert resp1.status_code == 200
    req_id1 = resp1.headers.get("X-Request-ID")
    assert req_id1 is not None
    assert len(req_id1) > 10

    # 2. Client-supplied ID is echoed exactly
    custom_trace_id = "trace-obs-" + uuid.uuid4().hex
    resp2 = await client.get("/healthz", headers={"X-Request-ID": custom_trace_id})
    assert resp2.status_code == 200
    assert resp2.headers.get("X-Request-ID") == custom_trace_id

    # 3. Reserve request includes show_id, seats, reason in structured log
    log_capture = io.StringIO()
    handler = logging.StreamHandler(log_capture)
    logging.getLogger().addHandler(handler)

    try:
        show_resp = await client.post(
            "/shows",
            json={
                "name": "Log Test Show",
                "price_paise": 1000,
                "per_user_limit": 4,
                "seats": ["L1"],
            },
            headers={"Authorization": f"Bearer {settings.admin_token}"},
        )
        show_id = show_resp.json()["id"]

        user_token = create_user_token("log_test_user")
        reserve_resp = await client.post(
            "/reservations",
            json={
                "show_id": show_id,
                "seats": ["L1"],
                "idempotency_key": "log-test-key-1",
            },
            headers={
                "Authorization": f"Bearer {user_token}",
                "X-Request-ID": "test-reserve-trace-99",
            },
        )
        assert reserve_resp.status_code == 201
        assert reserve_resp.headers.get("X-Request-ID") == "test-reserve-trace-99"

        # Examine log output lines
        log_lines = log_capture.getvalue().strip().split("\n")
        reserve_log_entry = None
        for line in log_lines:
            try:
                data = json.loads(line)
                if data.get("request_id") == "test-reserve-trace-99":
                    reserve_log_entry = data
                    break
            except Exception:
                continue

        assert reserve_log_entry is not None, "Log entry with request_id not found"
        assert reserve_log_entry["show_id"] == show_id
        assert reserve_log_entry["seats"] == ["L1"]
        assert reserve_log_entry["reason"] == "confirmed"
        assert reserve_log_entry["user_id"] == "log_test_user"
        assert reserve_log_entry["outcome"] == "success"
    finally:
        logging.getLogger().removeHandler(handler)


@pytest.mark.integration
async def test_readyz_stays_200_when_main_pool_saturated(client: AsyncClient):
    """/readyz must remain 200 and responsive even when the main pool is 100% saturated."""
    assert db_module.pool is not None
    pool = db_module.pool

    # Saturate the pool by acquiring all max_size connections
    held_conns = []
    try:
        # Acquire up to pool max size
        for _ in range(settings.db_pool_max_size):
            conn = await pool.acquire()
            held_conns.append(conn)

        # Main pool is now fully checked out
        assert pool.get_size() >= settings.db_pool_max_size
        assert pool.get_idle_size() == 0

        # /readyz must still succeed via its reserved connection within 1s
        ready_resp = await asyncio.wait_for(client.get("/readyz"), timeout=2.0)
        assert ready_resp.status_code == 200
        assert ready_resp.json()["status"] == "ready"
    finally:
        # Release all checked out connections back to the pool
        for conn in held_conns:
            await pool.release(conn)
