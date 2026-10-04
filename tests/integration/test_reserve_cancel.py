import uuid

import pytest
from httpx import AsyncClient

from app.auth import create_user_token
from app.config import settings
from tests.invariants import assert_invariants


async def _create_test_show(
    client: AsyncClient,
    seats_count: int = 10,
    price_paise: int = 20000,
    per_user_limit: int = 4,
) -> str:
    labels = [f"S{i}" for i in range(1, seats_count + 1)]
    resp = await client.post(
        "/shows",
        json={
            "name": f"Test Show {uuid.uuid4()}",
            "price_paise": price_paise,
            "per_user_limit": per_user_limit,
            "seats": labels,
        },
        headers={"Authorization": f"Bearer {settings.admin_token}"},
    )
    assert resp.status_code == 201
    return resp.json()["id"]


@pytest.mark.integration
async def test_happy_path_and_multi_seat(client: AsyncClient, db_conn):
    show_id = await _create_test_show(client, seats_count=5, price_paise=15000)
    token = create_user_token("user_happy")

    resp = await client.post(
        "/reservations",
        json={
            "show_id": show_id,
            "seats": ["S1", "S2"],
            "idempotency_key": "k-happy-1",
        },
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 201
    data = resp.json()
    assert data["status"] == "confirmed"
    assert data["seats"] == ["S1", "S2"]
    assert data["amount_paise"] == 30000
    assert data["user_id"] == "user_happy"

    state_resp = await client.get(f"/shows/{show_id}")
    state = state_resp.json()
    assert state["available"] == 3
    assert state["confirmed"] == 2
    assert state["seats"]["S1"] == "confirmed"
    assert state["seats"]["S2"] == "confirmed"

    await assert_invariants(db_conn, show_id)


@pytest.mark.integration
async def test_seat_taken_all_or_nothing(client: AsyncClient, db_conn):
    show_id = await _create_test_show(client, seats_count=5)
    token1 = create_user_token("user_first")
    token2 = create_user_token("user_second")

    # User 1 books S1
    r1 = await client.post(
        "/reservations",
        json={"show_id": show_id, "seats": ["S1"], "idempotency_key": "k-1"},
        headers={"Authorization": f"Bearer {token1}"},
    )
    assert r1.status_code == 201

    # User 2 attempts to book S1 and S2
    r2 = await client.post(
        "/reservations",
        json={"show_id": show_id, "seats": ["S1", "S2"], "idempotency_key": "k-2"},
        headers={"Authorization": f"Bearer {token2}"},
    )
    assert r2.status_code == 409
    err = r2.json()["error"]
    assert err["code"] == "seat_taken"
    assert err["unavailable_seats"] == ["S1"]

    # Verify S2 was NOT partially reserved
    state_resp = await client.get(f"/shows/{show_id}")
    state = state_resp.json()
    assert state["seats"]["S2"] == "available"
    assert state["available"] == 4
    assert state["confirmed"] == 1

    await assert_invariants(db_conn, show_id)


@pytest.mark.integration
async def test_validation_errors(client: AsyncClient, db_conn):
    show_id = await _create_test_show(client, seats_count=5, per_user_limit=4)
    token = create_user_token("user_val")

    # Unknown seat
    r_unknown = await client.post(
        "/reservations",
        json={"show_id": show_id, "seats": ["UNKNOWN_99"], "idempotency_key": "k-val-1"},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert r_unknown.status_code == 422
    assert r_unknown.json()["error"]["code"] == "unknown_seat"

    # Duplicate seats in request
    r_dup = await client.post(
        "/reservations",
        json={"show_id": show_id, "seats": ["S1", "S1"], "idempotency_key": "k-val-2"},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert r_dup.status_code == 422
    assert r_dup.json()["error"]["code"] == "validation_error"

    # More seats than per_user_limit
    r_over = await client.post(
        "/reservations",
        json={
            "show_id": show_id,
            "seats": ["S1", "S2", "S3", "S4", "S5"],
            "idempotency_key": "k-val-3",
        },
        headers={"Authorization": f"Bearer {token}"},
    )
    assert r_over.status_code == 422

    # Missing idempotency key
    r_nokey = await client.post(
        "/reservations",
        json={"show_id": show_id, "seats": ["S1"]},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert r_nokey.status_code == 422

    await assert_invariants(db_conn, show_id)


@pytest.mark.integration
async def test_per_user_limit_across_requests(client: AsyncClient, db_conn):
    show_id = await _create_test_show(client, seats_count=10, per_user_limit=4)
    token = create_user_token("user_limit_test")

    # Request 1: 2 seats -> OK
    r1 = await client.post(
        "/reservations",
        json={"show_id": show_id, "seats": ["S1", "S2"], "idempotency_key": "k-lim-1"},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert r1.status_code == 201

    # Request 2: 2 seats -> OK (total 4)
    r2 = await client.post(
        "/reservations",
        json={"show_id": show_id, "seats": ["S3", "S4"], "idempotency_key": "k-lim-2"},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert r2.status_code == 201

    # Request 3: 1 seat -> 409 per_user_limit
    r3 = await client.post(
        "/reservations",
        json={"show_id": show_id, "seats": ["S5"], "idempotency_key": "k-lim-3"},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert r3.status_code == 409
    assert r3.json()["error"]["code"] == "per_user_limit"

    await assert_invariants(db_conn, show_id)


@pytest.mark.integration
async def test_idempotency_semantics(client: AsyncClient, db_conn):
    show_id = await _create_test_show(client, seats_count=5)
    token1 = create_user_token("user_idem_1")
    token2 = create_user_token("user_idem_2")

    # 1. Initial reservation
    r1 = await client.post(
        "/reservations",
        json={"show_id": show_id, "seats": ["S1"]},
        headers={"Authorization": f"Bearer {token1}", "Idempotency-Key": "shared-key-1"},
    )
    assert r1.status_code == 201
    res1_body = r1.json()

    # 2. Replay with identical body -> 200 OK + header Idempotent-Replayed: true
    r2 = await client.post(
        "/reservations",
        json={"show_id": show_id, "seats": ["S1"]},
        headers={"Authorization": f"Bearer {token1}", "Idempotency-Key": "shared-key-1"},
    )
    assert r2.status_code == 200
    assert r2.headers.get("Idempotent-Replayed") == "true"
    assert r2.json() == res1_body

    # 3. Same user, same key, different seats -> 409 idempotency_key_reuse
    r3 = await client.post(
        "/reservations",
        json={"show_id": show_id, "seats": ["S2"]},
        headers={"Authorization": f"Bearer {token1}", "Idempotency-Key": "shared-key-1"},
    )
    assert r3.status_code == 409
    assert r3.json()["error"]["code"] == "idempotency_key_reuse"

    # 4. Different user, same key, different seat -> 201 Created (keys scoped per user)
    r4 = await client.post(
        "/reservations",
        json={"show_id": show_id, "seats": ["S2"]},
        headers={"Authorization": f"Bearer {token2}", "Idempotency-Key": "shared-key-1"},
    )
    assert r4.status_code == 201
    assert r4.json()["user_id"] == "user_idem_2"

    await assert_invariants(db_conn, show_id)


@pytest.mark.integration
async def test_decline_does_not_store_key(client: AsyncClient, db_conn):
    show_id = await _create_test_show(client, seats_count=5)
    token1 = create_user_token("user_a")
    token2 = create_user_token("user_b")

    # User A books S1
    r_a = await client.post(
        "/reservations",
        json={"show_id": show_id, "seats": ["S1"], "idempotency_key": "key-a"},
        headers={"Authorization": f"Bearer {token1}"},
    )
    assert r_a.status_code == 201
    res_a_id = r_a.json()["id"]

    # User B tries to book S1 with retry-key -> 409 seat_taken
    r_b1 = await client.post(
        "/reservations",
        json={"show_id": show_id, "seats": ["S1"], "idempotency_key": "retry-key"},
        headers={"Authorization": f"Bearer {token2}"},
    )
    assert r_b1.status_code == 409

    # User A cancels
    r_cancel = await client.post(
        f"/reservations/{res_a_id}/cancel",
        headers={"Authorization": f"Bearer {token1}"},
    )
    assert r_cancel.status_code == 200

    # User B retries with the SAME key "retry-key" -> must now succeed!
    r_b2 = await client.post(
        "/reservations",
        json={"show_id": show_id, "seats": ["S1"], "idempotency_key": "retry-key"},
        headers={"Authorization": f"Bearer {token2}"},
    )
    assert r_b2.status_code == 201
    assert r_b2.json()["seats"] == ["S1"]

    await assert_invariants(db_conn, show_id)


@pytest.mark.integration
async def test_cancel_semantics_and_guard(client: AsyncClient, db_conn):
    show_id = await _create_test_show(client, seats_count=5)
    token_owner = create_user_token("owner_user")
    token_other = create_user_token("other_user")

    # Book S1
    r_book = await client.post(
        "/reservations",
        json={"show_id": show_id, "seats": ["S1"], "idempotency_key": "k-own"},
        headers={"Authorization": f"Bearer {token_owner}"},
    )
    assert r_book.status_code == 201
    res_id = r_book.json()["id"]

    # Non-owner gets 404 (does not leak existence)
    r_foreign_cancel = await client.post(
        f"/reservations/{res_id}/cancel",
        headers={"Authorization": f"Bearer {token_other}"},
    )
    assert r_foreign_cancel.status_code == 404
    assert r_foreign_cancel.json()["error"]["code"] == "not_found"

    # Owner cancels -> 200 OK
    r_own_cancel = await client.post(
        f"/reservations/{res_id}/cancel",
        headers={"Authorization": f"Bearer {token_owner}"},
    )
    assert r_own_cancel.status_code == 200
    assert r_own_cancel.json()["status"] == "cancelled"

    # Double cancel -> 200 OK (idempotent)
    r_double_cancel = await client.post(
        f"/reservations/{res_id}/cancel",
        headers={"Authorization": f"Bearer {token_owner}"},
    )
    assert r_double_cancel.status_code == 200
    assert r_double_cancel.json()["status"] == "cancelled"

    # Seat is now re-booked by other_user
    r_rebook = await client.post(
        "/reservations",
        json={"show_id": show_id, "seats": ["S1"], "idempotency_key": "k-rebook"},
        headers={"Authorization": f"Bearer {token_other}"},
    )
    assert r_rebook.status_code == 201

    # If owner double cancels again, it returns 200 and DOES NOT
    # touch the seat now owned by other_user!
    r_triple_cancel = await client.post(
        f"/reservations/{res_id}/cancel",
        headers={"Authorization": f"Bearer {token_owner}"},
    )
    assert r_triple_cancel.status_code == 200

    # Verify S1 is still confirmed for other_user
    state_resp = await client.get(f"/shows/{show_id}")
    assert state_resp.json()["seats"]["S1"] == "confirmed"

    await assert_invariants(db_conn, show_id)


@pytest.mark.integration
async def test_body_spoofing_ignored(client: AsyncClient, db_conn):
    show_id = await _create_test_show(client, seats_count=5)
    token_attacker = create_user_token("attacker_1")

    # Attacker attempts to spoof user_id in body
    r_spoof = await client.post(
        "/reservations",
        json={
            "show_id": show_id,
            "seats": ["S1"],
            "idempotency_key": "k-spoof",
            "user_id": "victim_user",  # Spoofed!
        },
        headers={"Authorization": f"Bearer {token_attacker}"},
    )
    assert r_spoof.status_code == 201
    data = r_spoof.json()
    assert data["user_id"] == "attacker_1"  # Ignored body user_id!

    await assert_invariants(db_conn, show_id)
