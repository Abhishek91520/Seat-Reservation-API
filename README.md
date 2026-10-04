# High-Concurrency Seat Reservation API

A production-grade, highly observable seat reservation service built to remain strictly correct under ~20,000 concurrent requests without double-booking or inconsistent states.

## Live Service Information
- **Live URL**: `https://<render-app-slug>.onrender.com` (configured via Render deployment)
- **Health Check**: `GET /healthz`
- **Readiness Check**: `GET /readyz`
- **Prometheus Metrics**: `GET /metrics`

## Architectural Overview & Guarantees
1. **Zero Double-Selling**: Exactly one contender receives `201 Created` for any individual seat; all other simultaneous contenders receive `409 Conflict` (`seat_taken`).
2. **Strict Invariant**: `available + held + confirmed == total_seats` at all times, verified per snapshot.
3. **Deadlock Freedom**: Global locking hierarchy enforced strictly across all transactions:
   `Idempotency Row -> User Quota Row -> Seats (ORDER BY label)`.
4. **Exactly-Once Semantics**: Idempotent replay of an identical request returns `200 OK` with the header `Idempotent-Replayed: true`. Reusing an idempotency key with conflicting parameters returns `409 Conflict` (`idempotency_key_reuse`).
5. **Per-User Quotas**: Row-level locking on user quota rows guarantees users cannot exceed limits even during simultaneous bursts.
6. **Zero 5xx Under Load**: Bounded `asyncio.Semaphore` (pool size = 15) with a 25-second wait queue. Requests wait rather than fail; excessive load responds with clean `429 Too Many Requests` (`Retry-After`).

---

## Authentication & Authorization Assumptions

### Grader / User Tokens
- Tokens are minted via the demo endpoint:
  ```bash
  curl -X POST http://localhost:8000/auth/token \
    -H "Content-Type: application/json" \
    -d '{"user_id": "user_12345"}'
  ```
- Returns a signed JWT (`sub="user_12345"`, `role="user"`, signed with HS256).
- Every authenticated endpoint derives user identity **strictly from the JWT `sub` claim**. Body-supplied `user_id` fields are completely ignored to prevent impersonation and spoofing.

### Admin Token
- Administrative endpoints (`POST /shows`) require:
  `Authorization: Bearer <ADMIN_TOKEN>`
- The token value is configured via the `ADMIN_TOKEN` environment variable and shared privately for grading.

### Replay Status Code Assumption
- Replay of an existing successful reservation returns **HTTP 200** with the header `Idempotent-Replayed: true`. This ensures that the invariant "exactly one 201 per confirmed seat" holds across retries.

---

## Local Development & Testing

### Prerequisites
- Python 3.12 or 3.13
- PostgreSQL 16 (or Docker Compose)

### Quickstart with Docker Compose
```bash
# Start full application stack (app + postgres:16)
docker compose up -d

# Verify readiness
curl -i http://localhost:8000/readyz
```

### Fast Test Database
For development and test suites, launch the dedicated ephemeral test Postgres container:
```bash
make test-db-up
make test
```

### Running Test Tiers
```bash
# Run all unit and integration tests
make test

# Run unit tests only
make test-unit

# Run concurrency proof suite
make test-conc

# Run chaos and failure-injection tests
make test-chaos
```

### Linting & Formatting
```bash
make lint
```

### High-Concurrency Benchmark Burst
```bash
make burst BASE_URL=http://localhost:8000
```
