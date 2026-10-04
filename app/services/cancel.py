import uuid
from typing import Any, Dict

import asyncpg
import structlog

from app.db import execute_in_transaction_with_retry
from app.errors import NotFoundException
from app.metrics import reservations_cancelled_total

logger = structlog.get_logger(__name__)


async def cancel_reservation(user_id: str, reservation_id_str: str) -> Dict[str, Any]:
    """Cancels a reservation atomically inside a single transaction.

    Lock Order (strictly identical to reserve):
    1. Reservation row FOR UPDATE
    2. User quota row FOR UPDATE
    3. Seat rows ORDER BY label FOR UPDATE

    Returns:
        Cancelled reservation dictionary with status='cancelled'
    """
    try:
        reservation_uuid = uuid.UUID(reservation_id_str)
    except (ValueError, AttributeError):
        # Do not leak existence
        raise NotFoundException(f"Reservation {reservation_id_str} not found") from None

    async def _tx_operation(conn: asyncpg.Connection) -> Dict[str, Any]:
        # ---------------------------------------------------------------------
        # LOCK STEP 1: Lock Reservation Row
        # Why race-free:
        # Serializes concurrent cancel operations on the same reservation row.
        # Not found or not owned by caller returns 404 (does not leak existence).
        # ---------------------------------------------------------------------
        res_row = await conn.fetchrow(
            """
            SELECT id, show_id, user_id, seats, amount_paise, status, created_at, cancelled_at
            FROM reservations
            WHERE id = $1
            FOR UPDATE
            """,
            reservation_uuid,
        )

        if not res_row or res_row["user_id"] != user_id:
            # 404 whether missing or foreign to prevent leaking resource existence
            raise NotFoundException(f"Reservation {reservation_id_str} not found")

        # Idempotent double-cancel returns 200 OK immediately
        if res_row["status"] == "cancelled":
            return {
                "id": str(res_row["id"]),
                "show_id": str(res_row["show_id"]),
                "user_id": res_row["user_id"],
                "seats": res_row["seats"],
                "amount_paise": res_row["amount_paise"],
                "status": "cancelled",
                "created_at": res_row["created_at"].isoformat(),
                "cancelled_at": (
                    res_row["cancelled_at"].isoformat() if res_row["cancelled_at"] else None
                ),
            }

        show_id = res_row["show_id"]
        seats_list = res_row["seats"]
        num_seats = len(seats_list)
        sorted_seats = sorted(seats_list)

        # ---------------------------------------------------------------------
        # LOCK STEP 2: Lock User Quota Row
        # Why race-free:
        # Prevents race conditions with concurrent reservations or cancels by
        # the same user on this show.
        # ---------------------------------------------------------------------
        await conn.fetchrow(
            """
            SELECT held
            FROM user_show_quota
            WHERE show_id = $1 AND user_id = $2
            FOR UPDATE
            """,
            show_id,
            user_id,
        )

        # ---------------------------------------------------------------------
        # LOCK STEP 3: Lock Seat Rows In Lexicographical Order
        # Why race-free & deadlock-free:
        # Acquiring locks in ORDER BY label matches the reserve locking order,
        # ensuring cancel and reserve operations never deadlock against each other.
        # ---------------------------------------------------------------------
        await conn.fetch(
            """
            SELECT label, status, reservation_id
            FROM seats
            WHERE show_id = $1 AND label = ANY($2::text[])
            ORDER BY label
            FOR UPDATE
            """,
            show_id,
            sorted_seats,
        )

        # ---------------------------------------------------------------------
        # STEP 4: Release Seats With Reservation ID Guard
        # Why race-free:
        # Crucial guard: `AND reservation_id = $3`. If by any chance a seat was
        # re-booked by another transaction, this cancel will NEVER touch or free it.
        # ---------------------------------------------------------------------
        await conn.execute(
            """
            UPDATE seats
            SET status = 'available', reservation_id = NULL, user_id = NULL
            WHERE show_id = $1 AND label = ANY($2::text[]) AND reservation_id = $3
            """,
            show_id,
            sorted_seats,
            reservation_uuid,
        )

        # ---------------------------------------------------------------------
        # STEP 5: Decrement Quota & Mark Reservation Cancelled
        # Why race-free:
        # Decrements the exact number of freed seats without risking negative values.
        # ---------------------------------------------------------------------
        await conn.execute(
            """
            UPDATE user_show_quota
            SET held = GREATEST(0, held - $3)
            WHERE show_id = $1 AND user_id = $2
            """,
            show_id,
            user_id,
            num_seats,
        )

        cancelled_row = await conn.fetchrow(
            """
            UPDATE reservations
            SET status = 'cancelled', cancelled_at = now()
            WHERE id = $1
            RETURNING cancelled_at
            """,
            reservation_uuid,
        )

        cancelled_at_str = cancelled_row["cancelled_at"].isoformat()

        return {
            "id": str(res_row["id"]),
            "show_id": str(res_row["show_id"]),
            "user_id": res_row["user_id"],
            "seats": res_row["seats"],
            "amount_paise": res_row["amount_paise"],
            "status": "cancelled",
            "created_at": res_row["created_at"].isoformat(),
            "cancelled_at": cancelled_at_str,
        }

    cancelled_res = await execute_in_transaction_with_retry(_tx_operation)
    reservations_cancelled_total.inc()
    return cancelled_res
