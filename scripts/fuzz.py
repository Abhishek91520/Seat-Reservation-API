#!/usr/bin/env python3
"""scripts/fuzz.py: 50 simulated users running randomized operations for 60s.

Performs reservations (1-3 seats), idempotency replays, own and foreign cancellations,
and hot seat contention against any live base URL, then runs reconciliation.
"""

import argparse
import asyncio
import os
import random
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional, Tuple

import httpx
import jwt

ADMIN_TOKEN = os.environ.get("ADMIN_TOKEN", "admin-secret-token")
JWT_SECRET = os.environ.get("JWT_SECRET", "seat-reservation-dev-secret-change-in-prod")


def mint_token(arg1: str, arg2: Optional[str] = None) -> str:
    user_id = arg2 if arg2 is not None else arg1
    now = datetime.now(timezone.utc)
    return jwt.encode(
        {"sub": user_id, "role": "user", "iat": now, "exp": now + timedelta(days=7)},
        JWT_SECRET,
        algorithm="HS256",
    )


async def async_main():
    parser = argparse.ArgumentParser(description="Randomized operation fuzzer")
    parser.add_argument(
        "base_url", nargs="?", default="http://localhost:8000", help="Base URL of service"
    )
    parser.add_argument(
        "--duration", type=int, default=60, help="Duration to run fuzzer in seconds (default: 60)"
    )
    parser.add_argument(
        "--concurrency",
        type=int,
        default=50,
        help="Number of concurrent simulated users (default: 50)",
    )
    parser.add_argument("--seats", type=int, default=100, help="Number of seats in show")
    args = parser.parse_args()

    base_url = args.base_url.rstrip("/")
    admin_token = os.environ.get("ADMIN_TOKEN", ADMIN_TOKEN)

    print("=================================================================")
    print(f"FUZZER STARTING against {base_url}")
    print(f"Users={args.concurrency} | Duration={args.duration}s | Show Seats={args.seats}")
    print("=================================================================")

    limits = httpx.Limits(
        max_connections=args.concurrency, max_keepalive_connections=args.concurrency
    )
    async with httpx.AsyncClient(limits=limits, timeout=15.0) as client:
        # Check health
        h = await client.get(f"{base_url}/healthz")
        r = await client.get(f"{base_url}/readyz")
        if h.status_code != 200 or r.status_code != 200:
            print(f"[!] Target not ready: /healthz={h.status_code}, /readyz={r.status_code}")
            sys.exit(1)

        # Create fresh show
        seat_labels = [f"F{i}" for i in range(1, args.seats + 1)]
        show_resp = await client.post(
            f"{base_url}/shows",
            json={
                "name": f"Fuzz Show {int(time.time())}",
                "price_paise": 1500,
                "per_user_limit": 4,
                "seats": seat_labels,
            },
            headers={"Authorization": f"Bearer {admin_token}"},
        )
        if show_resp.status_code != 201:
            print(f"[!] Failed to create show: HTTP {show_resp.status_code} {show_resp.text}")
            sys.exit(1)
        show_id = show_resp.json()["id"]
        print(f"[+] Fresh show created: {show_id} with {len(seat_labels)} seats.")

        # Mint tokens for 50 users
        print(f"Minting JWTs for {args.concurrency} simulated users...")
        user_tokens = {}
        for i in range(args.concurrency):
            uid = f"fuzz_user_{i}"
            user_tokens[uid] = mint_token(base_url, uid)

        # Shared user states
        user_reservations: Dict[str, List[str]] = {uid: [] for uid in user_tokens}
        user_previous_requests: Dict[str, Tuple[List[str], str]] = {}  # uid -> (seats, key)

        status_counts: Dict[int, int] = {}
        stop_event = asyncio.Event()

        async def _fuzz_worker(worker_idx: int):
            uid = f"fuzz_user_{worker_idx}"
            tok = user_tokens[uid]
            headers = {"Authorization": f"Bearer {tok}"}
            op_idx = 0

            while not stop_event.is_set():
                op_idx += 1
                dice = random.random()
                try:
                    if dice < 0.40:
                        # 40%: Reserve 1 to 3 random seats
                        k = random.randint(1, 3)
                        chosen_seats = random.sample(seat_labels, k=k)
                        key = f"k-{uid}-{op_idx}-{random.randint(1, 100000)}"
                        user_previous_requests[uid] = (chosen_seats, key)
                        res = await client.post(
                            f"{base_url}/reservations",
                            json={
                                "show_id": show_id,
                                "seats": chosen_seats,
                                "idempotency_key": key,
                            },
                            headers=headers,
                        )
                        sc = res.status_code
                        status_counts[sc] = status_counts.get(sc, 0) + 1
                        if sc == 201:
                            user_reservations[uid].append(res.json()["id"])

                    elif dice < 0.65:
                        # 25%: Hot seat contention (F1 to F5)
                        hot_pick = random.choice(seat_labels[:5])
                        key = f"hot-{uid}-{op_idx}-{random.randint(1, 100000)}"
                        res = await client.post(
                            f"{base_url}/reservations",
                            json={"show_id": show_id, "seats": [hot_pick], "idempotency_key": key},
                            headers=headers,
                        )
                        sc = res.status_code
                        status_counts[sc] = status_counts.get(sc, 0) + 1
                        if sc == 201:
                            user_reservations[uid].append(res.json()["id"])

                    elif dice < 0.80:
                        # 15%: Idempotency replay with same key
                        prev = user_previous_requests.get(uid)
                        if prev:
                            seats, key = prev
                            res = await client.post(
                                f"{base_url}/reservations",
                                json={"show_id": show_id, "seats": seats, "idempotency_key": key},
                                headers=headers,
                            )
                            sc = res.status_code
                            status_counts[sc] = status_counts.get(sc, 0) + 1
                        else:
                            await asyncio.sleep(0.01)

                    elif dice < 0.90:
                        # 10%: Cancel own reservation
                        if user_reservations[uid]:
                            res_id = random.choice(user_reservations[uid])
                            res = await client.post(
                                f"{base_url}/reservations/{res_id}/cancel", headers=headers
                            )
                            sc = res.status_code
                            status_counts[sc] = status_counts.get(sc, 0) + 1
                        else:
                            await asyncio.sleep(0.01)

                    else:
                        # 10%: Cancel random or foreign reservation (expecting 404)
                        foreign_uid = f"fuzz_user_{(worker_idx + 1) % args.concurrency}"
                        foreign_res = user_reservations.get(foreign_uid, [])
                        target_id = (
                            random.choice(foreign_res) if foreign_res else f"nonexistent-{op_idx}"
                        )
                        res = await client.post(
                            f"{base_url}/reservations/{target_id}/cancel", headers=headers
                        )
                        sc = res.status_code
                        status_counts[sc] = status_counts.get(sc, 0) + 1

                except Exception:
                    status_counts[599] = status_counts.get(599, 0) + 1

                # Small cooperative yield
                await asyncio.sleep(0.005)

        print(f"\nLaunching {args.concurrency} worker tasks for {args.duration}s...")
        tasks = [asyncio.create_task(_fuzz_worker(i)) for i in range(args.concurrency)]

        # Let fuzzer run for specified duration
        await asyncio.sleep(args.duration)
        stop_event.set()
        await asyncio.gather(*tasks, return_exceptions=True)

        total_ops = sum(status_counts.values())
        errors_5xx = sum(v for k, v in status_counts.items() if 500 <= k <= 599)

        print("\n=================================================================")
        print(f"FUZZER COMPLETE: {total_ops} operations executed in {args.duration}s")
        print("Outcome Distribution:")
        for sc in sorted(status_counts.keys()):
            print(f"  HTTP {sc}: {status_counts[sc]}")
        print(f"Total 5xx Errors: {errors_5xx}")
        print("=================================================================")

        if errors_5xx > 0:
            print(f"[!] FAILED: 5xx errors encountered during fuzz run: {errors_5xx}")
            sys.exit(1)

        # Run reconcile.py against the fuzzed show
        print("\nInvoking reconcile.py on fuzzed show...")
        reconcile_script = os.path.join(os.path.dirname(__file__), "reconcile.py")
        cmd = [sys.executable, reconcile_script, base_url, "--show-id", show_id]
        res = subprocess.run(cmd)

        if res.returncode == 0:
            print("\nFUZZ TEST PASSED [OK]")
            sys.exit(0)
        else:
            print("\nFUZZ TEST FAILED RECONCILIATION [ERROR]")
            sys.exit(1)


def main():
    asyncio.run(async_main())


if __name__ == "__main__":
    main()
