import asyncio
import re
import uuid

import pytest
from httpx import ASGITransport, AsyncClient

from app.auth import create_user_token
from app.config import settings
from app.main import app
from app.services.shows import clear_show_state_cache


@pytest.mark.integration
async def test_live_endpoint_returns_200_and_no_external_resources():
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get("/live")
        assert resp.status_code == 200
        assert "text/html" in resp.headers["content-type"]
        html = resp.text

        # Assert no external http:// or https:// resource URLs (scripts, stylesheets, fonts, images)
        # Allows schema definitions or same-origin paths if any, but no external src/href
        external_res_pattern = re.compile(
            r'(?:src|href|url)\s*=\s*["\']https?://',
            re.IGNORECASE,
        )
        matches = external_res_pattern.findall(html)
        assert not matches, f"Found external resource URLs in /live HTML: {matches}"

        # Assert key UI elements are present in self-contained file
        assert "Seat Reservation API - Live Seat Grid" in html
        assert "SEAT ALLOCATION MAP" in html
        assert "sparkline" in html


@pytest.mark.integration
async def test_show_state_cache_hit_and_expiry(client: AsyncClient):
    clear_show_state_cache()
    original_cache_ms = settings.show_state_cache_ms
    settings.show_state_cache_ms = 500

    admin_headers = {"Authorization": f"Bearer {settings.admin_token}"}
    show_payload = {
        "name": f"Cache Test Show {uuid.uuid4().hex[:6]}",
        "price_paise": 10000,
        "per_user_limit": 4,
        "seats": ["C1", "C2", "C3"],
    }
    create_resp = await client.post("/shows", json=show_payload, headers=admin_headers)
    assert create_resp.status_code == 201
    show_id = create_resp.json()["id"]

    try:
        # 1. First read populates cache
        r1 = await client.get(f"/shows/{show_id}")
        assert r1.status_code == 200
        assert r1.json()["available"] == 3

        # 2. Second read within 500ms hits in-process cache
        r2 = await client.get(f"/shows/{show_id}")
        assert r2.status_code == 200
        assert r2.json()["available"] == 3

        # Verify from internal cache state that cache entry exists and is fresh
        from app.services.shows import _show_state_cache

        assert show_id in _show_state_cache
        cached_ts_1, _ = _show_state_cache[show_id]

        # 3. Reserve seat C1 directly
        token = create_user_token("cache_user")
        res_payload = {
            "show_id": show_id,
            "seats": ["C1"],
            "idempotency_key": f"key_{uuid.uuid4().hex}",
        }
        res_resp = await client.post(
            "/reservations",
            json=res_payload,
            headers={"Authorization": f"Bearer {token}"},
        )
        assert res_resp.status_code == 201

        # 4. Immediate read within 500ms window returns cached value (still 3 available)
        r_cached = await client.get(f"/shows/{show_id}")
        assert r_cached.status_code == 200
        assert r_cached.json()["available"] == 3

        # 5. Wait for cache TTL window to expire (>500ms)
        await asyncio.sleep(0.55)

        # 6. Read after window expired queries fresh from DB and shows 2 available, 1 confirmed
        r_fresh = await client.get(f"/shows/{show_id}")
        assert r_fresh.status_code == 200
        data_fresh = r_fresh.json()
        assert data_fresh["available"] == 2
        assert data_fresh["confirmed"] == 1
        assert data_fresh["seats"]["C1"] == "confirmed"

        cached_ts_2, _ = _show_state_cache[show_id]
        assert cached_ts_2 > cached_ts_1
    finally:
        settings.show_state_cache_ms = original_cache_ms
        clear_show_state_cache()
