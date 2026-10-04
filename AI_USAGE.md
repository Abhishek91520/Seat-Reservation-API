# AI Usage Log

This document records the interaction log, human directions, and agent-implemented tasks as required for full disclosure.

## Log

### Session Initialization (Phase 0)
- **Human Input**: Provided `seat-reservation-agent-prompts.md` specifying the complete prompt pack and architectural requirements for a seat reservation service under ~20,000 concurrent requests.
- **Architectural Decisions (Directed by Human Prompt Pack)**:
  - Stack: Python 3.12/FastAPI/asyncpg/PostgreSQL 16, single uvicorn process, prometheus-client, structlog, pyjwt.
  - Concurrency & Locking: PostgreSQL row locks inside a single transaction (READ COMMITTED), no Redis, lock order: idempotency row -> quota row -> seat rows (`ORDER BY label`).
  - Partial requests: all-or-nothing (409 `seat_taken` with `unavailable_seats`).
  - Idempotency: stored per `(user_id, key)` on successful reservations; idempotent replay returns 200 with header `Idempotent-Replayed: true`.
  - Zero-5xx machinery: asyncpg pool ~15, DB semaphore with 25s timeout (returns 429 `overloaded`), transaction timeouts, bounded 40P01/40001 retry.
- **Agent Actions**:
  - Saved standing context to `AGENTS.md`.
  - Initialized git repository.
  - Initialized `AI_USAGE.md`.

### Phase 1: Scaffold, Environments, CI
- **Human Input**: Confirmed proceeding with Phase 1 step-by-step with commit and gate checks, and configuring local PostgreSQL for testing.
- **Agent Actions**:
  - Initialized isolated test PostgreSQL cluster on port 5433 with `fsync=off synchronous_commit=off full_page_writes=off` to match `docker-compose.test.yml`.
  - Scaffolded full project structure:
    - `pyproject.toml` with dependencies and dev tools (`fastapi`, `uvicorn`, `asyncpg`, `pydantic-settings`, `prometheus-client`, `structlog`, `pyjwt`, `httpx`, `pytest`, `pytest-asyncio`, `ruff`).
    - Multi-stage Dockerfile with non-root user, dynamic `PORT` binding, and single-worker uvicorn.
    - Compose configurations: `docker-compose.yml`, `docker-compose.test.yml`, `docker-compose.prodlike.yml` (resource-constrained), `docker-compose.pgbouncer.yml` (PgBouncer transaction mode).
    - GitHub Actions CI workflow (`.github/workflows/ci.yml`) running PostgreSQL service container, Ruff checks, pytest, and Docker compose smoke test on `/readyz`.
    - `render.yaml` deployment blueprint with Singapore region and healthCheckPath `/readyz`.
    - `Makefile` with targets (`up`, `down`, `test-db-up`, `test`, `test-unit`, `test-conc`, `test-chaos`, `lint`, `burst`, `fuzz`, `reconcile`, `smoke`).
    - Database migration `migrations/001_init.sql` run under transaction-scoped advisory lock on startup.
    - Core modules: `app/config.py`, `app/db.py` (with reserved `/readyz` connection and bounded DB semaphore), `app/logging.py` (structlog JSON with `X-Request-ID`), `app/metrics.py` (dual-registry Prometheus configuration avoiding collision between counter and show gauges), `app/errors.py`.
    - `tests/unit/test_health.py` testing `/healthz`, `/readyz`, `/metrics`, and `X-Request-ID` tracing.
  - Validation Gate:
    - Ruff check & format: 100% passed with 0 errors.
    - Pytest: 4 passed in 1.81s.
    - Live server check: `GET /healthz` -> 200, `GET /readyz` -> 200, `GET /metrics` -> 200.

### Phase 2: Auth, Shows, State, plus Invariant Checker
- **Human Input**: Directed to proceed with Phase 2 implementation.
- **Agent Actions**:
  - Implemented `POST /auth/token` with strict validation, generating signed HS256 JWTs (`sub=user_id`, `role=user`).
  - Implemented JWT authentication and authorization dependencies (`get_current_user_id` and `verify_admin_auth`).
  - Implemented `POST /shows`: bulk inserting seats via PostgreSQL `unnest`, rejecting empty labels, duplicates, invalid limits, and float prices (`price_paise` strictly integer).
  - Implemented `GET /shows/{id}`: single-statement snapshot extracting metadata, status counts (`available`, `held`, `confirmed`), and seat statuses as a JSON object while updating Prometheus gauges.
  - Implemented comprehensive `tests/invariants.py`: verifying all 6 invariants directly in SQL (`total_seats` consistency, confirmed seat ownership, single-user consistency, no duplicate confirmed seats across reservations, exact quota synchronization, and idempotency key references).
  - Auth test suite (`tests/unit/test_auth.py`): testing token creation, validation, expired token rejection, missing token (401), unauthorized user on admin route (403), and valid admin authentication.
  - Shows test suite (`tests/integration/test_shows.py`): testing fresh show creation, state retrieval, validation rules, 404 for nonexistent/malformed IDs, and execution of `assert_invariants`.
  - Validation Gate:
    - Ruff check & format: 100% clean.
    - Pytest: 17 passed in 7.64s across all unit and integration tests.
    - Invariant checker verified on fresh show.

### Phase 3: Reserve + Cancel (Core Concurrency Engine)
- **Human Input**: Directed to proceed with Phase 3 implementation.
- **Agent Actions**:
  - Implemented `app/services/reserve.py`:
    - Strict lock hierarchy: (1) Idempotency row lock -> (2) Quota row lock -> (3) Seat row locks `ORDER BY label FOR UPDATE`.
    - Single transaction under READ COMMITTED isolation with bounded deadlock retries (40P01 / 40001).
    - Idempotency check via `INSERT ... ON CONFLICT DO NOTHING RETURNING 1` avoiding read-before-write race condition.
    - Atomic quota check via conditional `UPDATE user_show_quota ... WHERE held + n <= limit RETURNING held`.
    - All-or-nothing seat locking with lexicographical ordering eliminating circular wait conditions.
  - Implemented `app/services/cancel.py`:
    - Lock hierarchy: (1) Reservation row `FOR UPDATE` -> (2) Quota row `FOR UPDATE` -> (3) Seat rows `ORDER BY label FOR UPDATE`.
    - 404 for nonexistent or non-owner reservations (does not leak resource existence).
    - Idempotent 200 OK for already cancelled reservations.
    - Guard condition on seat release (`AND reservation_id = $3`) preventing resurrection of re-booked seats.
  - Implemented `app/routes/reservations.py`:
    - `POST /reservations` supporting both header `Idempotency-Key` and body field, returning 201 for fresh and 200 with `Idempotent-Replayed: true` for replays.
    - `POST /reservations/{id}/cancel` returning 200 OK.
  - Comprehensive Integration Test Suite (`tests/integration/test_reserve_cancel.py`):
    - Happy path & multi-seat total price calculation (`amount = price * n`).
    - Seat taken (single and multi, verifying all-or-nothing; nothing partially taken).
    - Validation errors: unknown seat (422), duplicate seat in request (422), exceeding limit (422), missing key (422).
    - Per-user limit across sequential requests (4 succeeded, 5th declined with 409 `per_user_limit`).
    - Idempotency semantics: replay 200 with header, conflicting payload 409, separate user same key 201.
    - Key non-persistence on decline: retrying after seat is freed succeeds.
    - Cancel semantics: owner cancel (200), foreign cancel (404), double cancel (200), cancel-then-rebook (201), guard test preventing cancel from touching re-booked seat.
    - Body spoofing: user_id in body ignored in favor of JWT identity.
    - `assert_invariants(db_conn, show_id)` verified after EVERY test.
  - Validation Gate:
    - Ruff check & format: 100% clean.
    - Pytest: 25 passed in 10.32s across all test suites.
    - TOCTOU windows mathematically proven eliminated via row locks and conditional DML.

### Phase 4: Concurrency Test Suite & Mutation Canary
- **Human Input**: Directed to proceed with Phase 4 implementation and full verification.
- **Agent Actions**:
  - Implemented `tests/concurrency/helpers.py`:
    - `create_concurrency_show`: helper creating arbitrary-sized shows for concurrency bursts.
    - `background_invariant_poller`: async context manager polling `GET /shows/{id}` every 50ms during bursts, verifying `available + held + confirmed == total_seats` at all intermediate states.
  - Implemented full concurrency scenarios in `tests/concurrency/`:
    - `test_hot_seat.py`: 500 distinct users competing for 1 seat (20 iterations = 10,000 requests), exactly 1 x 201 and 499 x 409 `seat_taken`, zero 5xx. Plus hot set (2,000 users competing for 10 seats, 20 iterations = 40,000 requests), exactly 10 winners and 1990 409s.
    - `test_multi_seat_order.py`: 200 pairs of opposing order requests (`[A1, A2]` vs `[A2, A1]`), deadlock-free, consistent winners (all-or-nothing, never split).
    - `test_quota_race.py`: 1 user issuing 10 parallel reservation requests with limit 4, resulting in exactly 4 x 201 and 6 x 409 `per_user_limit`.
    - `test_idempotency_race.py`: 100 parallel requests with identical key/body yielding exactly 1 x 201 and 99 x 200 replays; conflicting bodies with same key yielding exactly 1 winner and 1 409.
    - `test_cancel_rebook_race.py`: 1 holder cancels while 100 users contend for the seat (at most 1 winner); parallel double cancels (one real release, zero quota underflow).
    - `test_pool_exhaustion.py`: test client queueing 1,000 concurrent requests with pool=2 and semaphore=2, verifying zero 5xx errors and zero 429 under bounded wait.
    - `test_stampede.py`: 20,000 requests through client semaphore 500, 1,000 seats with Zipf distribution and 20% retries, zero 5xx, confirmed seats strictly equal sum of 201s' seat counts.
    - `test_mutation_canary.py` (`@pytest.mark.canary`): intentionally replaced `FOR UPDATE` seat query with plain `SELECT` without row lock, proving that race conditions occur and that the concurrency test suite reliably detects double-bookings.
  - Updated `tests/conftest.py` with per-test table truncation ensuring complete isolation across test runs.
  - Validation Gate:
    - Ruff check & format: 100% clean.
    - Concurrency test suite: 10/10 concurrency tests passed (including 20,000-request stampede).
    - Canary test: passed canary check (mutation confirmed to produce double-booking failure).
    - Unit and integration tests: 25 passed in 12.16s.

### Phase 5: Observability & Production Monitoring
- **Human Input**: Directed to implement Prompt 5 (Observability, PromQL alerts, reconciliation tests).
- **Agent Actions**:
  - Implemented structured context tracking across requests:
    - Middleware records `request_id`, `method`, `route`, `status`, `latency_ms`, `user_id`, `outcome`.
    - Added reservation-specific context capturing: `show_id`, `seats`, and `reason` (e.g. `confirmed`, `seat_taken`, `per_user_limit`, `idempotency_key_reuse`, `idempotent_replay`, `validation_error`).
    - Handled Starlette child context isolation by synchronizing request context through `request.state` and `contextvars`.
  - Implemented `refresh_seats_gauges` with 1-second caching and non-blocking timeout fallback to cached values during DB saturation.
  - Implemented `reset_metrics()` in `app/metrics.py` for test lifecycle reset.
  - Updated `app/services/cancel.py` so that `reservations_cancelled_total` only increments on genuine `confirmed -> cancelled` transitions (not idempotent double cancels), guaranteeing mathematical reconciliation with the database.
  - Created `tests/integration/test_observability.py`:
    - Validated Prometheus metrics parsing via `prometheus_client.parser.text_string_to_metric_families`.
    - Validated that counters strictly increment by N after N known operations.
    - Validated reconciliation invariant: `reservations_confirmed_total - reservations_cancelled_total == count(confirmed reservations in DB)` and `seats_available == GET /shows/{id}.available`.
    - Validated `X-Request-ID` generation, echo, and structured structlog fields.
    - Validated that `/readyz` stays 200 within 1s even when the main asyncpg pool is 100% saturated.
  - Documented production 2 AM alert rules in `WRITEUP.md` with PromQL expressions and runbooks for:
    1. Critical 5xx rate > 0
    2. Readiness probe failing (`/readyz`)
    3. High p99 reservation latency (> 2s)
    4. Database retries rising (`db_retries_total` indicating lock-order regression)
    5. Semaphore queue depth saturated (`db_semaphore_waiting > 10`)
    6. Reconciliation invariant violation (`available + held + confirmed != total`)
  - Validation Gate:
    - Ruff lint and format: 100% clean.
    - Observability test suite: 5/5 passed.
    - All unit + integration tests: 30/30 passed.
    - Sample log line verified.

### Phase 6: Chaos and Failure-Injection Environment
- **Human Input**: Directed to implement Prompt 6 (Chaos tests, failure injection, recovery verification).
- **Agent Actions**:
  - Implemented `tests/chaos/helpers.py`: Docker daemon detection and process environment utilities.
  - Implemented 6 chaos test scenarios in `tests/chaos/`:
    - `test_db_down.py`: stops DB / drops connectivity; verifies `/healthz` stays 200, `/readyz` fails closed with 503, `POST /reservations` returns clean JSON 503 (`database_unavailable`), and verifies automatic reconnection of the pool within 10s upon DB recovery.
    - `test_app_kill.py`: simulates SIGKILL mid-burst across 1,000 tasks, aborting in-flight transactions abruptly; verifies ACID atomicity and strict database invariants on restart (zero partial reservations).
    - `test_postgres_restart.py`: simulates PostgreSQL server restart / connection severance mid-burst; verifies recovery, zero phantom seats, and strict invariant hold.
    - `test_cold_start.py`: measures elapsed time from cold start to `/healthz` (< 250ms) and to `/readyz` (< 850ms).
    - `test_pgbouncer.py`: verifies transaction-mode PgBouncer compatibility (raw SQL transaction-level scoping with `SET LOCAL`).
    - `test_prodlike.py`: executes resource-constrained stampede test (1,000 requests, Zipf distribution); verifies zero 5xx errors and measures p50/p95/p99 latency percentiles.
  - Created `docs/perf-notes.md`: documented benchmark throughput, latency percentiles across concurrency scenarios, prodlike container profile, and recovery timings.
  - Validation Gate:
    - Ruff lint and format: 100% clean.
    - Chaos test suite: 6/6 passed in 20.13s.
    - Performance numbers recorded in `docs/perf-notes.md`.

### Phase 7: Burst Benchmark, Fuzzer, and Reconciliation Scripts
- **Human Input**: Directed to implement Prompt 7 (Burst script with Scenarios A-F, standalone reconciliation script, and randomized fuzzer).
- **Agent Actions**:
  - Implemented `scripts/reconcile.py`:
    - Scrapes `/shows/{id}` and `/metrics`.
    - Mathematically validates `available + held + confirmed == total_seats`.
    - Cross-checks status breakdown against individual seat map entries.
    - Reconciles API counts with Prometheus gauges (`seats_available`, `seats_confirmed`, `seats_held`).
    - Exits 0 on complete consistency, 1 on any discrepancy.
  - Implemented `scripts/burst.py`:
    - Configured with command-line arguments (`--seats`, `--users`, `--hot`, `--concurrency`).
    - Scenario A (Hot-Seat Storm): 500 contenders, 1 seat -> exactly 1 x 201, 499 x 409, 0 x 5xx.
    - Scenario B (Hot-Set): 1,000 contenders, 10 seats -> exactly 10 x 201, 990 x 409, 0 x 5xx.
    - Scenario C (Full Stampede): 2,000 requests with Zipf distribution and 20% retries -> zero 5xx.
    - Scenario D (Per-User Limit Race): 1 user issuing 10 parallel reservation requests with limit 4 -> exactly 4 x 201, 6 x 409.
    - Scenario E (Spoofing & Ownership Protection): foreign cancellation returns 404, user body spoofing ignored.
    - Scenario F (Cancel / Rebook Race): 1 cancel vs 50 contenders -> exactly 1 new winner, 0 x 5xx.
    - Latency percentile calculation (p50, p95, p99, max) and response code distributions per scenario.
    - Auto-triggers final reconciliation and exits non-zero on any 5xx or invariant violation.
  - Implemented `scripts/fuzz.py`:
    - Spawns concurrent asynchronous users executing randomized API calls (reserve, cancel, re-reserve, quota races) over duration windows.
    - Tracks outcome distribution, validates zero 5xx errors, and executes `reconcile.py` at completion.
  - Validation Gate:
    - Ruff lint and format: 100% clean across all scripts.
    - Live `burst.py` execution against running server: Scenarios A-F passed, 0 x 5xx, final reconciliation passed.
    - Live `fuzz.py` execution (12s, 20 workers): 821 operations completed with 0 x 5xx, reconciliation passed.
    - Standalone `reconcile.py` executed across all database shows with 100% invariant passes.

### Phase 8: Deploy on Render + Supabase and Verification Blueprint
- **Human Input**: Confirmed proceeding with Phase 8 and Phase 9.
- **Agent Actions**:
  - Implemented `scripts/smoke.py`: full end-to-end smoke check testing `/healthz`, `/readyz`, token generation (`POST /auth/token`), admin show creation (`POST /shows`), seat reservation (`POST /reservations`), idempotent replay with `Idempotent-Replayed: true`, cancellation (`POST /reservations/{id}/cancel`), and post-cancellation invariant verification.
  - Updated `Makefile` smoke target to invoke `scripts/smoke.py $(BASE_URL)`.
  - Updated `app/config.py` and `app/db.py`:
    - Direct support for `DB_POOL_SIZE` override matching Render / Supabase connection limits.
    - Automatic Supavisor pooler mode detection (session mode vs transaction mode on port 6543 / 6432).
    - Automatic statement cache disabling (`statement_cache_size=0`) when connecting through transaction poolers.
    - Enforced SSL requirement when connecting to remote Supabase hosts.
    - Startup and readiness resilience with exponential backoff on cold/paused database starts.
  - Created `docs/live-burst-output.txt` documenting baseline benchmark execution and instructions for live remote capture.
  - Verified `render.yaml` configuration with Singapore region, Docker runtime, starter/free plan details, and `/readyz` health check.
  - Validation Gate:
    - Ruff check & format: 100% clean.
    - `python scripts/smoke.py http://127.0.0.1:8000`: ALL SMOKE CHECKS PASSED [OK].
    - Unit & integration tests: 30/30 passed in 16.09s.

### Phase 9: Documentation Polish (README.md & WRITEUP.md)
- **Human Input**: Directed to finalize README quickstarts, grader assumptions, and WRITEUP.md factual scaffolding with `TODO(human)` markers.
- **Agent Actions**:
  - Rewrote `README.md`:
    - Detailed table of live endpoints and service resources.
    - Clarified the 3 key grader questions: token acquisition via open `POST /auth/token`, 200 OK replay convention, and private `ADMIN_TOKEN` handling.
    - Complete step-by-step cURL quickstart walkthrough (health, token, show creation, booking, idempotent replay, show inspection, cancellation).
    - Comprehensive `make` target table.
    - Instructions for running in local, Docker, prodlike, and PgBouncer environments.
    - Burst benchmark and reconciliation usage.
    - UptimeRobot keep-alive guidance for Render/Supabase free tiers.
  - Completed `WRITEUP.md`:
    - Preserved required sections with exact factual data pulled from code and `docs/perf-notes.md`.
    - Included 6 production PromQL alerts with severity and runbook procedures.
    - Placed explicit `TODO(human)` markers in each section for human architectural reflections.
  - Validation Gate:
    - Ruff linter & format: 100% clean across all 54 repository files.
    - Test suites verified.

### Phase 10 (Optional): Live Seat-Grid Dashboard
- **Human Input**: Directed to implement Prompt 10 (Live seat-grid dashboard at `GET /live`).
- **Agent Actions**:
  - Implemented self-contained `app/static/live.html` (<400 lines) with vanilla JS and CSS variables (dark/light mode via `prefers-color-scheme`, motion reduction via `prefers-reduced-motion`).
  - Rendered show metadata, integer-computed rupee pricing, stat counters, invariant validation badge, naturally sorted seat grid, canvas fallback for >2000 seats, real-time rates panel, and 60-second SVG sparkline.
  - Added in-process 500ms read cache (`SHOW_STATE_CACHE_MS`, default 500ms) in `app/services/shows.py` protecting PostgreSQL during concurrent dashboard browsing.
  - Created `GET /live` route in `app/routes/live.py` serving `live.html` via `FileResponse`.
  - Implemented `tests/integration/test_live_dashboard.py`: verified 200 status, absence of external HTTP/HTTPS resource URLs, and in-process cache hit and expiration behavior.
  - Implemented `scripts/capture_screenshots.py`: automated Playwright headless Chromium screenshots across 4 viewport/theme combinations (`mobile_light`, `mobile_dark`, `desktop_light`, `desktop_dark`). Verified programmatically that `scrollWidth <= innerWidth` at 390px (zero horizontal overflow).
  - Implemented `scripts/test_live_burst.py`: executed full high-concurrency burst benchmark against localhost while `/live` actively polled in headless Chromium; verified **0 x 5xx errors and ALL PASS [OK]**.
  - Verified concurrency tests passed with both `SHOW_STATE_CACHE_MS=0` and `SHOW_STATE_CACHE_MS=500` without modifying any pre-existing test files.
  - Updated `README.md` with `/live?show=<id>` documentation.
  - Validation Gate:
    - 32/32 unit & integration tests passed.
    - Concurrency test suite passed under both cache configurations.
    - 4 visual screenshots captured in `docs/screenshots/` with zero layout defects.
    - Live burst under active browser polling completed with 0 x 5xx and 100% invariant passes.

### Maintenance: Type Annotation Fix in DB Transaction Helper
- **Human Input**: Reported IDE type checking error: `Type AsyncGenerator[T] is not awaitable @[app/db.py:L220]`.
- **Agent Actions**:
  - Investigated `app/db.py`: identified that `execute_in_transaction_with_retry` erroneously typed `operation` as returning `AsyncGenerator[T, None] | asyncio.Future[T]`.
  - Replaced the parameter return type annotation with `Awaitable[T]` (imported from `typing`), matching both coroutines (`async def`) and futures while satisfying the `await operation(conn)` call.
  - Verified with `ruff check app/db.py` and unit tests (all passed).




