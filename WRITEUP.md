# Architectural Design & Reliability Writeup

This document outlines the architectural decisions, concurrency safety proofs, failure-mode analysis, and observability strategy for the Seat Reservation Service.

---

## 1. Atomic Decision & Single-Transaction Model
- **Core Mechanism**: Decision lives entirely in PostgreSQL row locks inside a single transaction under `READ COMMITTED` isolation. No application-level distributed locks (e.g., Redlock) and no Redis caching in the critical booking path.
- **Why READ COMMITTED**: Under PostgreSQL `READ COMMITTED`, `SELECT ... FOR UPDATE` re-evaluates the updated row version once a concurrent locking transaction commits. If another transaction took the seat, the subsequent waiter instantly sees `status = 'confirmed'` and triggers a clean rollback and domain decline.
- **Tradeoff: Single Uvicorn Process**:
  - The application is deliberately run as a single process under Uvicorn/uvloop.
  - Reason: `prometheus-client` in-memory counters and gauges are process-bound. Running multi-worker uvicorn without a shared aggregator produces inaccurate scraping metrics. In this architecture, PostgreSQL row locks and disk I/O are the fundamental bottleneck, not the asynchronous Python event loop.
- TODO(human): Add reflections on database roundtrips vs stored procedures (PL/pgSQL).

---

## 2. Deadlock Avoidance in Multi-Seat Reservations
- **Global Lock Ordering**:
  1. Idempotency row: `INSERT ... ON CONFLICT DO NOTHING` on `(user_id, key)`
  2. User quota row: `UPDATE user_show_quota ...`
  3. Seat rows: `SELECT ... FROM seats WHERE show_id=$1 AND label = ANY($2) ORDER BY label FOR UPDATE`
- **Why Ordering Prevents Deadlocks**:
  - If Transaction 1 requests seats `[A1, A2]` and Transaction 2 requests `[A2, A1]`, both transactions sort the requested labels lexicographically to `[A1, A2]`.
  - Transaction 1 acquires `A1`; Transaction 2 blocks attempting to acquire `A1`. Neither transaction can acquire `A2` until `A1` is resolved. The circular wait condition is mathematically eliminated.
- **Safety Net**:
  - `SET LOCAL lock_timeout='8s';`
  - Bounded retry (max 3 attempts with exponential jitter) on `SQLSTATE 40P01` (deadlock detected) and `SQLSTATE 40001` (serialization failure). Metric tracked via `db_retries_total{sqlstate}`.
- TODO(human): Document observations from concurrency test suite runs.

---

## 3. Idempotency (Storage, Exactly-Once, Conflicting Bodies)
- **Key Storage**: Keys are scoped to `(user_id, key)` and only persisted when the transaction successfully commits. A declined reservation rolls back the key row, allowing subsequent retry when seats become available.
- **Concurrent Race on Same Key**:
  - `INSERT INTO idempotency_keys(user_id, key, request_hash) VALUES (...) ON CONFLICT DO NOTHING RETURNING 1;`
  - A concurrent duplicate request blocks on the Postgres unique index lock until the initial transaction commits or rolls back.
  - If initial transaction committed: insert returns 0 rows. Transaction reads stored row:
    - If `request_hash == hash(canonical_json)`: returns `200 OK` with header `Idempotent-Replayed: true` and the stored response body.
    - If `request_hash != hash(canonical_json)`: declines with `409 Conflict` (`idempotency_key_reuse`).
- TODO(human): Add notes on payload hashing and collision considerations.

---

## 4. Holds and Expiry Model
- **Current Model**: Immediate confirmation. Every valid reservation transitions seats directly from `available` to `confirmed`.
- **Schema Extensibility**: The `seats` table schema supports `held` in its check constraints (`CHECK (status IN ('available','held','confirmed'))`), and the state counts output `held = 0`.
- **Future TTL Hold Extension**:
  - Adding `held_until timestamptz NULL` on `seats`.
  - A background sweeper running `SELECT ... FOR UPDATE SKIP LOCKED` where `status = 'held' AND held_until < now()`.
  - Seat-ownership guards on release ensuring an expired hold never reverts a seat that has already been confirmed to another user.
- TODO(human): Detail considerations for client hold lifecycles (e.g. 5-minute checkout timers).

---

## 5. Consistency vs. Availability Under Partition
- **CAP Theorem Stance**: Strict **CP** (Consistency & Partition Tolerance).
- **Rationale**: Double-booking a physical seat is an unrecoverable domain failure. If the database connection is lost or severed, the system fails closed immediately (`503 Service Unavailable` on `/readyz` and bounded database timeouts), refusing to make split-brain decisions.
- **Overload Shedding**: Bounded DB concurrency semaphore (depth = 15) with a 25-second queue. Excess concurrent traffic beyond queuing capacity receives `429 Too Many Requests` (`overloaded`) with `Retry-After`, protecting PostgreSQL from connection exhaustion.
- TODO(human): Discuss geographic multi-region replication vs single-master constraints.

---

## 6. Observability & 2 AM Production Alerts
- **Prometheus Metrics Exposed**:
  - `reservations_confirmed_total`, `seats_confirmed_total`
  - `reservations_declined_total{reason="..."}`
  - `reservations_cancelled_total`
  - `seats_available{show_id}`, `seats_confirmed{show_id}`, `seats_held{show_id}`
  - `http_requests_total{route, method, status}`
  - `http_request_duration_seconds{route}`
  - `db_pool_in_use`, `db_semaphore_waiting`, `db_retries_total{sqlstate}`, `inflight_requests`

### Production PromQL Alert Rules (2 AM Runbook)
1. **Critical 5xx Rate Spikes**:
   ```promql
   sum(rate(http_requests_total{status=~"5.."}[1m])) / sum(rate(http_requests_total[1m])) > 0.01
   ```
   *Action*: Investigate unhandled exceptions or database network failure.

2. **Readiness Probe Failing**:
   ```promql
   probe_success{instance=~".*/readyz"} == 0
   ```
   *Action*: PostgreSQL instance unresponsive or connection pool dropped. Check Render/Supabase health.

3. **Database Retries Rising (Lock-Order Regression)**:
   ```promql
   rate(db_retries_total[2m]) > 0
   ```
   *Action*: Deadlock detected (`40P01`). Check if any newly added endpoint queries seats without `ORDER BY label`.

4. **Semaphore Queue Depth Saturated**:
   ```promql
   db_semaphore_waiting > 100
   ```
   *Action*: Requests are backing up waiting for database connections; scale database instance or investigate long-running locks.

5. **Show Invariant Violation**:
   ```promql
   (seats_available + seats_held + seats_confirmed) != ignoring(status) on(show_id) total_seats
   ```
   *Action*: Severe data corruption alert. Trigger reconciliation job immediately.

- TODO(human): Add team escalation paths and pager policy details.

---

## 7. AI Usage Disclosure
- Directed by Human:
  - System architecture and tech stack constraints (Python, FastAPI, asyncpg, PostgreSQL 16, single uvicorn worker).
  - Explicit lock hierarchy (`idempotency -> quota -> seats ordered by label`).
  - Zero-5xx machinery (bounded semaphore, retry decorators, local transaction timeouts).
  - Invariant definitions and test scenarios.
- Executed by Agent:
  - Repository scaffolding, configuration, schema migrations, and Docker configurations.
  - Multi-registry Prometheus metrics configuration and structlog JSON logging middleware.
  - Invariant assertion engine and test fixtures.
  - Ruff linting, test suite execution, and CI workflow definition.

---

## 8. What I'd Do Next
- TODO(human): Document performance optimizations (e.g. PL/pgSQL single-roundtrip reservation function, distributed Redis read cache for show listings, Grafana dashboard dashboards).
