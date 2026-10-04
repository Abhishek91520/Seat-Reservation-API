import time
import uuid
from contextlib import asynccontextmanager
from typing import AsyncGenerator

import structlog
from fastapi import FastAPI, Request, Response
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


@app.middleware("http")
async def logging_and_metrics_middleware(request: Request, call_next) -> Response:
    # 1. Request ID handling
    req_id = request.headers.get("X-Request-ID")
    if not req_id:
        req_id = str(uuid.uuid4())
    request.state.request_id = req_id
    request_id_var.set(req_id)
    user_id_var.set(None)
    show_id_var.set(None)
    seats_var.set(None)
    reason_var.set(None)

    # In-flight gauge
    inflight_requests.inc()
    start_time = time.perf_counter()
    status_code = 500
    outcome = "unknown"

    try:
        response: Response = await call_next(request)
        status_code = response.status_code
        outcome = "success" if status_code < 400 else "error"
        response.headers["X-Request-ID"] = req_id
        return response
    except Exception as exc:
        outcome = "unhandled_exception"
        raise exc
    finally:
        latency_ms = round((time.perf_counter() - start_time) * 1000, 2)
        latency_s = latency_ms / 1000.0
        inflight_requests.dec()

        # Route matching template (e.g. /shows/{id} instead of raw path)
        route_template = request.url.path
        if request.scope.get("endpoint"):
            # If matched by starlette routing
            route = request.scope.get("route")
            if route and hasattr(route, "path"):
                route_template = route.path

        # Record metrics
        http_requests_total.labels(
            route=route_template,
            method=request.method,
            status=str(status_code),
        ).inc()
        http_request_duration_seconds.labels(route=route_template).observe(latency_s)

        user_id = getattr(request.state, "user_id", None) or user_id_var.get()
        show_id = getattr(request.state, "show_id", None) or show_id_var.get()
        seats = getattr(request.state, "seats", None) or seats_var.get()
        reason = getattr(request.state, "reason", None) or reason_var.get()

        log_data = {
            "request_id": req_id,
            "method": request.method,
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

        logger.info("http_request", **log_data)


# Register routes
app.include_router(health_router)
app.include_router(auth_router)
app.include_router(shows_router)
app.include_router(reservations_router)
app.include_router(live_router)
