import json
import time
import uuid
from typing import Any, Dict, List, Optional, Tuple

from app.config import settings
from app.db import db_connection
from app.errors import NotFoundException, ValidationException
from app.metrics import record_seat_metrics

# In-process cache for show state reads: show_id -> (timestamp, data)
_show_state_cache: Dict[str, Tuple[float, Dict[str, Any]]] = {}

# In-process cache for immutable show metadata (id, name, price_paise, per_user_limit, total_seats)
_show_meta_cache: Dict[uuid.UUID, Dict[str, Any]] = {}


def clear_show_state_cache() -> None:
    _show_state_cache.clear()
    _show_meta_cache.clear()


def get_cached_show_meta(show_id: uuid.UUID) -> Optional[Dict[str, Any]]:
    return _show_meta_cache.get(show_id)


def set_cached_show_meta(show_id: uuid.UUID, data: Dict[str, Any]) -> None:
    _show_meta_cache[show_id] = data



async def create_show(
    name: str,
    price_paise: int,
    per_user_limit: int,
    seats: List[str],
) -> Dict[str, Any]:
    # Domain validation
    name_clean = name.strip()
    if not name_clean:
        raise ValidationException("Show name cannot be empty")

    if price_paise <= 0:
        raise ValidationException("price_paise must be a strictly positive integer")

    if not (1 <= per_user_limit <= 100):
        raise ValidationException("per_user_limit must be between 1 and 100")

    if not seats:
        raise ValidationException("Show must have at least one seat")

    if len(seats) > 100_000:
        raise ValidationException("Cannot create more than 100,000 seats per show")

    cleaned_seats: List[str] = []
    seen = set()
    for s in seats:
        label = s.strip()
        if not label:
            raise ValidationException("Seat label cannot be empty or whitespace")
        if label in seen:
            raise ValidationException(f"Duplicate seat label: '{label}'")
        seen.add(label)
        cleaned_seats.append(label)

    total_seats = len(cleaned_seats)

    async with db_connection() as conn:
        async with conn.transaction():
            show_row = await conn.fetchrow(
                """
                INSERT INTO shows (name, price_paise, per_user_limit, total_seats)
                VALUES ($1, $2, $3, $4)
                RETURNING id, name, price_paise, per_user_limit, total_seats, created_at
                """,
                name_clean,
                price_paise,
                per_user_limit,
                total_seats,
            )
            show_id = show_row["id"]

            await conn.execute(
                """
                INSERT INTO seats (show_id, label, status)
                SELECT $1, unnest($2::text[]), 'available'
                """,
                show_id,
                cleaned_seats,
            )

    record_seat_metrics(str(show_id), available=total_seats, confirmed=0, held=0)
    set_cached_show_meta(
        show_id,
        {
            "id": show_id,
            "name": name_clean,
            "price_paise": price_paise,
            "per_user_limit": per_user_limit,
            "total_seats": total_seats,
        },
    )

    return {
        "id": str(show_row["id"]),
        "name": show_row["name"],
        "price_paise": show_row["price_paise"],
        "per_user_limit": show_row["per_user_limit"],
        "total_seats": show_row["total_seats"],
        "created_at": show_row["created_at"].isoformat(),
    }


async def get_show_by_id(show_id_str: str) -> Dict[str, Any]:
    try:
        show_uuid = uuid.UUID(show_id_str)
    except (ValueError, AttributeError):
        raise NotFoundException(f"Show {show_id_str} not found") from None

    # In-process cache check
    cache_ttl = settings.show_state_cache_ms / 1000.0
    now = time.monotonic()
    if cache_ttl > 0:
        cached = _show_state_cache.get(show_id_str)
        if cached is not None:
            cached_at, cached_data = cached
            if (now - cached_at) < cache_ttl:
                return cached_data

    async with db_connection() as conn:
        row = await conn.fetchrow(
            """
            SELECT
                s.id,
                s.name,
                s.price_paise,
                s.per_user_limit,
                s.total_seats,
                count(st.label) FILTER (WHERE st.status = 'available')::int AS available,
                count(st.label) FILTER (WHERE st.status = 'held')::int AS held,
                count(st.label) FILTER (WHERE st.status = 'confirmed')::int AS confirmed,
                COALESCE(
                    jsonb_object_agg(st.label, st.status) FILTER (WHERE st.label IS NOT NULL),
                    '{}'::jsonb
                ) AS seats
            FROM shows s
            LEFT JOIN seats st ON st.show_id = s.id
            WHERE s.id = $1
            GROUP BY s.id
            """,
            show_uuid,
        )

    if not row:
        raise NotFoundException(f"Show {show_id_str} not found")

    seats_data = row["seats"]
    if isinstance(seats_data, str):
        seats_data = json.loads(seats_data)

    available = row["available"]
    held = row["held"]
    confirmed = row["confirmed"]

    record_seat_metrics(show_id_str, available=available, confirmed=confirmed, held=held)

    result = {
        "id": str(row["id"]),
        "name": row["name"],
        "price_paise": row["price_paise"],
        "per_user_limit": row["per_user_limit"],
        "total_seats": row["total_seats"],
        "available": available,
        "held": held,
        "confirmed": confirmed,
        "seats": seats_data,
    }

    if cache_ttl > 0:
        _show_state_cache[show_id_str] = (now, result)

    return result
