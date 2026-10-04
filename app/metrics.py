import time
from typing import Dict, Tuple

from prometheus_client import (
    CONTENT_TYPE_LATEST,
    CollectorRegistry,
    Counter,
    Gauge,
    Histogram,
    generate_latest,
)

# Application registry for global counters, gauges, histograms
app_registry = CollectorRegistry(auto_describe=True)
shows_registry = CollectorRegistry(auto_describe=True)

# Counters
reservations_confirmed_total = Counter(
    "reservations_confirmed_total",
    "Total count of confirmed reservations",
    registry=app_registry,
)

seats_confirmed_total = Counter(
    "seats_confirmed_total",
    "Total count of confirmed seats",
    registry=app_registry,
)

reservations_declined_total = Counter(
    "reservations_declined_total",
    "Total count of declined reservations partitioned by decline reason",
    ["reason"],
    registry=app_registry,
)

reservations_cancelled_total = Counter(
    "reservations_cancelled_total",
    "Total count of cancelled reservations",
    registry=app_registry,
)

# HTTP Request metrics
http_requests_total = Counter(
    "http_requests_total",
    "Total HTTP requests received",
    ["route", "method", "status"],
    registry=app_registry,
)

http_request_duration_seconds = Histogram(
    "http_request_duration_seconds",
    "HTTP request latency in seconds",
    ["route"],
    buckets=[0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0],
    registry=app_registry,
)

# System & DB Gauges
inflight_requests = Gauge(
    "inflight_requests",
    "Number of currently in-flight HTTP requests",
    registry=app_registry,
)

db_pool_in_use = Gauge(
    "db_pool_in_use",
    "Number of database connections currently checked out of the pool",
    registry=app_registry,
)

db_semaphore_waiting = Gauge(
    "db_semaphore_waiting",
    "Number of requests currently waiting to acquire the DB concurrency semaphore",
    registry=app_registry,
)

db_retries_total = Counter(
    "db_retries_total",
    "Total count of transient database retries partitioned by SQLSTATE",
    ["sqlstate"],
    registry=app_registry,
)

# Seat status gauges per show
seats_available = Gauge(
    "seats_available",
    "Current count of available seats for a show",
    ["show_id"],
    registry=shows_registry,
)

seats_confirmed = Gauge(
    "seats_confirmed",
    "Current count of confirmed seats for a show",
    ["show_id"],
    registry=shows_registry,
)

seats_held = Gauge(
    "seats_held",
    "Current count of held seats for a show",
    ["show_id"],
    registry=shows_registry,
)

# Cache for seat gauges (1s TTL)
_seat_metrics_cache: Dict[str, Tuple[float, int, int, int]] = {}


def record_seat_metrics(show_id: str, available: int, confirmed: int, held: int = 0):
    now = time.time()
    _seat_metrics_cache[show_id] = (now, available, confirmed, held)
    seats_available.labels(show_id=show_id).set(available)
    seats_confirmed.labels(show_id=show_id).set(confirmed)
    seats_held.labels(show_id=show_id).set(held)


def get_metrics_output() -> Tuple[bytes, str]:
    output = generate_latest(app_registry) + generate_latest(shows_registry)
    return output, CONTENT_TYPE_LATEST
