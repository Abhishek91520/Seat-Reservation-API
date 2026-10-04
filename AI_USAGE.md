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
