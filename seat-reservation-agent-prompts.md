# Seat Reservation at Scale: Agent Prompt Pack

How to use: paste **PROMPT 0** once as the agent's standing context (save it as `AGENTS.md` / `CLAUDE.md` in the repo). Then paste PROMPT 1, 2, ... one at a time. Each phase has a **GATE**: the agent must run it and show green output before you move on. Commit after every phase (they read the history).

Rule for you: read every diff for Phases 3 to 5. You will be asked to extend this live, so the locking, idempotency and quota logic must be something you can explain line by line.

---

## PROMPT 0: Standing context (paste once)

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

---

## PROMPT 1: Scaffold, environments, CI

Create the repo skeleton from PROMPT 0: layout, `pyproject.toml`, Dockerfile (multi-stage, non-root, slim base, runs uvicorn single-process), and all compose files:

- `docker-compose.yml`: app + postgres:16, with a healthcheck, volume, and env from `.env.example`.
- `docker-compose.test.yml`: **postgres only**, on port 5433, `tmpfs` data dir, and `fsync=off synchronous_commit=off full_page_writes=off` for fast tests. Tests connect from the host via `TEST_DATABASE_URL`.
- `docker-compose.prodlike.yml`: the app limited to `cpus: 0.5, mem_limit: 256m` and postgres limited to `cpus: 1, mem_limit: 512m`, to mimic free-tier limits.
- `docker-compose.pgbouncer.yml`: adds pgbouncer in transaction mode in front of postgres, to verify the app works with a pooled managed DB (asyncpg needs `statement_cache_size=0` there; make that an env flag).

Makefile targets: `up`, `down`, `test-db-up`, `test`, `test-unit`, `test-conc`, `test-chaos`, `lint`, `burst BASE_URL=...`, `fuzz`, `reconcile`, `smoke`.
GitHub Actions: postgres service container, ruff, pytest (all tiers except chaos), `docker build`, then `docker compose up -d` and a `/readyz` smoke check.
Migrations: run `migrations/001_init.sql` on startup (idempotent, `IF NOT EXISTS`, guarded by a transaction-scoped advisory lock so cold starts don't race).
Add only `/healthz` and `/readyz` for now.

**GATE:** `make test-db-up && make test` passes, `docker compose up` gives healthy containers, and `curl localhost:8000/readyz` returns 200.

---

## PROMPT 2: Auth, shows, state, plus the invariant checker

Implement `POST /auth/token`, JWT verification, admin auth, `POST /shows` (bulk insert seats via `unnest`, reject duplicates and empty labels, a sane cap such as 100k seats), and `GET /shows/{id}` (single-statement counts).

Then write `tests/invariants.py`. It is the most important test file, and every later test calls it.
`assert_invariants(conn, show_id)` must check ALL of these directly in SQL:
1. `available + held + confirmed == total_seats == count(seats)`.
2. Every confirmed seat has a `reservation_id` pointing to a reservation with `status='confirmed'` whose `seats` array contains it.
3. Every confirmed reservation's seats are all marked `confirmed` for the same user.
4. No seat label appears in two confirmed reservations of the same show.
5. `user_show_quota.held` equals the actual confirmed seat count per user, and is `<= per_user_limit`.
6. Every `idempotency_keys.reservation_id` exists.

Tests: validation, auth (missing, bad, expired token gives 401; a user token on an admin route gives 403), the state endpoint on a fresh show.

**GATE:** all green, and the invariant checker passes on a fresh show.

---

## PROMPT 3: Reserve + cancel (the core)

Implement `services/reserve.py` and `services/cancel.py` exactly as specified in PROMPT 0, including the lock order, SQL, error codes, the bounded deadlock retry, and the semaphore. Keep it readable. Comment *why* each statement is race-free.

Write integration tests (real Postgres, httpx `ASGITransport`, sequential first):
- happy path, multi-seat, amount = price * n
- seat taken (single and multi, nothing partially taken)
- unknown seat (422), duplicate seats in request (422), more seats than the limit (422)
- per-user limit across several requests (4 ok, 5th gives 409 `per_user_limit`)
- idempotent replay gives 200 with an identical body and the `Idempotent-Replayed` header; same key with different seats gives 409; the same key on a different user is independent
- a decline does not store a key (retry after the seat frees up succeeds)
- cancel: owner OK, other user gets 404, double cancel gets 200, cancel then rebook works, and a cancel does not touch a seat re-booked by someone else (guard test)
- body spoofing: `{"user_id": "victim"}` in the body is ignored

After EVERY test, call `assert_invariants`.

**GATE:** all green, and the agent explains in the PR/commit message why there are no TOCTOU windows.

---

## PROMPT 4: Concurrency test suite (the real proof)

Write `tests/concurrency/`. Use real Postgres, real pool (size 15), and `asyncio.gather`. Each test runs **20 iterations** on fresh shows to hunt flakes. Call `assert_invariants` after each and also **during** the burst (a background task polling `GET /shows/{id}` and asserting counts sum to `total_seats` every 50ms).

1. **Hot seat:** 500 distinct users, same seat. Exactly 1 x 201, 499 x 409 `seat_taken`, zero 5xx.
2. **Hot set:** 2000 users, each picks 1 seat from 10 hot seats. Exactly 10 winners, the rest 409.
3. **Stampede:** 20,000 requests (run through a client semaphore of 500), 1000 seats, Zipf-weighted choice, 20% of requests are same-key retries. Zero 5xx, invariants hold, confirmed seats == sum of 201s' seat counts.
4. **Multi-seat opposite order:** users A requests [A1,A2] while B requests [A2,A1], 200 pairs concurrently. No deadlock, no 5xx, and for each pair the winners are consistent (all-or-nothing, never one seat each).
5. **Per-user limit race:** 1 user x 10 parallel reserves of different seats, limit 4. Exactly 4 x 201 and 6 x 409 `per_user_limit`. Quota row == 4.
6. **Idempotency race:** the same key, same body, x 100 parallel. Exactly 1 x 201, 99 x 200 replay, exactly 1 reservation row. Same key with 2 different bodies in parallel: exactly one body wins, the other gets 409.
7. **Cancel/rebook race:** one holder cancels while 100 users try to book that seat. At most 1 winner, and invariants hold. Also run a parallel double-cancel (one real release, no quota underflow).
8. **Pool exhaustion:** run the app with pool=2 and semaphore=2, then 1000 concurrent. Zero 5xx (waiting is fine, 429 `overloaded` is a last resort, and the test asserts the rate of 429 is 0 under a 25s wait).
9. **Mutation check:** temporarily replace the `FOR UPDATE` select with a plain read-then-update (in a test-only branch or monkeypatched function) and prove tests 1, 2 and 4 FAIL. This proves the tests can catch a double-sell. Keep it as `tests/concurrency/test_mutation_canary.py`, marked `@pytest.mark.canary`.

**GATE:** `make test-conc` green 20 out of 20 iterations, and the canary fails when the lock is removed.

---

## PROMPT 5: Observability

Implement the logging middleware, `/metrics`, `/readyz` with the reserved connection, and all metrics from PROMPT 0. Add tests:
- the metrics endpoint parses (use `prometheus_client.parser`)
- after N known operations the counters equal N
- **reconciliation test:** after a mixed burst, `reservations_confirmed_total - reservations_cancelled_total` equals the count of confirmed reservations in the DB (on a fresh process), and `seats_available` equals `GET /shows/{id}.available`
- every response has an `X-Request-ID`, a client-supplied id is echoed, and the log lines carry it
- `/readyz` stays 200 while the main pool is fully saturated

Document the 2am alerts in WRITEUP.md as PromQL (do not build the alerting). Include: 5xx rate > 0, `readyz` failing, p99 reserve latency, `db_retries_total` rising (lock-order bug), semaphore queue depth, and the reconciliation gauge (`available + held + confirmed - total != 0`).

**GATE:** all green, `curl /metrics` shows the metric families, and a sample log line is pasted in the commit message.

---

## PROMPT 6: Chaos and failure-injection environment

Write `tests/chaos/` (marker `chaos`, run via `make test-chaos`, using docker compose from Python or shell):
1. **DB down:** stop postgres. Expect `/healthz` 200, `/readyz` 503, and reserve returns a clean JSON 503 (no stack trace, no hang beyond the timeouts). Start postgres again. Expect `/readyz` back to 200 within 10s with no manual restart (the pool must recover).
2. **App killed mid-burst:** `docker kill` the app during a 5000-request burst, restart it, and run `assert_invariants`. Committed state must be consistent (a transaction either fully landed or did not).
3. **Postgres restart mid-burst:** same, with `docker restart postgres`. After recovery, invariants hold and there are no phantom seats.
4. **Cold start:** `docker compose down`, bring everything up, and fire a request the moment `/healthz` is green. Measure time to `/readyz`.
5. **PgBouncer variant:** run the concurrency tests 1, 3 and 6 against `docker-compose.pgbouncer.yml`.
6. **Resource-constrained variant:** run test 3 against `docker-compose.prodlike.yml` and report p50/p99 and the 5xx count.

**GATE:** all pass. Record the p99 and throughput numbers in `docs/perf-notes.md` (you will quote them in the write-up).

---

## PROMPT 7: Burst script, fuzzer, reconcile

`scripts/burst.py <BASE_URL> [--users 20000 --seats 2000 --hot 10 --concurrency 500]`. Python asyncio + httpx with a connection limit. Steps:
1. Mint an admin session via `ADMIN_TOKEN` env, create a fresh show.
2. Run scenarios in order, printing a table for each: **A** hot-seat storm (500 users, 1 seat), **B** hot set, **C** full stampede with 20% same-key retries, **D** per-user limit (1 user x 10 parallel), **E** spoof test (body user_id spoofed; cancel another user's reservation, expecting 404), **F** cancel/rebook race.
3. Print the outcome distribution: confirmed (201), replay (200), declined by reason (409 by code, 422, 429), **5xx**, other, and the p50/p95/p99 latency.
4. Final reconciliation: GET the show, check `available+held+confirmed == total`, `confirmed_seats == sum(seats in 201 responses) - cancelled`, and scrape `/metrics` and compare `seats_available` with the API. Print PASS/FAIL per check and **exit non-zero on any failure or any 5xx**.
5. `make burst BASE_URL=https://...` wraps it.

`scripts/fuzz.py`: 50 simulated users run random operations (reserve 1 to 3 seats, retry with the same key, cancel a random own or foreign reservation, reserve hot seats) for 60s against a base URL, then run `reconcile.py`. `reconcile.py` uses the public API and metrics only (no DB access), so it works against the live deploy.

**GATE:** `make burst BASE_URL=http://localhost:8000` is all PASS with 0 5xx, and the same under `docker-compose.prodlike.yml`.

---

## PROMPT 8: Deploy on Render + Supabase and verify

Target: **Render** (Docker web service, Singapore) + **Supabase** Postgres (Singapore, `ap-southeast-1`). Same region for both, because every round trip inside a transaction lengthens the hot-seat lock hold time.

**App changes**
- Dockerfile `CMD` must bind to `${PORT:-8000}` on `0.0.0.0` (Render injects `PORT`).
- Env vars: `DATABASE_URL`, `JWT_SECRET`, `ADMIN_TOKEN`, `DB_POOL_SIZE` (default 15), `DB_STATEMENT_CACHE_SIZE` (default 100; must be 0 if the URL is Supavisor transaction mode, port 6543). Log the effective pool size and mode at startup. SSL required for the Supabase host.
- **Connection string:** use the Supavisor **session-mode** pooler URL (IPv4-compatible, behaves like a direct connection). Do NOT use the `db.<ref>.supabase.co` direct host: it is IPv6-only and may not resolve from Render. Keep `DB_POOL_SIZE` comfortably below the plan's connection limit shown in the Supabase dashboard.
- **Cold DB tolerance:** on startup retry the initial connect with exponential backoff for up to 60s (a paused or slow Supabase project must not crash-loop the app). Meanwhile `/healthz` returns 200 and `/readyz` returns 503.
- **Migrations:** run at startup under `pg_advisory_xact_lock(<const>)` inside a transaction (the transaction-scoped lock works in both pooler modes). Statements are `IF NOT EXISTS`.

**Render config (`render.yaml` blueprint)**
- `type: web`, `runtime: docker`, `region: singapore`, `healthCheckPath: /readyz`, `autoDeploy: true`.
- Secrets use `sync: false`. Instance plan: `starter` ($7, no spin-down, more CPU than free's 0.1 CPU). Keep a `plan: free` option commented in the file with a note on its 15-minute spin-down.

**Keep-alive (human step, document in the README):** a free UptimeRobot monitor on `https://<app>.onrender.com/readyz` every 5 minutes. It keeps the app awake if on free, and it touches the DB so Supabase does not auto-pause after a week of inactivity.

**Verification, in order**
1. Clean clone: `git clone <repo> /tmp/x && cd /tmp/x && docker compose up --build` reaches healthy, and the Render build uses the same Dockerfile.
2. `make smoke BASE_URL=https://<app>.onrender.com` (health, ready, token, create show, reserve, replay, cancel).
3. `make burst` against the live URL **twice**: once right after a deploy or restart (cold) and once warm.
4. If any 5xx appears or p99 is very high, check Render CPU and memory, and the Supabase pooler limit. Fix by lowering `DB_POOL_SIZE`, upgrading the instance, or using a nearer region. Record what you changed in `docs/perf-notes.md`.
5. Save the outputs to `docs/live-burst-output.txt`. Render logs need a dashboard login, so document how to view them and plan a short **screen recording** of the live logs during the burst.

**GATE:** both live bursts are all PASS with 0 5xx.

**Your manual steps (the agent cannot do these):** create the Supabase project (Singapore) and copy the session-pooler string; create the Render service from the blueprint and set the three secrets; set up the UptimeRobot monitor; record the logs video; email the live URL, token instructions and `ADMIN_TOKEN`.

---

## PROMPT 9: README + WRITEUP skeleton

README: live URL, the `ADMIN_TOKEN` question (see below), how to get a token, a curl quickstart, `make` targets, how to run each test environment, how to run the burst, and the `/metrics`, `/healthz`, `/readyz` links.

WRITEUP.md: create **headings and factual scaffolding only** for these sections, and leave `TODO(human)` markers for my own reasoning: atomic decision; deadlock avoidance in multi-seat; idempotency (storage, exactly-once, same-key-different-body); holds and expiry model; consistency vs availability under partition; observability and the 2am pages; AI usage; what I'd do next. Pull the true facts from the code and `docs/perf-notes.md`. Do not invent claims or numbers.

---

# Your own checklist (not for the agent)

**Questions to settle early (email Karan if needed)**
- How do graders get tokens? The open `POST /auth/token` is my assumption. Put the exact curl in the README and state the assumption. Ask for confirmation if you can.
- Replay status: I chose 200 (so "exactly one 201 per seat" holds). Say so in the README.
- Whether an admin token needs to be shared with them. Put it in the submission email, not in the repo.

**Be able to explain live (they will extend it)**
1. Why `SELECT ... FOR UPDATE ORDER BY label` is race-free under READ COMMITTED (the blocked transaction re-reads the committed row after the lock is released).
2. Why a global lock order removes deadlock, and what 40P01 retry is for.
3. Why the idempotency insert-first approach is exactly-once (the unique-index wait).
4. Why quota is a row you can lock rather than `count(*)` (a count would race).
5. Why a single process, and what changes when you scale out (multiprocess metrics, or push to a shared collector).
6. What the canary mutation test proves.
7. What you would add for TTL holds (`held_until`, a sweeper using `SKIP LOCKED`, a guard so a release never resurrects a re-booked seat).

**Suggested time budget (about 1 day)**
Phase 1 to 2: 1.5h, Phase 3: 2h, Phase 4: 2h, Phase 5: 1h, Phase 6 to 7: 1.5h, Phase 8: 1.5h, Phase 9: 1h.

**Honest AI disclosure (write it yourself)**
Directed: stack, lock order, all-or-nothing, replay status, test matrix. Decided by the agent: boilerplate, SQL typing, test scaffolding. Anything you changed after reading a diff is worth naming.

**Optional upgrade after everything passes**
Move the reserve transaction into one PL/pgSQL function to cut round trips inside the lock (this shortens lock hold time against a remote DB). Only do it if the live burst's p99 is poor, and re-run the full suite. Keep the Python version as the reference.
