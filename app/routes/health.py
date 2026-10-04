from fastapi import APIRouter, Response, status

from app.db import check_db_ready
from app.metrics import get_metrics_output

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
    body, content_type = get_metrics_output()
    return Response(content=body, media_type=content_type)
