#!/usr/bin/env python3
"""scripts/reconcile.py: Verifies consistency and invariants using public API and metrics only.

Works against any running instance (local, docker, or remote live URL) without database access.
"""

import argparse
import sys
from typing import Dict, List, Optional

import httpx


def parse_prometheus_metrics(metrics_text: str) -> Dict[str, List[Dict[str, any]]]:
    """Lightweight Prometheus text parser for remote metrics."""
    metrics: Dict[str, List[Dict[str, any]]] = {}
    for line in metrics_text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split()
        if len(parts) < 2:
            continue
        metric_str, val_str = parts[0], parts[1]
        try:
            val = float(val_str)
        except ValueError:
            continue

        name = metric_str
        labels: Dict[str, str] = {}
        if "{" in metric_str and metric_str.endswith("}"):
            name, label_part = metric_str[:-1].split("{", 1)
            for pair in label_part.split(","):
                if "=" in pair:
                    k, v = pair.split("=", 1)
                    labels[k.strip()] = v.strip().strip('"')

        if name not in metrics:
            metrics[name] = []
        metrics[name].append({"labels": labels, "value": val})
    return metrics


def get_metric_value(
    metrics: Dict[str, List[Dict[str, any]]],
    name: str,
    labels: Optional[Dict[str, str]] = None,
) -> Optional[float]:
    for entry in metrics.get(name, []):
        if labels is None or all(entry["labels"].get(k) == v for k, v in labels.items()):
            return entry["value"]
    return None


def reconcile_show(
    client: httpx.Client,
    base_url: str,
    show_id: str,
    metrics: Dict[str, List[Dict[str, any]]],
) -> bool:
    resp = client.get(f"{base_url}/shows/{show_id}")
    if resp.status_code != 200:
        print(f"[-] FAILED: Could not fetch show {show_id}: HTTP {resp.status_code}")
        return False

    data = resp.json()
    total = data.get("total_seats", 0)
    avail = data.get("available", 0)
    held = data.get("held", 0)
    conf = data.get("confirmed", 0)
    seats_map = data.get("seats", {})

    print(f"\nReconciling Show: {show_id} ('{data.get('name')}')")
    print(f"  Seats State: Total={total} | Available={avail} | Held={held} | Confirmed={conf}")

    checks_passed = True

    # 1. Total Invariant
    sum_seats = avail + held + conf
    if sum_seats == total:
        print(
            f"  [PASS] Invariant: available ({avail}) + held ({held}) "
            f"+ confirmed ({conf}) == {total}"
        )
    else:
        print(f"  [FAIL] Invariant broken: {avail} + {held} + {conf} = {sum_seats} != {total}")
        checks_passed = False

    # 2. Seat Map Count Invariant
    if len(seats_map) == total:
        print(f"  [PASS] Seat map size matches total_seats ({len(seats_map)} == {total})")
    else:
        print(f"  [FAIL] Seat map size mismatch: {len(seats_map)} != {total}")
        checks_passed = False

    # 3. Status Distribution in Seat Map
    map_avail = sum(1 for s in seats_map.values() if s == "available")
    map_held = sum(1 for s in seats_map.values() if s == "held")
    map_conf = sum(1 for s in seats_map.values() if s == "confirmed")
    if map_avail == avail and map_held == held and map_conf == conf:
        print("  [PASS] Seat map status counts match exact state numbers")
    else:
        print(
            f"  [FAIL] Seat map counts ({map_avail}/{map_held}/{map_conf}) "
            f"differ from totals ({avail}/{held}/{conf})"
        )
        checks_passed = False

    # 4. Metric Alignment
    metric_avail = get_metric_value(metrics, "seats_available", {"show_id": show_id})
    metric_conf = get_metric_value(metrics, "seats_confirmed", {"show_id": show_id})
    metric_held = get_metric_value(metrics, "seats_held", {"show_id": show_id})

    if metric_avail is not None:
        if metric_avail == avail:
            print(f"  [PASS] Prometheus gauge seats_available matches API ({metric_avail})")
        else:
            print(f"  [FAIL] Prometheus seats_available ({metric_avail}) != API ({avail})")
            checks_passed = False
    else:
        print("  [INFO] Prometheus gauge seats_available not yet scraped for this show")

    if metric_conf is not None and metric_conf != conf:
        print(f"  [FAIL] Prometheus seats_confirmed ({metric_conf}) != API ({conf})")
        checks_passed = False

    if metric_held is not None and metric_held != held:
        print(f"  [FAIL] Prometheus seats_held ({metric_held}) != API ({held})")
        checks_passed = False

    return checks_passed


def main():
    parser = argparse.ArgumentParser(
        description="Reconcile database and metrics invariants via API"
    )
    parser.add_argument(
        "base_url", nargs="?", default="http://localhost:8000", help="Base URL of service"
    )
    parser.add_argument("--show-id", default=None, help="Target Show UUID to reconcile")
    args = parser.parse_args()

    base_url = args.base_url.rstrip("/")
    print(f"=== Running Reconciliation against {base_url} ===")

    with httpx.Client(timeout=10.0) as client:
        # Check health
        health = client.get(f"{base_url}/healthz")
        ready = client.get(f"{base_url}/readyz")
        if health.status_code != 200 or ready.status_code != 200:
            print(
                f"[!] Target service not ready: "
                f"/healthz={health.status_code}, /readyz={ready.status_code}"
            )
            sys.exit(1)

        # Scrape metrics
        m_resp = client.get(f"{base_url}/metrics")
        if m_resp.status_code != 200:
            print(f"[!] Failed to scrape /metrics: HTTP {m_resp.status_code}")
            sys.exit(1)
        metrics = parse_prometheus_metrics(m_resp.text)

        all_ok = True
        if args.show_id:
            show_ok = reconcile_show(client, base_url, args.show_id, metrics)
            if not show_ok:
                all_ok = False
        else:
            # Reconcile shows observed in metrics
            found_shows = set()
            for entry in metrics.get("seats_available", []):
                sid = entry["labels"].get("show_id")
                if sid:
                    found_shows.add(sid)

            if not found_shows:
                print(
                    "No active shows found in Prometheus metrics. "
                    "Pass --show-id to reconcile a specific show."
                )
            else:
                for sid in found_shows:
                    show_ok = reconcile_show(client, base_url, sid, metrics)
                    if not show_ok:
                        all_ok = False

        print("\n" + "=" * 50)
        if all_ok:
            print("RECONCILIATION RESULT: ALL CHECKS PASSED [OK]")
            sys.exit(0)
        else:
            print("RECONCILIATION RESULT: FAILURES DETECTED [ERROR]")
            sys.exit(1)


if __name__ == "__main__":
    main()
