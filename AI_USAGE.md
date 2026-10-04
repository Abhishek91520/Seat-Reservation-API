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
