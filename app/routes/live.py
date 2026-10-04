import os

from fastapi import APIRouter
from fastapi.responses import FileResponse

router = APIRouter(tags=["dashboard"])

LIVE_HTML_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "static",
    "live.html",
)


@router.get("/live", response_class=FileResponse)
async def get_live_dashboard():
    return FileResponse(LIVE_HTML_PATH, media_type="text/html")
