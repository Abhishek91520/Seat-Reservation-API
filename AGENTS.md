# Standing Context & System Requirements

You are building a take-home service: **a seat-reservation API that stays correct under ~20,000 concurrent requests**, deployed publicly and observable. Graders clone the repo, deploy-check it, and fire their own bursts at the live URL. They grade the running service, not the write-up.

### Hard requirements
1. No seat is ever confirmed to two users. For a hot seat with N contenders: exactly one 201, all others 409.
2. Zero 5xx during a burst. Domain declines are 4xx.
3. `available + held + confirmed == total_seats` at all times, to the unit.
4. Idempotency: same key and same body returns the original reservation; same key and different seats returns 409.
5. Per-user limit (default 4) holds under concurrency (10 parallel reserves, limit 4, at most 4 held).
6. Identity comes only from the auth token. Spoofed body fields are ignored. Cancel works only for the owner.
7. Money is integer paise. Never floats anywhere.

### Stack (fixed, do not substitute)
- Python 3.12, FastAPI, uvicorn (single process, uvloop), asyncpg (raw SQL, no ORM), PostgreSQL 16
- `prometheus-client`, `structlog` (JSON logs), `pyjwt` (HS256)
- Tests: pytest, pytest-asyncio, httpx. Lint: ruff.
- Docker + docker compose. CI: GitHub Actions.
- **Single uvicorn process on purpose.** `prometheus-client` counters are per-process, so multiple workers would give wrong metrics. The DB is the bottleneck, not Python. Record this in WRITEUP.md as a deliberate tradeoff.

### Design decisions (already made, follow exactly)
- **Atomic decision lives in Postgres row locks inside one transaction** (READ COMMITTED). No read-then-write. No app-level locks. No Redis.
- **Lock order, always:** idempotency row, then quota row, then seat rows `ORDER BY label`. Cancel uses the same order (reservation row, quota row, seats sorted). A single global order is what makes it deadlock-free.
- **Partial requests: all-or-nothing.** If any requested seat is unavailable, nothing is taken, and the response is 409 `seat_taken` with `unavailable_seats: [...]`.
- **Release model: explicit `POST /reservations/{id}/cancel`.** Reserve confirms immediately (status `confirmed`). The `held` state exists in the schema and API output but is always 0 (reserved for a TTL extension).
- **Decline precedence:** validation 4xx, then idempotency, then per_user_limit, then seat checks.
- **Idempotency scope:** per `(user_id, key)`. Only *successful* reservations are stored. A decline rolls back the whole transaction, including the key row, so a retry after a seat frees up can succeed.
- **Idempotent replay returns 200** (not 201) with header `Idempotent-Replayed: true` and a body identical to the original. This way "exactly one 201 per seat" holds even with retries. Document it in the README.
- **Auth:** `POST /auth/token {"user_id": "u123"}` returns a JWT (`sub=user_id`, `role=user`). Open, demo-grade, documented in the README. Admin = `Authorization: Bearer <ADMIN_TOKEN>` (env var) for `POST /shows`. Everything else requires a user JWT. The `user_id` in any request body is ignored.
- **Error shape:** `{"error": {"code": "...", "message": "...", "request_id": "..."}}`. Codes: `seat_taken`, `per_user_limit`, `idempotency_key_reuse`, `unknown_seat`, `validation_error`, `unauthorized`, `forbidden`, `not_found`, `overloaded`.
- **Zero-5xx machinery:**
  - Bounded `asyncio.Semaphore` in front of DB work (size = pool size), with an acquire timeout of about 25s. If it times out, return 429 `overloaded` with `Retry-After`. Requests wait rather than fail.
  - asyncpg pool: min=max=~15 (configurable via env). Reserve one extra connection for `/readyz`.
  - Per transaction: `SET LOCAL lock_timeout='8s'; SET LOCAL statement_timeout='15s'; SET LOCAL idle_in_transaction_session_timeout='15s'`.
  - Bounded retry (max 3, with jitter) on SQLSTATE 40P01 and 40001, counted in a metric. It should never fire if lock order is right, but it is the safety net.
  - A global exception handler returns clean JSON. A DB-down 503 is acceptable only when the DB is actually down.

### Schema (Postgres)
```sql
CREATE TABLE shows (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  name text NOT NULL,
  price_paise bigint NOT NULL CHECK (price_paise > 0),
  per_user_limit int NOT NULL DEFAULT 4 CHECK (per_user_limit BETWEEN 1 AND 100),
  total_seats int NOT NULL CHECK (total_seats > 0),
  created_at timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE seats (
  show_id uuid NOT NULL REFERENCES shows(id),
  label text NOT NULL,
  status text NOT NULL DEFAULT 'available' CHECK (status IN ('available','held','confirmed')),
  reservation_id uuid NULL,
  user_id text NULL,
  PRIMARY KEY (show_id, label),
  CHECK ((status = 'available') = (reservation_id IS NULL))
);
CREATE TABLE reservations (
  id uuid PRIMARY KEY,
  show_id uuid NOT NULL REFERENCES shows(id),
  user_id text NOT NULL,
  seats text[] NOT NULL,
  amount_paise bigint NOT NULL,
  status text NOT NULL CHECK (status IN ('confirmed','cancelled')),
  created_at timestamptz NOT NULL DEFAULT now(),
  cancelled_at timestamptz NULL
);
CREATE TABLE user_show_quota (
  show_id uuid NOT NULL, user_id text NOT NULL,
  held int NOT NULL DEFAULT 0 CHECK (held >= 0),
  PRIMARY KEY (show_id, user_id)
);
CREATE TABLE idempotency_keys (
  user_id text NOT NULL, key text NOT NULL,
  request_hash text NOT NULL,
  reservation_id uuid NULL,
  response_body jsonb NULL,
  created_at timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (user_id, key)
);
```
The CHECK constraints are the last line of defense, and tests must prove the app never trips them.

### Reserve algorithm (one transaction, READ COMMITTED)
0. Validate: 1 to per_user_limit seats, no duplicates, labels non-empty, idempotency key present (`Idempotency-Key` header or body field). Load the show (404 if missing; the row is immutable, so it can be cached).
1. `INSERT INTO idempotency_keys(user_id,key,request_hash) VALUES (...) ON CONFLICT DO NOTHING RETURNING 1`.
   - A concurrent same-key insert blocks on the unique index until the first transaction ends. If it committed, this insert conflicts, so `SELECT` the row: hash differs gives 409 `idempotency_key_reuse`; hash equal gives 200 replay with the stored `response_body`.
   - `request_hash = sha256(canonical JSON of {show_id, sorted seats})`.
2. Quota: `INSERT INTO user_show_quota(show_id,user_id) VALUES(...) ON CONFLICT DO NOTHING;` then `UPDATE user_show_quota SET held = held + $n WHERE show_id=$1 AND user_id=$2 AND held + $n <= $limit RETURNING held`. Zero rows means rollback and 409 `per_user_limit`. The row lock serializes one user's parallel requests.
3. Seats: `SELECT label, status FROM seats WHERE show_id=$1 AND label = ANY($2) ORDER BY label FOR UPDATE`.
   - Row count != n: 422 `unknown_seat`.
   - Any status != 'available': rollback and 409 `seat_taken` with `unavailable_seats`.
   - Otherwise `UPDATE seats SET status='confirmed', reservation_id=$r, user_id=$u WHERE show_id=$1 AND label = ANY($2) AND status='available'`. Assert `rowcount == n`, else rollback and treat as a bug.
4. `INSERT INTO reservations(... status 'confirmed', amount_paise = price * n)`, then `UPDATE idempotency_keys SET reservation_id, response_body`. Commit and return 201.

### Cancel algorithm
Transaction: `SELECT ... FROM reservations WHERE id=$1 FOR UPDATE`. Not found or not owner gives 404 (do not leak existence). Already cancelled gives 200 (idempotent). Then lock the quota row, then lock seats `ORDER BY label FOR UPDATE`. `UPDATE seats SET status='available', reservation_id=NULL, user_id=NULL WHERE show_id=$1 AND label=ANY($2) AND reservation_id=$3` (the `reservation_id` guard means it can never free a seat now owned by someone else). Decrement quota by n, mark the reservation `cancelled`.

### GET /shows/{id}
Counts and per-seat statuses from **one statement** (one snapshot), so the invariant is exact. Return `total_seats`, `available`, `held`, `confirmed`, and `seats: {label: status}`.

### Observability spec
- `GET /healthz`: liveness, no DB, always 200 if the process is up.
- `GET /readyz`: runs `SELECT 1` on the reserved connection with a 1s timeout. 200 if OK, **503 if not** (fails closed). It must never fail just because the main pool is saturated.
- `GET /metrics` (Prometheus text):
  - `reservations_confirmed_total` (counter), `seats_confirmed_total` (counter)
  - `reservations_declined_total{reason="seat_taken|per_user_limit|idempotency_key_reuse|idempotent_replay"}`
  - `reservations_cancelled_total`
  - `seats_available{show_id}`, `seats_confirmed{show_id}`, `seats_held{show_id}` gauges, computed from the DB at scrape time and cached 1s
  - `http_requests_total{route,method,status}`, `http_request_duration_seconds{route}` histogram (use the route template as the label, not the raw path)
  - `db_pool_in_use`, `db_semaphore_waiting`, `db_retries_total{sqlstate}`, `inflight_requests`
- Logs: structlog JSON to stdout. Middleware sets `request_id` (accept `X-Request-ID` or generate a uuid4, echo it in the response header, bind it to contextvars). One line per request: `request_id, method, route, status, latency_ms, user_id, outcome`. Reserve logs also include `show_id, seats, reason`.

### Repo layout
```
app/ main.py config.py db.py auth.py metrics.py logging.py errors.py
     routes/{shows,reservations,auth,health}.py  services/{reserve,cancel,shows}.py
migrations/001_init.sql
tests/ unit/ integration/ concurrency/ chaos/ conftest.py invariants.py
scripts/burst.py  scripts/fuzz.py  scripts/reconcile.py
docker/ Dockerfile  docker-compose.yml  docker-compose.test.yml
        docker-compose.prodlike.yml  docker-compose.pgbouncer.yml
.github/workflows/ci.yml  Makefile  render.yaml  README.md  WRITEUP.md  AGENTS.md
```

### Working rules for you, the agent
- Small commits with meaningful messages after each passing gate. Never rewrite history.
- Never claim something works without running it and showing the output. Never mock the database in any concurrency test.
- If a requirement conflicts with the design above, stop and ask. Do not silently change the design.
- Keep a running `AI_USAGE.md` log of what you were asked to do and which decisions the human made (for the honest disclosure section).
