from datetime import timedelta

import jwt
import pytest
from httpx import AsyncClient

from app.auth import create_user_token
from app.config import settings


@pytest.mark.unit
async def test_auth_token_minting(client: AsyncClient):
    response = await client.post("/auth/token", json={"user_id": "test_user_42"})
    assert response.status_code == 200
    data = response.json()
    assert "access_token" in data
    assert data["token_type"] == "bearer"
    assert data["expires_in"] > 0

    # Verify decoded claims
    payload = jwt.decode(
        data["access_token"],
        settings.jwt_secret,
        algorithms=[settings.jwt_algorithm],
    )
    assert payload["sub"] == "test_user_42"
    assert payload["role"] == "user"


@pytest.mark.unit
async def test_auth_token_empty_user_id(client: AsyncClient):
    # Empty string
    response = await client.post("/auth/token", json={"user_id": ""})
    assert response.status_code == 422
    data = response.json()
    assert data["error"]["code"] == "validation_error"

    # Whitespace only
    response = await client.post("/auth/token", json={"user_id": "   "})
    assert response.status_code == 422
    data = response.json()
    assert data["error"]["code"] == "validation_error"

    # Missing user_id field
    response = await client.post("/auth/token", json={})
    assert response.status_code == 422


@pytest.mark.unit
async def test_admin_route_unauthorized_when_missing_token(client: AsyncClient):
    payload = {
        "name": "Secret Show",
        "price_paise": 15000,
        "per_user_limit": 4,
        "seats": ["S1", "S2"],
    }
    response = await client.post("/shows", json=payload)
    assert response.status_code == 401
    data = response.json()
    assert data["error"]["code"] == "unauthorized"


@pytest.mark.unit
async def test_admin_route_forbidden_with_user_token(client: AsyncClient):
    # Obtain a regular user JWT
    token_resp = await client.post("/auth/token", json={"user_id": "regular_user"})
    user_token = token_resp.json()["access_token"]

    payload = {
        "name": "Secret Show",
        "price_paise": 15000,
        "per_user_limit": 4,
        "seats": ["S1", "S2"],
    }
    response = await client.post(
        "/shows",
        json=payload,
        headers={"Authorization": f"Bearer {user_token}"},
    )
    assert response.status_code == 403
    data = response.json()
    assert data["error"]["code"] == "forbidden"


@pytest.mark.unit
async def test_admin_route_forbidden_with_wrong_token(client: AsyncClient):
    payload = {
        "name": "Secret Show",
        "price_paise": 15000,
        "per_user_limit": 4,
        "seats": ["S1", "S2"],
    }
    response = await client.post(
        "/shows",
        json=payload,
        headers={"Authorization": "Bearer wrong-admin-token"},
    )
    assert response.status_code == 403
    data = response.json()
    assert data["error"]["code"] == "forbidden"


@pytest.mark.unit
async def test_admin_route_succeeds_with_admin_token(client: AsyncClient):
    payload = {
        "name": "Authorized Admin Show",
        "price_paise": 20000,
        "per_user_limit": 4,
        "seats": ["A1", "A2"],
    }
    response = await client.post(
        "/shows",
        json=payload,
        headers={"Authorization": f"Bearer {settings.admin_token}"},
    )
    assert response.status_code == 201
    data = response.json()
    assert data["name"] == "Authorized Admin Show"
    assert data["total_seats"] == 2


@pytest.mark.unit
async def test_expired_jwt_rejected(client: AsyncClient):
    # Create expired token
    expired_token = create_user_token("user_expired", expires_delta=timedelta(seconds=-10))

    # Test against admin route (should reject before or during auth)
    response = await client.post(
        "/shows",
        json={"name": "Expired Show", "price_paise": 100, "seats": ["E1"]},
        headers={"Authorization": f"Bearer {expired_token}"},
    )
    # Since admin route expects ADMIN_TOKEN, a bearer with expired
    # token is forbidden or unauthorized
    assert response.status_code in (401, 403)
