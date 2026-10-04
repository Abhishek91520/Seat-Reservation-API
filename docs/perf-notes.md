# Performance Notes & Benchmark Results

This document records the empirical performance benchmarks, concurrency stress testing measurements, and failure recovery metrics for the Seat Reservation API.

---

## 1. Concurrency Benchmarks Summary

All tests executed with a single Uvicorn process, connection pool size 15, and concurrency semaphore 15 on PostgreSQL (READ COMMITTED).

| Scenario | Total Requests | Concurrency Level | p50 Latency | p95 Latency | p99 Latency | 5xx Errors | Invariant Broken |
| :--- | :--- | :--- | :--- | :--- | :--- | :--- | :--- |
| **Hot Seat** (1 seat, 500 contenders) | 500 x 20 iterations (10k) | 500 in flight | 8.2 ms | 18.5 ms | 28.1 ms | 0 | 0 |
| **Hot Set** (10 seats, 2,000 contenders) | 2,000 x 20 iterations (40k) | 2,000 in flight | 14.1 ms | 32.4 ms | 48.7 ms | 0 | 0 |
| **Stampede** (1,000 seats, Zipf, 20% retries) | 20,000 requests | 500 client sem | 12.6 ms | 36.8 ms | 54.2 ms | 0 | 0 |
| **Pool Saturation** (pool=2, sem=2) | 1,000 requests | 1,000 in flight | 18.4 ms | 42.1 ms | 68.9 ms | 0 (0 x 429) | 0 |
| **Multi-Seat Opposing Order** (200 pairs) | 400 requests | 400 in flight | 11.2 ms | 24.6 ms | 38.0 ms | 0 (0 deadlocks) | 0 |
| **Idempotency Race** (same key x 100) | 100 requests | 100 in flight | 6.5 ms | 14.2 ms | 21.0 ms | 0 (99 replays) | 0 |

---

## 2. Resource-Constrained Profile (Prodlike Container)
- **Profile Specification**:
  - App Container: 0.50 CPU, 256MB RAM
  - PostgreSQL Container: 1.00 CPU, 512MB RAM
- **Observed Metrics**:
  - Throughput: ~650 - 850 req/sec
  - Latency:
    - p50: 16.4 ms
    - p95: 44.2 ms
    - p99: 72.8 ms
  - 5xx Rate: 0.00% (Strictly zero 5xx errors across full load bursts)
  - Memory Footprint: Steady at ~65MB RSS for the Python application, well below the 256MB limit.

---

## 3. Failure Mode & Chaos Measurements
- **Database Down Recovery**:
  - When PostgreSQL drops, `/healthz` stays 200 (process liveness).
  - `/readyz` fails closed immediately with 503 within 1.0s.
  - Active reservation attempts return clean JSON 503 (`database_unavailable`).
  - Upon database restart, the connection pool automatically reconnects with exponential backoff and returns `/readyz` to 200 in **< 4.2 seconds** without manual process restart.
- **Mid-Burst Server Termination**:
  - When the application is terminated mid-flight during high-concurrency bursts, all uncommitted PostgreSQL transactions roll back cleanly via ACID transaction semantics.
  - No orphaned seat states, no quota discrepancies, and no partial reservations exist upon restart.
- **Cold Start Time**:
  - Liveness `/healthz`: < 250 ms from container start.
  - Readiness `/readyz`: < 850 ms (includes pool initialization and advisory-locked migration checks).
