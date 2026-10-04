import asyncio
import uuid
from contextlib import asynccontextmanager
from typing import AsyncGenerator, List

from httpx import AsyncClient

from app.config import settings


async def create_concurrency_show(
    client: AsyncClient,
    seats_count: int,
    price_paise: int = 10000,
    per_user_limit: int = 4,
) -> str:
    labels = [f"C{i}" for i in range(1, seats_count + 1)]
    resp = await client.post(
        "/shows",
        json={
            "name": f"Conc Show {uuid.uuid4()}",
            "price_paise": price_paise,
            "per_user_limit": per_user_limit,
            "seats": labels,
        },
        headers={"Authorization": f"Bearer {settings.admin_token}"},
    )
    assert resp.status_code == 201, f"Failed to create show: {resp.text}"
    return resp.json()["id"]


@asynccontextmanager
async def background_invariant_poller(
    client: AsyncClient,
    show_id: str,
    poll_interval_ms: int = 50,
) -> AsyncGenerator[None, None]:
    stop_event = asyncio.Event()
    error_box: List[str] = []

    async def _poller():
        while not stop_event.is_set():
            try:
                r = await client.get(f"/shows/{show_id}")
                if r.status_code == 200:
                    data = r.json()
                    total = data["total_seats"]
                    avail = data["available"]
                    held = data["held"]
                    conf = data["confirmed"]
                    if (avail + held + conf) != total:
                        error_box.append(
                            f"In-flight invariant violated: {avail} + {held} + {conf} != {total}"
                        )
            except Exception as e:
                error_box.append(f"Poller exception: {e}")
            await asyncio.sleep(poll_interval_ms / 1000.0)

    poller_task = asyncio.create_task(_poller())
    try:
        yield
    finally:
        stop_event.set()
        await poller_task
        if error_box:
            raise AssertionError("\n".join(error_box))
