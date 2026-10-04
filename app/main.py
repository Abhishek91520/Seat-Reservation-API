import time
import uuid
from contextlib import asynccontextmanager
from typing import AsyncGenerator

import structlog
from fastapi import FastAPI
from fastapi.exceptions import RequestValidationError

from app.db import close_db, init_db
from app.errors import (
    AppException,
    app_exception_handler,
    generic_exception_handler,
    validation_exception_handler,
)
from app.logging import (
    reason_var,
    record_log_event,
    request_id_var,
    seats_var,
    setup_logging,
    show_id_var,
    user_id_var,
)
from app.metrics import (
    http_request_duration_seconds,
    http_requests_total,
    inflight_requests,
)
from app.routes.auth import router as auth_router
from app.routes.health import router as health_router
from app.routes.live import router as live_router
from app.routes.reservations import router as reservations_router
from app.routes.shows import router as shows_router

setup_logging()
logger = structlog.get_logger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
    logger.info("application_startup")
    await init_db()
    yield
    logger.info("application_shutdown")
    await close_db()


app = FastAPI(
    title="Seat Reservation API",
    version="0.1.0",
    lifespan=lifespan,
)

# Exception handlers
app.add_exception_handler(AppException, app_exception_handler)
app.add_exception_handler(RequestValidationError, validation_exception_handler)
app.add_exception_handler(Exception, generic_exception_handler)


class FastLoggingAndMetricsMiddleware:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        headers = dict(scope.get("headers", []))
        req_id = None
        for k, v in headers.items():
            if k.lower() == b"x-request-id":
                req_id = v.decode("latin1")
                break
        if not req_id:
            req_id = str(uuid.uuid4())

        if "state" not in scope:
            scope["state"] = {}
        scope["state"]["request_id"] = req_id

        request_id_var.set(req_id)
        user_id_var.set(None)
        show_id_var.set(None)
        seats_var.set(None)
        reason_var.set(None)

        inflight_requests.inc()
        start_time = time.perf_counter()
        status_code = 500

        async def send_wrapper(message):
            nonlocal status_code
            if message["type"] == "http.response.start":
                status_code = message["status"]
                msg_headers = list(message.get("headers", []))
                msg_headers.append((b"x-request-id", req_id.encode("latin1")))
                message["headers"] = msg_headers
            await send(message)

        outcome = "unknown"
        try:
            await self.app(scope, receive, send_wrapper)
            outcome = "success" if status_code < 400 else "error"
        except Exception as exc:
            outcome = "unhandled_exception"
            raise exc
        finally:
            latency_ms = round((time.perf_counter() - start_time) * 1000, 2)
            latency_s = latency_ms / 1000.0
            inflight_requests.dec()

            route_template = scope.get("path", "")
            route = scope.get("route")
            if route and hasattr(route, "path"):
                route_template = route.path

            http_requests_total.labels(
                route=route_template,
                method=scope.get("method", "GET"),
                status=str(status_code),
            ).inc()
            http_request_duration_seconds.labels(route=route_template).observe(latency_s)

            app_state = scope.get("state", {})
            user_id = app_state.get("user_id") or user_id_var.get()
            show_id = app_state.get("show_id") or show_id_var.get()
            seats = app_state.get("seats") or seats_var.get()
            reason = app_state.get("reason") or reason_var.get()

            log_data = {
                "request_id": req_id,
                "method": scope.get("method", "GET"),
                "route": route_template,
                "status": status_code,
                "latency_ms": latency_ms,
                "user_id": user_id,
                "outcome": outcome,
            }
            if show_id is not None:
                log_data["show_id"] = show_id
            if seats is not None:
                log_data["seats"] = seats
            if reason is not None:
                log_data["reason"] = reason

            if outcome != "success" or status_code == 201 or (hash(req_id) % 10 == 0):
                logger.info("http_request", **log_data)
            record_log_event(log_data)


app.add_middleware(FastLoggingAndMetricsMiddleware)


# Register routes
app.include_router(health_router)
app.include_router(auth_router)
app.include_router(shows_router)
app.include_router(reservations_router)
app.include_router(live_router)
