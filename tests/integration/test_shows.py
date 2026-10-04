import uuid

import pytest
from httpx import AsyncClient

from app.config import settings
from tests.invariants import assert_invariants


@pytest.mark.integration
async def test_create_and_get_fresh_show(client: AsyncClient, db_conn):
    seats = [f"A{i}" for i in range(1, 51)]
    payload = {
        "name": "Live Concert Show",
        "price_paise": 45000,
        "per_user_limit": 4,
        "seats": seats,
    }

    # 1. Create show
    response = await client.post(
        "/shows",
        json=payload,
        headers={"Authorization": f"Bearer {settings.admin_token}"},
    )
    assert response.status_code == 201
    created_show = response.json()
    show_id = created_show["id"]
    assert created_show["name"] == "Live Concert Show"
    assert created_show["price_paise"] == 45000
    assert created_show["per_user_limit"] == 4
    assert created_show["total_seats"] == 50

    # 2. Get show state
    state_resp = await client.get(f"/shows/{show_id}")
    assert state_resp.status_code == 200
    state = state_resp.json()
    assert state["id"] == show_id
    assert state["total_seats"] == 50
    assert state["available"] == 50
    assert state["held"] == 0
    assert state["confirmed"] == 0
    assert len(state["seats"]) == 50
    for seat_label in seats:
        assert state["seats"][seat_label] == "available"

    # 3. Verify database invariants on fresh show
    await assert_invariants(db_conn, show_id)


@pytest.mark.integration
async def test_create_show_rejects_duplicate_seats(client: AsyncClient):
    payload = {
        "name": "Duplicate Seat Show",
        "price_paise": 10000,
        "per_user_limit": 4,
        "seats": ["A1", "A2", "A1"],
    }
    response = await client.post(
        "/shows",
        json=payload,
        headers={"Authorization": f"Bearer {settings.admin_token}"},
    )
    assert response.status_code == 422
    data = response.json()
    assert data["error"]["code"] == "validation_error"


@pytest.mark.integration
async def test_create_show_rejects_empty_seat_label(client: AsyncClient):
    payload = {
        "name": "Empty Seat Show",
        "price_paise": 10000,
        "per_user_limit": 4,
        "seats": ["A1", "   "],
    }
    response = await client.post(
        "/shows",
        json=payload,
        headers={"Authorization": f"Bearer {settings.admin_token}"},
    )
    assert response.status_code == 422
    data = response.json()
    assert data["error"]["code"] == "validation_error"


@pytest.mark.integration
async def test_create_show_rejects_float_price(client: AsyncClient):
    # Float prices violate requirement: "Money is integer paise. Never floats anywhere."
    payload = {
        "name": "Float Price Show",
        "price_paise": 1250.50,
        "per_user_limit": 4,
        "seats": ["A1", "A2"],
    }
    response = await client.post(
        "/shows",
        json=payload,
        headers={"Authorization": f"Bearer {settings.admin_token}"},
    )
    assert response.status_code == 422
    data = response.json()
    assert data["error"]["code"] == "validation_error"


@pytest.mark.integration
async def test_create_show_rejects_invalid_limits(client: AsyncClient):
    # Limit < 1
    response = await client.post(
        "/shows",
        json={"name": "Show", "price_paise": 1000, "per_user_limit": 0, "seats": ["A1"]},
        headers={"Authorization": f"Bearer {settings.admin_token}"},
    )
    assert response.status_code == 422

    # Limit > 100
    response = await client.post(
        "/shows",
        json={"name": "Show", "price_paise": 1000, "per_user_limit": 101, "seats": ["A1"]},
        headers={"Authorization": f"Bearer {settings.admin_token}"},
    )
    assert response.status_code == 422


@pytest.mark.integration
async def test_get_nonexistent_show_returns_404(client: AsyncClient):
    random_uuid = str(uuid.uuid4())
    response = await client.get(f"/shows/{random_uuid}")
    assert response.status_code == 404
    data = response.json()
    assert data["error"]["code"] == "not_found"

    # Malformed UUID also returns 404 cleanly
    malformed_response = await client.get("/shows/not-a-valid-uuid")
    assert malformed_response.status_code == 404
    data = malformed_response.json()
    assert data["error"]["code"] == "not_found"


@pytest.mark.integration
async def test_reserve_via_shows_endpoint(client: AsyncClient, db_conn):
    # 1. Create show
    resp = await client.post(
        "/shows",
        json={"name": "Reserve Path Show", "price_paise": 25000, "seats": ["A1", "A2"]},
        headers={"Authorization": f"Bearer {settings.admin_token}"},
    )
    assert resp.status_code == 201
    show_id = resp.json()["id"]

    # 2. Get user token
    token_resp = await client.post("/auth/token", json={"user_id": "test_user_shows_reserve"})
    token = token_resp.json()["access_token"]

    # 3. Reserve via POST /shows/{id}/reserve
    reserve_resp = await client.post(
        f"/shows/{show_id}/reserve",
        json={"seats": ["A1"], "idempotency_key": "show-reserve-key-1"},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert reserve_resp.status_code == 201
    res_data = reserve_resp.json()
    assert res_data["show_id"] == show_id
    assert res_data["status"] == "confirmed"
    assert res_data["seats"] == ["A1"]
    assert "reservation_id" in res_data
    assert "id" in res_data
    await assert_invariants(db_conn, show_id)
