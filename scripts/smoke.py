#!/usr/bin/env python3
"""scripts/smoke.py: End-to-end smoke check against any deployment (local or remote live URL).

Tests:
1. Health check (GET /healthz -> 200)
2. Readiness check (GET /readyz -> 200)
3. Auth token issuance (POST /auth/token -> 200)
4. Admin show creation (POST /shows -> 201)
5. Reservation booking (POST /reservations -> 201)
6. Idempotent replay (POST /reservations -> 200 with Idempotent-Replayed: true)
7. Reservation cancellation (POST /reservations/{id}/cancel -> 200)
8. Invariant verification (GET /shows/{id} -> 200 with available == total)
"""

import os
import sys
import uuid

import httpx

ADMIN_TOKEN = os.environ.get("ADMIN_TOKEN", "admin-secret-token")


def run_smoke(base_url: str):
    print(f"=== Running Smoke Test against {base_url} ===")
    client = httpx.Client(base_url=base_url, timeout=10.0)

    # 1. Health check
    r = client.get("/healthz")
    assert r.status_code == 200, f"/healthz failed: {r.status_code} {r.text}"
    print("[PASS] 1. GET /healthz -> 200 OK")

    # 2. Readiness check
    r = client.get("/readyz")
    assert r.status_code == 200, f"/readyz failed: {r.status_code} {r.text}"
    print("[PASS] 2. GET /readyz -> 200 OK")

    # 3. Auth token
    user_id = f"smoke_user_{uuid.uuid4().hex[:8]}"
    r = client.post("/auth/token", json={"user_id": user_id})
    assert r.status_code == 200, f"/auth/token failed: {r.status_code} {r.text}"
    token = r.json().get("access_token") or r.json().get("token")
    assert token, f"No token in /auth/token response: {r.json()}"
    auth_headers = {"Authorization": f"Bearer {token}"}
    print("[PASS] 3. POST /auth/token -> 200 OK (JWT issued)")

    # 4. Create show
    admin_headers = {"Authorization": f"Bearer {ADMIN_TOKEN}"}
    show_name = f"Smoke Show {uuid.uuid4().hex[:6]}"
    show_payload = {
        "name": show_name,
        "price_paise": 150000,
        "per_user_limit": 4,
        "seats": ["S1", "S2", "S3", "S4", "S5", "S6", "S7", "S8", "S9", "S10"],
    }
    r = client.post("/shows", json=show_payload, headers=admin_headers)
    assert r.status_code == 201, f"/shows failed: {r.status_code} {r.text}"
    show_id = r.json()["id"]
    print(f"[PASS] 4. POST /shows -> 201 Created (show_id={show_id})")

    # 5. Reserve seat S1
    idem_key = f"smoke_key_{uuid.uuid4().hex}"
    reserve_payload = {
        "show_id": show_id,
        "seats": ["S1"],
        "idempotency_key": idem_key,
    }
    r = client.post("/reservations", json=reserve_payload, headers=auth_headers)
    assert r.status_code == 201, f"Reserve failed: {r.status_code} {r.text}"
    res_data = r.json()
    reservation_id = res_data["id"]
    assert res_data["status"] == "confirmed"
    print(f"[PASS] 5. POST /reservations -> 201 Created (reservation_id={reservation_id})")

    # 6. Idempotent replay
    r = client.post("/reservations", json=reserve_payload, headers=auth_headers)
    assert r.status_code == 200, f"Replay failed: {r.status_code} {r.text}"
    assert r.headers.get("Idempotent-Replayed") == "true", (
        f"Missing Idempotent-Replayed header: {r.headers}"
    )
    assert r.json()["id"] == reservation_id
    print("[PASS] 6. POST /reservations (replay) -> 200 OK [Idempotent-Replayed: true]")

    # 7. Cancel reservation
    r = client.post(f"/reservations/{reservation_id}/cancel", headers=auth_headers)
    assert r.status_code == 200, f"Cancel failed: {r.status_code} {r.text}"
    assert r.json()["status"] == "cancelled"
    print("[PASS] 7. POST /reservations/{id}/cancel -> 200 OK (status=cancelled)")

    # 8. Check show state after cancel
    r = client.get(f"/shows/{show_id}")
    assert r.status_code == 200, f"GET /shows failed: {r.status_code} {r.text}"
    state = r.json()
    assert state["total_seats"] == 10
    assert state["available"] == 10
    assert state["held"] == 0
    assert state["confirmed"] == 0
    assert state["seats"]["S1"] == "available"
    print("[PASS] 8. GET /shows/{id} -> Invariant verified (10 available == 10 total)")

    print("\nALL SMOKE CHECKS PASSED [OK]")


if __name__ == "__main__":
    url = sys.argv[1] if len(sys.argv) > 1 else "http://localhost:8000"
    try:
        run_smoke(url)
    except AssertionError as e:
        print(f"\n[FAIL] SMOKE CHECK FAILED: {e}", file=sys.stderr)
        sys.exit(1)
    except Exception as e:
        print(f"\n[ERROR] UNEXPECTED ERROR: {e}", file=sys.stderr)
        sys.exit(2)
