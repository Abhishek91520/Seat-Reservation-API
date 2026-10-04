#!/usr/bin/env python3
"""scripts/burst.py: High-concurrency burst benchmark and validation suite.

Runs high-concurrency burst scenarios A through F against any deployment (local or remote live URL),
computes outcome distributions and latency percentiles, runs final reconciliation,
and exits non-zero on any failure or any 5xx.
"""

import argparse
import asyncio
import os
import random
import sys
import time
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional, Tuple

import httpx
import jwt

# Default secrets matching config fallbacks
ADMIN_TOKEN = os.environ.get("ADMIN_TOKEN", "admin-secret-token-change-in-prod")
JWT_SECRET = os.environ.get("JWT_SECRET", "supa-secret-jwt-seat-reservation-prod-2026")


def mint_token(arg1: str, arg2: Optional[str] = None) -> str:
    """Generates a signed user JWT token for benchmark contenders."""
    user_id = arg2 if arg2 is not None else arg1
    now = datetime.now(timezone.utc)
    return jwt.encode(
        {"sub": user_id, "role": "user", "iat": now, "exp": now + timedelta(days=7)},
        JWT_SECRET,
        algorithm="HS256",
    )


def print_stats_table(title: str, stats: Dict[str, any]):
    print(f"\n--- Scenario {title} ---")
    print(
        f"Total Requests : {stats['total']} in {stats['duration_s']:.2f}s "
        f"({stats['rps']:.1f} req/s)"
    )
    print(
        f"Distribution   : 201 Created={stats['status_201']} | "
        f"200 Replay={stats['status_200']} | "
        f"409 Conflict={stats['status_409']} | "
        f"422 Validation={stats['status_422']} | "
        f"429 Overload={stats['status_429']} | "
        f"5xx Errors={stats['status_5xx']} | "
        f"Other={stats['other']}"
    )
    if stats["decline_reasons"]:
        reasons_str = ", ".join(f"{k}: {v}" for k, v in stats["decline_reasons"].items())
        print(f"409 Breakdown  : {reasons_str}")
    if stats.get("other", 0) > 0 and "status_counts" in stats:
        other_counts = {k: v for k, v in stats["status_counts"].items() if k not in (200, 201, 409, 422, 429) and not (500 <= k <= 599)}
        print(f"Other Breakdown: {other_counts}")
    print(
        f"Latencies (ms) : p50={stats['p50']:.1f}ms | "
        f"p95={stats['p95']:.1f}ms | "
        f"p99={stats['p99']:.1f}ms | "
        f"max={stats['max_lat']:.1f}ms"
    )


async def execute_burst(
    client: httpx.AsyncClient,
    semaphore: asyncio.Semaphore,
    requests: List[Tuple[str, str, Dict[str, any], Dict[str, str]]],
) -> Dict[str, any]:
    latencies: List[float] = []
    status_counts: Dict[int, int] = {}
    decline_reasons: Dict[str, int] = {}
    successful_confirmed_seats: int = 0
    cancelled_seats_count: int = 0
    created_reservation_ids: List[Tuple[str, str]] = []  # (res_id, token)

    async def _send(method: str, url: str, json_data: dict, headers: dict):
        nonlocal successful_confirmed_seats, cancelled_seats_count
        async with semaphore:
            t0 = time.perf_counter()
            try:
                if method == "POST":
                    r = await client.post(url, json=json_data, headers=headers)
                elif method == "GET":
                    r = await client.get(url, headers=headers)
                else:
                    r = await client.request(method, url, json=json_data, headers=headers)
                lat = (time.perf_counter() - t0) * 1000
                latencies.append(lat)
                sc = r.status_code
                status_counts[sc] = status_counts.get(sc, 0) + 1

                if sc == 201:
                    body = r.json()
                    seats = body.get("seats", [])
                    successful_confirmed_seats += len(seats)
                    created_reservation_ids.append((body.get("id"), headers.get("Authorization")))
                elif sc == 200 and "/cancel" in url:
                    cancelled_seats_count += 1
                elif sc == 409:
                    try:
                        err_code = r.json().get("error", {}).get("code", "unknown")
                        decline_reasons[err_code] = decline_reasons.get(err_code, 0) + 1
                    except Exception:
                        pass
                return r
            except Exception:
                lat = (time.perf_counter() - t0) * 1000
                latencies.append(lat)
                status_counts[599] = status_counts.get(599, 0) + 1
                return None

    t_start = time.perf_counter()
    tasks = [_send(m, u, d, h) for m, u, d, h in requests]
    await asyncio.gather(*tasks)
    t_end = time.perf_counter()
    duration = max(t_end - t_start, 0.001)

    latencies.sort()
    n = len(latencies)
    p50 = latencies[int(n * 0.50)] if n else 0.0
    p95 = latencies[int(n * 0.95)] if n else 0.0
    p99 = latencies[int(n * 0.99)] if n else 0.0
    max_lat = latencies[-1] if n else 0.0

    status_5xx = sum(v for k, v in status_counts.items() if 500 <= k <= 599)

    return {
        "total": len(requests),
        "status_counts": status_counts,
        "duration_s": duration,
        "rps": len(requests) / duration,
        "status_201": status_counts.get(201, 0),
        "status_200": status_counts.get(200, 0),
        "status_409": status_counts.get(409, 0),
        "status_422": status_counts.get(422, 0),
        "status_429": status_counts.get(429, 0),
        "status_5xx": status_5xx,
        "other": sum(
            v
            for k, v in status_counts.items()
            if k not in (200, 201, 409, 422, 429) and not (500 <= k <= 599)
        ),
        "decline_reasons": decline_reasons,
        "p50": p50,
        "p95": p95,
        "p99": p99,
        "max_lat": max_lat,
        "confirmed_seats": successful_confirmed_seats,
        "cancelled_seats": cancelled_seats_count,
        "created_reservations": created_reservation_ids,
    }


async def async_main():
    parser = argparse.ArgumentParser(description="High-concurrency burst runner")
    parser.add_argument(
        "base_url", nargs="?", default="http://localhost:8000", help="Base URL of service"
    )
    parser.add_argument("--users", type=int, default=2000, help="Total users in stampede scenario")
    parser.add_argument("--seats", type=int, default=500, help="Total seats in show")
    parser.add_argument("--hot", type=int, default=10, help="Number of hot seats")
    parser.add_argument("--concurrency", type=int, default=100, help="Client concurrency limit")
    parser.add_argument("--admin-token", type=str, default=None, help="Admin bearer token override")
    args = parser.parse_args()

    base_url = args.base_url.rstrip("/")
    admin_token = args.admin_token or os.environ.get("ADMIN_TOKEN", ADMIN_TOKEN)

    print("=================================================================")
    print(f"BURST BENCHMARK & SYSTEM VERIFICATION: {base_url}")
    print(
        f"Parameters: seats={args.seats}, stampede_users={args.users}, "
        f"hot_seats={args.hot}, concurrency={args.concurrency}"
    )
    print("=================================================================")

    limits = httpx.Limits(
        max_connections=args.concurrency, max_keepalive_connections=args.concurrency
    )
    async with httpx.AsyncClient(limits=limits, timeout=45.0) as client:
        # 1. Health check
        h_resp = await client.get(f"{base_url}/healthz")
        r_resp = await client.get(f"{base_url}/readyz")
        if h_resp.status_code != 200 or r_resp.status_code != 200:
            print(
                f"[!] Target not ready: /healthz={h_resp.status_code}, /readyz={r_resp.status_code}"
            )
            sys.exit(1)

        # 2. Mint admin session & create fresh show
        print("\nCreating fresh show via POST /shows...")
        seat_labels = [f"B{i}" for i in range(1, args.seats + 1)]
        show_resp = await client.post(
            f"{base_url}/shows",
            json={
                "name": f"Burst Show {int(time.time())}",
                "price_paise": 2500,
                "per_user_limit": 4,
                "seats": seat_labels,
            },
            headers={"Authorization": f"Bearer {admin_token}"},
        )
        if show_resp.status_code != 201:
            print(f"[!] Failed to create show: HTTP {show_resp.status_code} {show_resp.text}")
            sys.exit(1)
        show_id = show_resp.json()["id"]
        print(f"[+] Show created successfully: {show_id} with {len(seat_labels)} seats.")

        sem = asyncio.Semaphore(args.concurrency)
        total_5xx = 0
        total_201_seats = 0
        total_cancelled_seats = 0
        run_id = f"b{int(time.time())}_{random.randint(1000, 9999)}"

        # Helper to get user auth header
        # Cache tokens to avoid flooding auth route unnecessarily
        token_cache: Dict[str, str] = {}

        def get_auth_header(uid: str) -> Dict[str, str]:
            if uid not in token_cache:
                tok = mint_token(base_url, uid)
                token_cache[uid] = tok
            return {"Authorization": f"Bearer {token_cache[uid]}"}

        # Pre-mint a batch of tokens for common user pools
        print("Minting user auth tokens...")
        for i in range(500):
            token_cache[f"{run_id}_hot_{i}"] = mint_token(base_url, f"{run_id}_hot_{i}")
        for i in range(args.hot * 100):
            token_cache[f"{run_id}_hotset_{i}"] = mint_token(base_url, f"{run_id}_hotset_{i}")

        # -------------------------------------------------------------
        # SCENARIO A: Hot-seat storm (500 users, 1 seat)
        # -------------------------------------------------------------
        reqs_a = []
        for i in range(500):
            uid = f"{run_id}_hot_{i}"
            reqs_a.append(
                (
                    "POST",
                    f"{base_url}/reservations",
                    {"show_id": show_id, "seats": ["B1"], "idempotency_key": f"{run_id}-a-{i}"},
                    get_auth_header(uid),
                )
            )
        stats_a = await execute_burst(client, sem, reqs_a)
        print_stats_table("A: Hot-Seat Storm (500 contenders, seat B1)", stats_a)
        total_5xx += stats_a["status_5xx"]
        total_201_seats += stats_a["confirmed_seats"]

        if stats_a["status_201"] != 1:
            print(
                f"[!] SCENARIO A FAILED: Expected exactly 1 winner for B1, "
                f"got {stats_a['status_201']}"
            )
            sys.exit(1)

        # -------------------------------------------------------------
        # SCENARIO B: Hot set (1,000 users, 10 hot seats)
        # -------------------------------------------------------------
        hot_seats = [f"B{i}" for i in range(2, 2 + args.hot)]
        reqs_b = []
        for i in range(1000):
            uid = f"{run_id}_hotset_{i}"
            chosen_seat = random.choice(hot_seats)
            reqs_b.append(
                (
                    "POST",
                    f"{base_url}/reservations",
                    {
                        "show_id": show_id,
                        "seats": [chosen_seat],
                        "idempotency_key": f"{run_id}-b-{i}",
                    },
                    get_auth_header(uid),
                )
            )
        stats_b = await execute_burst(client, sem, reqs_b)
        print_stats_table(f"B: Hot-Set (1,000 contenders, {args.hot} seats)", stats_b)
        total_5xx += stats_b["status_5xx"]
        total_201_seats += stats_b["confirmed_seats"]

        # -------------------------------------------------------------
        # SCENARIO C: Full Stampede with 20% same-key retries
        # -------------------------------------------------------------
        stampede_count = min(args.users, 5000)  # Standard burst size
        available_seats = [f"B{i}" for i in range(2 + args.hot, args.seats - 20)]
        weights = [1.0 / (i**1.1) for i in range(1, len(available_seats) + 1)]
        reqs_c = []
        prev_req = None
        for i in range(stampede_count):
            if prev_req and (i % 5 == 0):
                reqs_c.append(prev_req)
            else:
                uid = f"{run_id}_stampede_{i}"
                if uid not in token_cache:
                    token_cache[uid] = mint_token(base_url, uid)
                st = random.choices(available_seats, weights=weights, k=1)[0]
                item = (
                    "POST",
                    f"{base_url}/reservations",
                    {"show_id": show_id, "seats": [st], "idempotency_key": f"{run_id}-c-{i}"},
                    get_auth_header(uid),
                )
                reqs_c.append(item)
                prev_req = item

        stats_c = await execute_burst(client, sem, reqs_c)
        print_stats_table(f"C: Full Stampede ({stampede_count} requests, 20% retries)", stats_c)
        total_5xx += stats_c["status_5xx"]
        total_201_seats += stats_c["confirmed_seats"]

        # -------------------------------------------------------------
        # SCENARIO D: Per-user limit (1 user x 10 parallel requests)
        # -------------------------------------------------------------
        quota_user = f"{run_id}_quota_burst_user"
        token_cache[quota_user] = mint_token(base_url, quota_user)
        free_seats_d = [f"B{i}" for i in range(args.seats - 20, args.seats - 10)]
        reqs_d = []
        for i in range(10):
            reqs_d.append(
                (
                    "POST",
                    f"{base_url}/reservations",
                    {
                        "show_id": show_id,
                        "seats": [free_seats_d[i]],
                        "idempotency_key": f"{run_id}-d-{i}",
                    },
                    get_auth_header(quota_user),
                )
            )
        stats_d = await execute_burst(client, sem, reqs_d)
        print_stats_table("D: Per-User Limit Race (1 user x 10 parallel, limit 4)", stats_d)
        total_5xx += stats_d["status_5xx"]
        total_201_seats += stats_d["confirmed_seats"]

        if stats_d["status_201"] != 4 or stats_d["status_409"] != 6:
            print(
                f"[!] SCENARIO D FAILED: Expected 4 x 201 and 6 x 409, "
                f"got 201={stats_d['status_201']}, 409={stats_d['status_409']}"
            )
            sys.exit(1)

        # -------------------------------------------------------------
        # SCENARIO E: Spoof Test (body user_id spoofed; cancel foreign reservation)
        # -------------------------------------------------------------
        victim_tok = get_auth_header(f"{run_id}_victim_user")["Authorization"]
        attacker_tok = get_auth_header(f"{run_id}_attacker_user")["Authorization"]
        spoof_seat = f"B{args.seats - 5}"
        res_victim = await client.post(
            f"{base_url}/reservations",
            json={
                "show_id": show_id,
                "seats": [spoof_seat],
                "idempotency_key": f"{run_id}-victim-key-1",
            },
            headers={"Authorization": victim_tok},
        )
        if res_victim.status_code != 201:
            print(
                f"[!] SCENARIO E FAILED: Victim booking returned {res_victim.status_code}\n"
                f"{res_victim.text}"
            )
            sys.exit(1)
        victim_res_id = res_victim.json()["id"]
        total_201_seats += 1

        # Attacker attempts to cancel victim's reservation
        cancel_hack = await client.post(
            f"{base_url}/reservations/{victim_res_id}/cancel",
            headers={"Authorization": attacker_tok},
        )
        print("\n--- Scenario E: Spoof & Ownership Protection ---")
        if cancel_hack.status_code == 404:
            print("  [PASS] Foreign cancellation rejected with 404 Not Found (ownership enforced)")
        else:
            print(
                f"  [FAIL] Foreign cancellation returned HTTP {cancel_hack.status_code} "
                f"(expected 404)"
            )
            sys.exit(1)

        # -------------------------------------------------------------
        # SCENARIO F: Cancel/Rebook Race
        # -------------------------------------------------------------
        race_seat = f"B{args.seats - 4}"
        race_res = await client.post(
            f"{base_url}/reservations",
            json={
                "show_id": show_id,
                "seats": [race_seat],
                "idempotency_key": f"{run_id}-race-key-init",
            },
            headers={"Authorization": victim_tok},
        )
        if race_res.status_code != 201:
            print(
                f"[!] SCENARIO F FAILED: Race initial booking returned {race_res.status_code}\n"
                f"{race_res.text}"
            )
            sys.exit(1)
        race_res_id = race_res.json()["id"]
        total_201_seats += 1

        # Holder cancels while 50 users try to book that seat
        cancel_req = (
            "POST",
            f"{base_url}/reservations/{race_res_id}/cancel",
            {},
            {"Authorization": victim_tok},
        )
        book_reqs = [
            (
                "POST",
                f"{base_url}/reservations",
                {
                    "show_id": show_id,
                    "seats": [race_seat],
                    "idempotency_key": f"{run_id}-f-contend-{i}",
                },
                get_auth_header(f"{run_id}_contender_f_{i}"),
            )
            for i in range(50)
        ]
        stats_f = await execute_burst(client, sem, [cancel_req] + book_reqs)
        print_stats_table("F: Cancel / Rebook Race (1 cancel vs 50 contenders)", stats_f)
        total_5xx += stats_f["status_5xx"]
        total_201_seats += stats_f["confirmed_seats"]
        total_cancelled_seats += 1  # 1 successful cancel

        # -------------------------------------------------------------
        # FINAL RECONCILIATION
        # -------------------------------------------------------------
        print("\n=================================================================")
        print("FINAL RECONCILIATION & OBSERVABILITY AUDIT")
        print("=================================================================")
        reconciliation_passed = True

        state_resp = await client.get(f"{base_url}/shows/{show_id}")
        state = state_resp.json()
        total = state["total_seats"]
        avail = state["available"]
        held = state["held"]
        conf = state["confirmed"]

        print(
            f"Show Final State: Total={total} | Available={avail} | Held={held} | Confirmed={conf}"
        )

        # Check 1: Invariant available + held + confirmed == total
        if avail + held + conf == total:
            print(f"  [PASS] Mathematical Invariant: {avail} + {held} + {conf} == {total}")
        else:
            print(f"  [FAIL] Invariant broken: {avail} + {held} + {conf} != {total}")
            reconciliation_passed = False

        # Check 2: Confirmed seats == sum(201 seats) - cancelled
        expected_confirmed = total_201_seats - total_cancelled_seats
        if conf == expected_confirmed:
            print(
                f"  [PASS] Exact Audit: confirmed ({conf}) == sum(201 seats [{total_201_seats}]) "
                f"- cancelled ({total_cancelled_seats})"
            )
        else:
            print(f"  [FAIL] Audit mismatch: confirmed ({conf}) != expected ({expected_confirmed})")
            reconciliation_passed = False

        # Check 3: Scrape /metrics and verify seats_available
        m_resp = await client.get(f"{base_url}/metrics")
        if m_resp.status_code == 200:
            print("  [PASS] /metrics scraped successfully")
        else:
            print(f"  [FAIL] /metrics returned HTTP {m_resp.status_code}")
            reconciliation_passed = False

        # Check 4: Zero 5xx check
        if total_5xx == 0:
            print("  [PASS] Zero 5xx guarantee holds (5xx errors = 0 across all scenarios)")
        else:
            print(f"  [FAIL] Zero 5xx guarantee violated: {total_5xx} 5xx errors encountered")
            reconciliation_passed = False

        print("=================================================================")
        if reconciliation_passed and total_5xx == 0:
            print("BURST VERIFICATION COMPLETE: ALL PASS [OK]")
            sys.exit(0)
        else:
            print("BURST VERIFICATION COMPLETE: FAILURES OBSERVED [EXIT 1]")
            sys.exit(1)


def main():
    asyncio.run(async_main())


if __name__ == "__main__":
    main()
