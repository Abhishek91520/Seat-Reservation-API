import asyncio

from fastapi import APIRouter, Response, status

from app.db import check_db_ready, db_connection
from app.metrics import get_metrics_output, refresh_seats_gauges

router = APIRouter(tags=["health"])


@router.get("/healthz", status_code=status.HTTP_200_OK)
async def healthz():
    """Liveness probe. Always 200 if the process is up."""
    return {"status": "ok"}


@router.get("/readyz")
async def readyz():
    """Readiness probe. Checks database readiness via dedicated connection."""
    is_ready = await check_db_ready()
    if is_ready:
        return Response(
            content='{"status":"ready"}',
            status_code=status.HTTP_200_OK,
            media_type="application/json",
        )
    return Response(
        content='{"status":"unavailable","message":"Database check failed"}',
        status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
        media_type="application/json",
    )


@router.get("/metrics")
async def metrics():
    """Prometheus metrics endpoint."""
    try:
        async with asyncio.timeout(0.5):
            async with db_connection() as conn:
                await refresh_seats_gauges(conn)
    except Exception:
        pass
    body, content_type = get_metrics_output()
    return Response(content=body, media_type=content_type)


@router.get("/logs")
async def logs(limit: int = 50):
    """Public structured log viewer returning recent JSON request logs with correlation IDs."""
    from app.logging import get_recent_logs, recent_logs

    safe_limit = max(1, min(limit, 200))
    return {
        "total_buffered": len(recent_logs),
        "count": len(get_recent_logs(safe_limit)),
        "logs": get_recent_logs(safe_limit),
    }

