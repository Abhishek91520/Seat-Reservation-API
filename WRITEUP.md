# Architectural Design & Reliability Writeup

This document outlines the architectural decisions, concurrency safety proofs, failure-mode analysis, empirical benchmark measurements, and observability strategy for the Seat Reservation Service.

---

## 1. Atomic Decision & Single-Transaction Model

### Factual Implementation
- **Single ACID Transaction**: The reservation and cancellation decisions live entirely within PostgreSQL row locks in a single transaction under `READ COMMITTED` isolation.
- **No Redis / No Application Locks**: There are no distributed locks (e.g. Redlock), no cache layers in the write path, and no read-then-write windows in Python memory.
- **Why READ COMMITTED Works**: Under PostgreSQL `READ COMMITTED`, when a query executes `SELECT ... FOR UPDATE`, any row currently locked by a concurrent transaction causes the current query to block. Once the locking transaction commits, PostgreSQL re-evaluates the query condition against the committed version of the row. If the competing transaction confirmed the seat, the waiter's `status = 'available'` check immediately fails, triggering a clean rollback and returning `409 Conflict` (`seat_taken`).
- **Single Process Uvicorn Tradeoff**:
  - The application runs as a single Uvicorn process with `uvloop`.
  - *Rationale*: Prometheus counters and histograms in `prometheus-client` are in-memory and process-bound; running multiple workers without a shared multiprocess directory or external collector produces inaccurate metric scrapes.
  - *Bottleneck Reality*: The fundamental bottleneck of high-concurrency booking is row-level contention and database transaction commit latency, not Python's async event loop.
- **Performance Facts (from `docs/perf-notes.md`)**:
  - Memory RSS is steady at ~65MB under full burst load.
  - Cold start to `/healthz` takes < 250ms; `/readyz` is ready in < 850ms (including database pool warmup and advisory lock migration checks).

- `TODO(human)`: Add personal reflections on the tradeoffs between client-side multi-query transactions vs moving the reserve sequence into a PL/pgSQL stored procedure to eliminate network roundtrips inside the lock hold window.

---

## 2. Deadlock Avoidance in Multi-Seat Reservations

### Factual Implementation
- **Deterministic Global Lock Hierarchy**:
  All transactions that acquire multiple resources enforce an identical acquisition order:
  1. **Idempotency Row**: `INSERT INTO idempotency_keys(user_id, key, request_hash) VALUES (...) ON CONFLICT DO NOTHING RETURNING 1`
  2. **User Quota Row**: `UPDATE user_show_quota SET held = held + $n ...`
  3. **Seat Rows**: `SELECT label, status FROM seats WHERE show_id=$1 AND label = ANY($2) ORDER BY label FOR UPDATE`
  Cancellation follows the mirror sequence: (1) Reservation row `FOR UPDATE`, (2) Quota row `FOR UPDATE`, (3) Seat rows `ORDER BY label FOR UPDATE`.
- **Mathematical Elimination of Deadlock**:
  - Suppose User A books `["A1", "A2"]` and User B concurrently books `["A2", "A1"]`.
  - Both transactions sort requested labels lexicographically to `["A1", "A2"]`.
  - User A acquires `A1`; User B blocks attempting to acquire `A1`. User B cannot acquire `A2` first.
  - Circular wait condition is eliminated ($P_1 \to R_1 \to P_2 \to R_2 \to P_1$ cannot form).
- **Safety Net**:
  - Transaction-level timeouts configured on every connection:
    ```sql
    SET LOCAL lock_timeout = '8s';
    SET LOCAL statement_timeout = '15s';
    SET LOCAL idle_in_transaction_session_timeout = '15s';
    ```
  - Bounded retry loop (max 3 retries with exponential jitter) on `SQLSTATE 40P01` (deadlock detected) and `SQLSTATE 40001` (serialization failure). Metric tracked via `db_retries_total{sqlstate}`.
- **Empirical Verification**:
  - Concurrency test `test_multi_seat_order.py` ran 200 pairs of conflicting opposing-order requests across 20 iterations: **0 deadlocks, 0 retries triggered, 0 5xx errors**.

- `TODO(human)`: Add observations on why sorting seat labels alphabetically is both necessary and sufficient for preventing graph cycles in database lock managers.

---

## 3. Idempotency (Storage, Exactly-Once, Conflicting Bodies)

### Factual Implementation
- **Storage Scope**: Idempotency is keyed strictly by `(user_id, key)`.
- **Failure Non-Persistence**: Idempotency records are written in the same transaction as the reservation. If a booking declines (e.g., seat taken or quota exceeded), the transaction aborts and rolls back the key row. A client retrying after a seat is released can therefore succeed.
- **Insert-First Unique Index Lock**:
  - On receiving a request, the engine attempts an atomic insert:
    ```sql
    INSERT INTO idempotency_keys(user_id, key, request_hash)
    VALUES ($user_id, $key, $request_hash)
    ON CONFLICT DO NOTHING RETURNING 1;
    ```
  - If two identical requests arrive concurrently, the second transaction blocks at the unique index on `(user_id, key)` until the first completes.
  - If the first transaction committed: the second transaction's insert returns 0 rows. It fetches the existing row:
    - If `stored_hash == sha256(canonical_json)`: returns **HTTP 200 OK** with `Idempotent-Replayed: true` and the stored `response_body`.
    - If `stored_hash != sha256(...)`: aborts and returns **HTTP 409 Conflict** (`idempotency_key_reuse`).
- **Response Code Distinction**:
  - First execution returns `201 Created`.
  - Replays return `200 OK` (with header `Idempotent-Replayed: true`).
  - *Invariant Preserved*: For any confirmed seat, there is exactly one `201 Created` emitted across the entire cluster lifecycle.

- `TODO(human)`: Discuss considerations around key expiration (TTL on idempotency keys) and hashing canonical JSON structures across diverse client SDKs.

---

## 4. Holds and Expiry Model

### Factual Implementation
- **Immediate Confirmation**: In the current implementation, seat reservations transition immediately from `available` to `confirmed`.
- **Schema Compatibility**: The database schema explicitly provisions the `held` state:
  ```sql
  CHECK (status IN ('available', 'held', 'confirmed'))
  ```
  The state snapshot endpoint `GET /shows/{id}` calculates and exposes `held` (currently returning 0).
- **TTL Extension Design**:
  1. Add column `held_until timestamptz NULL` to `seats` table.
  2. Implement `POST /shows/{id}/hold` with 5-minute expiration timestamp.
  3. A background sweeper running:
     ```sql
     SELECT show_id, label FROM seats
     WHERE status = 'held' AND held_until < now()
     FOR UPDATE SKIP LOCKED;
     ```
  4. Release guard: `UPDATE seats SET status='available', reservation_id=NULL WHERE show_id=$1 AND label=$2 AND reservation_id=$held_res_id`. The reservation ID check ensures an expired hold can never unseat a re-booked confirmation.

- `TODO(human)`: Elaborate on user experience tradeoffs between temporary cart holds (e.g. Ticketmaster 8-minute timer) versus flash-sale instant confirmation models.

---

## 5. Consistency vs. Availability Under Partition

### Factual Implementation
- **CAP Theorem Stance**: Strict **CP** (Consistency & Partition Tolerance).
- **Zero Double-Selling Policy**: Selling the same physical seat twice is an irrecoverable real-world failure. When network partitions or database outages occur, the system fails closed immediately (`503 Service Unavailable` on `/readyz`), refusing to make uncoordinated booking decisions.
- **Overload Shedding Mechanism**:
  - An `asyncio.Semaphore` (capacity = 15, matching the asyncpg pool size) sits in front of all transactional work.
  - Acquire timeout is set to 25.0 seconds. Requests queue rather than fail during brief traffic spikes.
  - If the queue is saturated and times out, the system returns **HTTP 429 Too Many Requests** (`overloaded`) with a `Retry-After: 2` header, protecting PostgreSQL from thrashing and connection exhaustion.
- **Prodlike Performance (from `docs/perf-notes.md`)**:
  - Under resource constraints (0.5 CPU, 256MB RAM), the service sustained 650–850 req/sec with 0% 5xx errors and 0 invariant violations.

- `TODO(human)`: Contrast single-region strongly consistent PostgreSQL against multi-region distributed databases (e.g. CockroachDB, Spanner) in terms of cross-region WAN latencies during lock hold times.

---

## 6. Observability & 2 AM Production Alerts

### Factual Implementation
- **Structured JSON Logging**: Every request emits a single structured JSON line containing:
  - `request_id` (from `X-Request-ID` or generated UUID4)
  - `method`, `route`, `status`, `latency_ms`
  - `user_id` (extracted strictly from JWT `sub`)
  - `outcome` (`confirmed`, `declined`, `error`)
  - Reservation details: `show_id`, `seats`, and decline `reason` (`seat_taken`, `per_user_limit`, `idempotency_key_reuse`, `validation_error`).
- **Readiness Isolation**:
  - `/healthz` tests process liveness (no DB queries; always 200 if container is up).
  - `/readyz` probes PostgreSQL via a dedicated, reserved connection with a 1.0s timeout. It continues reporting accurate database health even when the application connection pool is 100% saturated.

### Production PromQL Alert Rules (2 AM Runbook)

#### 1. Critical 5xx Rate > 0
```promql
sum(rate(http_requests_total{status=~"5.."}[1m])) > 0
```
- **Severity**: P1 Critical (Immediate Page).
- **Runbook**: Inspect structlog logs for `unhandled_exception` or `database_unavailable`. Check PostgreSQL connectivity, pool health, and container disk space.

#### 2. Readiness Probe Failing (`/readyz`)
```promql
probe_success{instance=~".*/readyz"} == 0 or http_requests_total{route="/readyz", status="503"} > 0
```
- **Severity**: P1 Critical (Immediate Page).
- **Runbook**: Dedicated health connection failed to execute `SELECT 1` within 1.0s. Verify whether PostgreSQL is paused, out of memory, or exhausted of connection slots.

#### 3. High p99 Reservation Latency
```promql
histogram_quantile(0.99, sum(rate(http_request_duration_seconds_bucket{route="/reservations"}[5m])) by (le)) > 2.0
```
- **Severity**: P2 Warning.
- **Runbook**: 99th percentile booking latency exceeds 2.0s. Inspect `db_semaphore_waiting` and active row locks in `pg_locks` to identify lock contention on hot shows.

#### 4. Database Retries Rising (Lock-Order Regression Bug)
```promql
sum(rate(db_retries_total[2m])) > 0
```
- **Severity**: P1 Critical.
- **Runbook**: PostgreSQL detected deadlocks (`40P01`) or serialization errors (`40001`). Because all queries enforce global deterministic ordering (`idempotency -> quota -> seats ORDER BY label`), this alert signifies a code regression that introduced an out-of-order lock acquisition.

#### 5. Semaphore Queue Depth Saturated
```promql
db_semaphore_waiting > 10
```
- **Severity**: P2 Warning.
- **Runbook**: Inbound requests have exhausted the connection pool (`max_size=15`) and are filling the 25-second wait buffer. Risk of 429 shedding. Scale pool size or add read replicas for `/shows/{id}` queries.

#### 6. State Reconciliation Invariant Violation
```promql
(seats_available + seats_held + seats_confirmed) != on(show_id) group_left() (shows_total_seats)
```
- **Severity**: P0 Catastrophic (Immediate Page).
- **Runbook**: State corruption detected: `available + held + confirmed != total_seats`. Immediately pause booking traffic for the show. Run `python scripts/reconcile.py $BASE_URL` to identify mismatched seat records.

- `TODO(human)`: Add specific internal on-call escalation schedules and pager duty routing.

---

## 7. AI Usage Disclosure

- **Directed by Human**:
  - Core tech stack selection (Python 3.12, FastAPI, asyncpg, PostgreSQL 16, single uvicorn worker).
  - Explicit lock hierarchy (`idempotency -> quota -> seats ORDER BY label`).
  - Zero-5xx machinery design (bounded semaphore, 25s queue, transaction timeouts, 40P01 retries).
  - All-or-nothing partial booking semantics.
  - Idempotent replay HTTP status 200 with header.
  - Invariant definitions and 10-prompt test gating strategy.
- **Decided / Implemented by Agent**:
  - Boilerplate code, Pydantic models, SQL schema definitions, and migration scripts.
  - Dual Prometheus collector registries resolving collector collision between counters and show gauges.
  - Starlette context variable synchronization via `request.state` to ensure nested route context reaches parent logging middleware.
  - Concurrency test suites (20 iterations per test, background 50ms invariant pollers).
  - Chaos failure injection harnesses and benchmark script implementation.

- `TODO(human)`: Review and adjust personal commentary on AI tool collaboration.

---

## 8. What I'd Do Next

### Architectural Next Steps
1. **Single-Roundtrip PL/pgSQL Function**:
   Move the reserve transaction into a PostgreSQL stored function (`fn_reserve_seats(show_id, user_id, seats, idem_key)`). This condenses the 4 roundtrips (idempotency, quota, seat lock, reservation insert) into a single database call, slashing lock hold duration by ~60% over WAN connections (e.g. Render to Supabase).
2. **Read-Through Cache for Show Availability**:
   Deploy an asynchronous Redis cache or in-process cache (100–250ms TTL) for `GET /shows/{id}` to offload seat map reads from the primary transactional database during massive ticket drops.
3. **Automated Grafana Dashboards**:
   Provision pre-built Grafana dashboards importing the Prometheus metrics (`db_semaphore_waiting`, `seats_confirmed`, `http_request_duration_seconds`).

- `TODO(human)`: Add personal vision for future product extensions (e.g., seat map visual layout, tiered pricing, waiting room queues).
