# High-Concurrency Seat Reservation API

A production-grade, highly observable seat reservation service built to remain strictly correct under ~20,000 concurrent requests without double-booking, state corruption, or 5xx errors.

---

## 1. Live Service Information & Endpoints

| Resource | Path / Value | Description |
| :--- | :--- | :--- |
| **Live Service URL** | `https://<render-app-slug>.onrender.com` | Deployed on Render (Singapore, `ap-southeast-1`) |
| **Liveness Check** | `GET /healthz` | Process liveness (no DB call, always 200 if up) |
| **Readiness Check** | `GET /readyz` | Probes PostgreSQL on dedicated connection (`SELECT 1`, 1s timeout) |
| **Prometheus Metrics**| `GET /metrics` | Prometheus text metrics (counters, histograms, gauges) |
| **Show State** | `GET /shows/{id}` | Atomic snapshot of show availability and seat map |
| **Auth Token** | `POST /auth/token` | Mints signed HS256 JWT for testing |
| **Create Show** | `POST /shows` | Admin endpoint to create show and initialize seats |
| **Reserve Seats** | `POST /reservations` | Reserve 1 to N seats with idempotency key |
| **Cancel Booking** | `POST /reservations/{id}/cancel` | Cancel an active reservation |
| **Live Dashboard** | `GET /live?show=<id>` | Real-time seat allocation grid visualizer |


---

## 2. Standing Assumptions & Grader Conventions

### A. How Graders Obtain Tokens (Open Token Endpoint)
Tokens are generated using the open authentication endpoint:
```bash
curl -X POST https://<app>.onrender.com/auth/token \
  -H "Content-Type: application/json" \
  -d '{"user_id": "grader_demo"}'
```
Response:
```json
{
  "access_token": "eyJhbGciOiJIUzI1Ni...",
  "token_type": "bearer",
  "expires_in": 604800
}
```
- The JWT contains `{"sub": "<user_id>", "role": "user"}` signed with `JWT_SECRET`.
- User identity is **strictly derived from the JWT `sub` claim**. Any `user_id` provided in request bodies is completely ignored to prevent spoofing.

### B. Idempotent Replay Returns HTTP 200
- Initial reservation confirmation returns **HTTP 201 Created**.
- Idempotent replay (same `user_id`, same `idempotency_key`, same seats) returns **HTTP 200 OK** with the header `Idempotent-Replayed: true` and a body identical to the original response.
- **Why**: This guarantees the invariant: *for any individual seat, there is exactly one 201 response emitted, even if the client retries over an unreliable network*. Reusing the same key with conflicting seats returns **HTTP 409 Conflict** (`idempotency_key_reuse`).

### C. Admin Token Management
- The administrative route `POST /shows` requires:
  ```http
  Authorization: Bearer <ADMIN_TOKEN>
  ```
- The `ADMIN_TOKEN` secret is configured via environment variables and supplied securely in the submission email (not committed to public source control).

---

## 3. Quickstart: End-to-End cURL Flow

### Step 1: Check System Readiness
```bash
curl -i https://<app>.onrender.com/readyz
```

### Step 2: Mint a User JWT
```bash
TOKEN=$(curl -s -X POST https://<app>.onrender.com/auth/token \
  -H "Content-Type: application/json" \
  -d '{"user_id": "alice"}' | jq -r .access_token)
```

### Step 3: Create a Show (Admin)
```bash
SHOW_ID=$(curl -s -X POST https://<app>.onrender.com/shows \
  -H "Authorization: Bearer <ADMIN_TOKEN>" \
  -H "Content-Type: application/json" \
  -d '{
    "name": "Interstellar 70mm IMAX",
    "price_paise": 45000,
    "per_user_limit": 4,
    "seats": ["A1", "A2", "A3", "A4", "B1", "B2"]
  }' | jq -r .id)
echo "Created Show: $SHOW_ID"
```

### Step 4: Book Seats (Idempotent Reservation)
```bash
RESERVATION_ID=$(curl -s -X POST https://<app>.onrender.com/reservations \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d "{
    \"show_id\": \"$SHOW_ID\",
    \"seats\": [\"A1\", \"A2\"],
    \"idempotency_key\": \"req-uuid-001\"
  }" | jq -r .id)
echo "Created Reservation: $RESERVATION_ID"
```

### Step 5: Test Idempotent Replay
Re-sending the exact same request returns `200 OK` and `Idempotent-Replayed: true`:
```bash
curl -i -X POST https://<app>.onrender.com/reservations \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d "{
    \"show_id\": \"$SHOW_ID\",
    \"seats\": [\"A1\", \"A2\"],
    \"idempotency_key\": \"req-uuid-001\"
  }"
```

### Step 6: Verify Atomic Show State
```bash
curl -s https://<app>.onrender.com/shows/$SHOW_ID | jq .
```
Shows:
```json
{
  "id": "...",
  "name": "Interstellar 70mm IMAX",
  "total_seats": 6,
  "available": 4,
  "held": 0,
  "confirmed": 2,
  "seats": {
    "A1": "confirmed",
    "A2": "confirmed",
    "A3": "available",
    "A4": "available",
    "B1": "available",
    "B2": "available"
  }
}
```

### Step 7: Cancel Reservation
```bash
curl -i -X POST https://<app>.onrender.com/reservations/$RESERVATION_ID/cancel \
  -H "Authorization: Bearer $TOKEN"
```

---

## 4. Makefile Commands & Targets

| Target | Description |
| :--- | :--- |
| `make up` | Start full Docker stack (`app` + `postgres:16`) |
| `make down` | Tear down all Docker containers, networks, and test volumes |
| `make test-db-up` | Start isolated fast PostgreSQL container on port 5433 |
| `make test` | Run all unit, integration, and concurrency tests (excludes chaos/canary) |
| `make test-unit` | Run fast unit tests only |
| `make test-conc` | Run all 10 high-concurrency burst tests (20 iterations per test) |
| `make test-chaos` | Run chaos & failure injection tests (DB kill, app kill, restarts) |
| `make lint` | Run Ruff linter and code formatting checks |
| `make burst` | Run full benchmark suite (`scripts/burst.py`) against `BASE_URL` |
| `make fuzz` | Run 60s random operations fuzzer (`scripts/fuzz.py`) against `BASE_URL` |
| `make reconcile` | Run mathematical state & Prometheus reconciliation (`scripts/reconcile.py`) |
| `make smoke` | Run end-to-end smoke check against `BASE_URL` |

---

## 5. Running Across Environments

### A. Local Development Environment
```bash
# 1. Create and activate venv
python -m venv .venv
source .venv/bin/activate  # Or .venv\Scripts\Activate.ps1 on Windows

# 2. Install dependencies
pip install -e ".[dev]"

# 3. Start local Postgres test database
make test-db-up

# 4. Run test suite
make test
```

### B. Standard Docker Compose Environment
```bash
docker compose up -d --build
make smoke BASE_URL=http://localhost:8000
```

### C. Resource-Constrained (Prodlike) Environment
Runs with 0.5 CPU and 256MB RAM limits to simulate budget cloud containers:
```bash
docker compose -f docker-compose.prodlike.yml up -d --build
pytest tests/chaos/test_prodlike.py -v
```

### D. PgBouncer Transaction Mode Environment
Validates compatibility with connection poolers operating in transaction mode:
```bash
docker compose -f docker-compose.pgbouncer.yml up -d --build
pytest tests/chaos/test_pgbouncer.py -v
```

---

## 6. High-Concurrency Burst & Reconciliation

Run the automated burst benchmark suite:
```bash
make burst BASE_URL=https://<app>.onrender.com
```

The script executes 6 sequential scenarios:
1. **Scenario A (Hot-Seat Storm)**: 500 distinct contenders targeting 1 single seat.
   - Result: Exactly 1 x `201 Created`, 499 x `409 Conflict` (`seat_taken`), 0 x `5xx`.
2. **Scenario B (Hot Set)**: 1,000 contenders picking from 10 hot seats.
   - Result: Exactly 10 x `201 Created`, 990 x `409 Conflict`, 0 x `5xx`.
3. **Scenario C (Stampede)**: 2,000 requests over Zipf distribution with 20% same-key retries.
   - Result: Zero 5xx errors; all retries replay cleanly.
4. **Scenario D (Per-User Limit Race)**: 1 user issuing 10 parallel reservation requests (limit 4).
   - Result: Exactly 4 x `201 Created`, 6 x `409 Conflict` (`per_user_limit`).
5. **Scenario E (Spoof & Ownership Protection)**: Foreign cancellation rejected with 404; body user_id spoofing ignored.
6. **Scenario F (Cancel / Rebook Race)**: 1 cancel contending with 50 parallel re-bookers.
   - Result: At most 1 winner takes the freed seat; 0 5xx.
7. **Final Reconciliation**:
   - Queries `GET /shows/{id}` and `/metrics`.
   - Mathematically verifies:
     $$\text{available} + \text{held} + \text{confirmed} == \text{total\_seats}$$
     $$\text{confirmed\_seats} == \sum(\text{seats in 201}) - \text{cancelled}$$
   - Compares Prometheus gauge `seats_available` with the API.

---

## 7. Cloud Deployment Blueprint (Render + Supabase)

### Deployment Architecture
- **Web Service**: Render Docker Service deployed in **Singapore** (`singapore`).
- **Database**: Supabase PostgreSQL deployed in **Singapore** (`ap-southeast-1`).
- *Colocation rationale*: Minimizes network round-trip latency inside database transactions, keeping row-lock hold times on hot seats under 15ms.

### Connection Configuration (Supavisor Session Mode)
- **Host**: Use the **Supavisor session-mode pooler URL** (port 5432) from the Supabase dashboard.
- **Do NOT** use the direct `db.<ref>.supabase.co` hostname, as it resolves via IPv6 only and can fail from cloud providers lacking full IPv6 routing.
- Set `DB_POOL_SIZE=15` (comfortably within Supabase connection limits).

### Free Tier Keep-Alive Guidance
If deployed on Render's free tier:
1. Set up a free monitor on [UptimeRobot](https://uptimerobot.com).
2. Configure HTTP `GET` probe targeting `https://<app>.onrender.com/readyz` every 5 minutes.
3. This prevents Render from spinning down after 15 minutes of inactivity and prevents Supabase from pausing the database after 7 days of inactivity.

---

## 8. Live Visual Seat Grid Dashboard

A self-contained, real-time visualizer is available at `/live` to monitor shows filling up during concurrent bursts:
- **URL Pattern**: `https://<app>.onrender.com/live?show=<show_id>`
- **Show Selector**: Visiting `/live` without a query parameter renders an input form to paste any Show UUID.
- **Features**:
  - Live header with currency formatting derived purely from integer paise math.
  - Large counter cards and an active invariant status badge (`available + held + confirmed == total`).
  - Interactive seat grid featuring natural alphanumeric ordering (`A1`, `A2`, `A10`), status flash highlights, and HTML5 canvas fallback for stadiums (> 2,000 seats).
  - Rate panel tracking per-second confirmation and decline velocities, and an automated red highlight if any 5xx occurs.
  - 60-second rolling sparkline of booking progress.
- **Database Protection**: Backed by a server-side 500ms in-process read cache (`SHOW_STATE_CACHE_MS`), preventing 100 simultaneous observer tabs from querying PostgreSQL more than twice per second.

