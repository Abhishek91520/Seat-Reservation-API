import hashlib
import json
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

import asyncpg
import structlog

from app.db import execute_in_transaction_with_retry
from app.errors import (
    IdempotencyKeyReuseException,
    NotFoundException,
    PerUserLimitException,
    SeatTakenException,
    UnknownSeatException,
    ValidationException,
)
from app.logging import reason_var, seats_var, show_id_var
from app.metrics import (
    reservations_confirmed_total,
    reservations_declined_total,
    seats_confirmed_total,
)
from app.services.shows import get_cached_show_meta, set_cached_show_meta

logger = structlog.get_logger(__name__)


def compute_request_hash(show_id: str, seats: List[str]) -> str:
    """Computes a deterministic SHA-256 hash of the canonical request payload."""
    canonical_payload = {
        "seats": sorted(seats),
        "show_id": str(show_id),
    }
    canonical_json = json.dumps(
        canonical_payload,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(canonical_json.encode("utf-8")).hexdigest()


async def reserve_seats(
    user_id: str,
    show_id_str: Optional[str] = None,
    seats: Optional[List[str]] = None,
    idempotency_key: str = "",
    *,
    show_id: Optional[str] = None,
) -> Tuple[Dict[str, Any], int, bool]:
    """Executes atomic seat reservation in a single READ COMMITTED transaction.

    Returns:
        (response_body, status_code, is_replay)
    """
    actual_show_id = show_id if show_id is not None else show_id_str
    if actual_show_id is None:
        reason_var.set("validation_error")
        raise ValidationException("Show ID is required")
    show_id_str = str(actual_show_id)
    if seats is None:
        seats = []

    show_id_var.set(show_id_str)
    seats_var.set(seats)

    # -------------------------------------------------------------------------
    # STEP 0: In-Memory Validation & Show Preload
    # -------------------------------------------------------------------------
    if not idempotency_key or not idempotency_key.strip():
        reason_var.set("validation_error")
        raise ValidationException("Idempotency key must be provided and non-empty")
    idempotency_key = idempotency_key.strip()

    try:
        show_uuid = uuid.UUID(show_id_str)
    except (ValueError, AttributeError):
        reason_var.set("not_found")
        raise NotFoundException(f"Show {show_id_str} not found") from None

    if not seats:
        reason_var.set("validation_error")
        raise ValidationException("Must request at least one seat")

    cleaned_seats: List[str] = []
    seen = set()
    for s in seats:
        lbl = s.strip()
        if not lbl:
            reason_var.set("validation_error")
            raise ValidationException("Seat label cannot be empty or whitespace")
        if lbl in seen:
            reason_var.set("validation_error")
            raise ValidationException(f"Duplicate seat label in request: '{lbl}'")
        seen.add(lbl)
        cleaned_seats.append(lbl)

    num_requested = len(cleaned_seats)
    sorted_labels = sorted(cleaned_seats)
    request_hash = compute_request_hash(str(show_uuid), sorted_labels)

    # -------------------------------------------------------------------------
    # Transactional Execution with Bounded Retry on 40P01 / 40001
    # -------------------------------------------------------------------------
    async def _tx_operation(conn: asyncpg.Connection) -> Tuple[Dict[str, Any], int, bool]:
        # Pre-check show existence & fetch immutable limit and price (from cache or DB on miss)
        cached_show = get_cached_show_meta(show_uuid)
        if cached_show:
            per_user_limit = cached_show["per_user_limit"]
            price_paise = cached_show["price_paise"]
        else:
            show_row = await conn.fetchrow(
                """
                SELECT id, name, price_paise, per_user_limit, total_seats
                FROM shows
                WHERE id = $1
                """,
                show_uuid,
            )
            if not show_row:
                reason_var.set("not_found")
                raise NotFoundException(f"Show {show_id_str} not found")

            cached_data = {
                "id": show_row["id"],
                "name": show_row["name"],
                "price_paise": show_row["price_paise"],
                "per_user_limit": show_row["per_user_limit"],
                "total_seats": show_row["total_seats"],
            }
            set_cached_show_meta(show_uuid, cached_data)
            per_user_limit = cached_data["per_user_limit"]
            price_paise = cached_data["price_paise"]

        if num_requested > per_user_limit:
            reason_var.set("validation_error")
            raise ValidationException(
                f"Requested {num_requested} seats exceeds show per_user_limit of {per_user_limit}"
            )

        # ---------------------------------------------------------------------
        # LOCK ORDER STEP 1: Idempotency Row Lock & Conflict Resolution
        # Why race-free:
        # Inserting into idempotency_keys with unique constraint (user_id, key)
        # acts as a distributed mutex. A concurrent same-key transaction will
        # block on the unique index until the first transaction commits or rolls back.
        # If the first committed, the insert yields 0 rows and we read the committed body.
        # If the first rolled back, this transaction proceeds to win the insert.
        # ---------------------------------------------------------------------
        inserted_key = await conn.fetchval(
            """
            INSERT INTO idempotency_keys (user_id, key, request_hash)
            VALUES ($1, $2, $3)
            ON CONFLICT (user_id, key) DO NOTHING
            RETURNING 1
            """,
            user_id,
            idempotency_key,
            request_hash,
        )

        if not inserted_key:
            # Row already exists; inspect committed state
            stored_key_row = await conn.fetchrow(
                """
                SELECT request_hash, reservation_id, response_body
                FROM idempotency_keys
                WHERE user_id = $1 AND key = $2
                """,
                user_id,
                idempotency_key,
            )
            if stored_key_row:
                if stored_key_row["request_hash"] != request_hash:
                    reason_var.set("idempotency_key_reuse")
                    reservations_declined_total.labels(reason="idempotency_key_reuse").inc()
                    raise IdempotencyKeyReuseException(
                        "Idempotency key reused with different request payload"
                    )

                # Identical request replay: return stored response with HTTP 200
                reason_var.set("idempotent_replay")
                reservations_declined_total.labels(reason="idempotent_replay").inc()
                response_data = stored_key_row["response_body"]
                if isinstance(response_data, str):
                    response_data = json.loads(response_data)
                return response_data, 200, True

        # ---------------------------------------------------------------------
        # LOCK ORDER STEP 2: Quota Row Lock & Quota Increment (Single Statement Upsert)
        # Why race-free & atomic:
        # Atomic INSERT ... ON CONFLICT DO UPDATE WHERE held + n <= limit RETURNING held.
        # Postgres acquires an exclusive row-level lock on (show_id, user_id).
        # Any parallel requests for the same user serialize at this row.
        # If held + n > limit, 0 rows are updated/inserted, returning None and rolling back.
        # ---------------------------------------------------------------------
        new_quota = await conn.fetchval(
            """
            INSERT INTO user_show_quota (show_id, user_id, held)
            VALUES ($1, $2, $3)
            ON CONFLICT (show_id, user_id)
            DO UPDATE SET held = user_show_quota.held + EXCLUDED.held
            WHERE (user_show_quota.held + EXCLUDED.held) <= $4
            RETURNING held
            """,
            show_uuid,
            user_id,
            num_requested,
            per_user_limit,
        )

        if new_quota is None:
            reason_var.set("per_user_limit")
            reservations_declined_total.labels(reason="per_user_limit").inc()
            raise PerUserLimitException(
                f"User quota exceeded: cannot reserve {num_requested} seats "
                f"with current limit {per_user_limit}"
            )

        # ---------------------------------------------------------------------
        # LOCK ORDER STEP 3: Seat Rows Locked In Deterministic Order
        # Why race-free & deadlock-free:
        # 1. All transactions sort seat labels alphabetically (ORDER BY label).
        #    This global canonical ordering eliminates the circular wait condition.
        # 2. SELECT ... FOR UPDATE acquires row locks on every requested seat.
        # 3. Under READ COMMITTED, if another transaction was in flight for any seat,
        #    this query waits for it to complete. Once committed, the row is re-read;
        #    if status != 'available', it is detected immediately.
        # ---------------------------------------------------------------------
        seat_rows = await conn.fetch(
            """
            SELECT label, status
            FROM seats
            WHERE show_id = $1 AND label = ANY($2::text[])
            ORDER BY label
            FOR UPDATE
            """,
            show_uuid,
            sorted_labels,
        )

        if len(seat_rows) != num_requested:
            reason_var.set("unknown_seat")
            found_labels = {r["label"] for r in seat_rows}
            missing = [lbl for lbl in sorted_labels if lbl not in found_labels]
            raise UnknownSeatException(f"Unknown seat labels in show: {', '.join(missing)}")

        unavailable = [r["label"] for r in seat_rows if r["status"] != "available"]
        if unavailable:
            reason_var.set("seat_taken")
            reservations_declined_total.labels(reason="seat_taken").inc()
            raise SeatTakenException(unavailable_seats=sorted(unavailable))

        # ---------------------------------------------------------------------
        # STEP 4: Seat State Transition & Reservation Creation
        # Why race-free:
        # We hold locks on the quota row and all seat rows. We update seats to
        # 'confirmed' and link them to the new reservation ID.
        # ---------------------------------------------------------------------
        reservation_id = uuid.uuid4()
        total_amount_paise = price_paise * num_requested
        created_at_dt = datetime.now(timezone.utc)

        response_body = {
            "id": str(reservation_id),
            "reservation_id": str(reservation_id),
            "show_id": str(show_uuid),
            "user_id": user_id,
            "seats": sorted_labels,
            "amount_paise": total_amount_paise,
            "status": "confirmed",
            "created_at": created_at_dt.isoformat(),
        }

        await conn.execute(
            """
            WITH upd_seats AS (
                UPDATE seats
                SET status = 'confirmed', reservation_id = $3, user_id = $4
                WHERE show_id = $1 AND label = ANY($2::text[]) AND status = 'available'
            ),
            ins_res AS (
                INSERT INTO reservations (
                    id, show_id, user_id, seats, amount_paise, status, created_at
                )
                VALUES ($3, $1, $4, $2, $5, 'confirmed', $6)
            )
            UPDATE idempotency_keys
            SET reservation_id = $3, response_body = $7::jsonb
            WHERE user_id = $4 AND key = $8;
            """,
            show_uuid,
            sorted_labels,
            reservation_id,
            user_id,
            total_amount_paise,
            created_at_dt,
            json.dumps(response_body),
            idempotency_key,
        )

        reason_var.set("confirmed")
        return response_body, 201, False

    result, status_code, is_replay = await execute_in_transaction_with_retry(_tx_operation)

    if status_code == 201:
        reservations_confirmed_total.inc()
        seats_confirmed_total.inc(num_requested)

    return result, status_code, is_replay
