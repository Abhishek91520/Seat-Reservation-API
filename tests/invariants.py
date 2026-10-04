import asyncpg


async def assert_invariants(conn: asyncpg.Connection, show_id: str) -> None:
    """Verifies all hard consistency and isolation invariants in a single snapshot."""
    # 1. available + held + confirmed == total_seats == count(seats)
    show_row = await conn.fetchrow(
        """
        SELECT total_seats, per_user_limit FROM shows WHERE id = $1
        """,
        show_id,
    )
    assert show_row is not None, f"Show {show_id} not found"
    total_seats = show_row["total_seats"]
    per_user_limit = show_row["per_user_limit"]

    seat_counts = await conn.fetchrow(
        """
        SELECT
            count(*)::int AS total,
            count(*) FILTER (WHERE status = 'available')::int AS available,
            count(*) FILTER (WHERE status = 'held')::int AS held,
            count(*) FILTER (WHERE status = 'confirmed')::int AS confirmed
        FROM seats
        WHERE show_id = $1
        """,
        show_id,
    )

    assert seat_counts["total"] == total_seats, (
        f"Seat count mismatch: seats in DB={seat_counts['total']}, show.total_seats={total_seats}"
    )
    assert (
        seat_counts["available"] + seat_counts["held"] + seat_counts["confirmed"] == total_seats
    ), (
        f"Seat invariant violated: available({seat_counts['available']}) + "
        f"held({seat_counts['held']}) + confirmed({seat_counts['confirmed']}) "
        f"!= total({total_seats})"
    )

    # 2. Every confirmed seat has a reservation_id pointing to a reservation with status='confirmed'
    # whose seats array contains it.
    orphan_seats = await conn.fetch(
        """
        SELECT s.label, s.reservation_id
        FROM seats s
        LEFT JOIN reservations r ON s.reservation_id = r.id
        WHERE s.show_id = $1
          AND s.status = 'confirmed'
          AND (r.id IS NULL OR r.status != 'confirmed' OR NOT (s.label = ANY(r.seats)))
        """,
        show_id,
    )
    assert len(orphan_seats) == 0, (
        f"Confirmed seats without valid confirmed reservation: {[dict(r) for r in orphan_seats]}"
    )

    # 3. Every confirmed reservation's seats are all marked confirmed for the same user.
    mismatched_res = await conn.fetch(
        """
        SELECT r.id, r.user_id, s.label, s.status, s.user_id AS seat_user_id
        FROM reservations r
        JOIN seats s ON s.show_id = r.show_id AND s.label = ANY(r.seats)
        WHERE r.show_id = $1
          AND r.status = 'confirmed'
          AND (s.status != 'confirmed' OR s.user_id != r.user_id OR s.reservation_id != r.id)
        """,
        show_id,
    )
    assert len(mismatched_res) == 0, (
        f"Reservation seats not properly confirmed to owner: {[dict(r) for r in mismatched_res]}"
    )

    # 4. No seat label appears in two confirmed reservations of the same show.
    duplicate_seats = await conn.fetch(
        """
        SELECT unnest(seats) AS label, count(*) AS cnt
        FROM reservations
        WHERE show_id = $1 AND status = 'confirmed'
        GROUP BY label
        HAVING count(*) > 1
        """,
        show_id,
    )
    assert len(duplicate_seats) == 0, (
        f"Seat label confirmed in multiple reservations: {[dict(r) for r in duplicate_seats]}"
    )

    # 5. user_show_quota.held equals actual confirmed seat count per user, and is <= per_user_limit.
    quota_mismatches = await conn.fetch(
        """
        WITH actual_user_seats AS (
            SELECT user_id, count(*)::int AS actual_held
            FROM seats
            WHERE show_id = $1 AND status = 'confirmed'
            GROUP BY user_id
        ),
        show_quotas AS (
            SELECT user_id, held
            FROM user_show_quota
            WHERE show_id = $1
        )
        SELECT
            COALESCE(q.user_id, a.user_id) AS user_id,
            COALESCE(q.held, 0) AS quota_held,
            COALESCE(a.actual_held, 0) AS actual_held
        FROM show_quotas q
        FULL OUTER JOIN actual_user_seats a ON q.user_id = a.user_id
        WHERE COALESCE(q.held, 0) != COALESCE(a.actual_held, 0)
           OR COALESCE(q.held, 0) > $2
        """,
        show_id,
        per_user_limit,
    )
    assert len(quota_mismatches) == 0, (
        f"Quota mismatches detected: {[dict(r) for r in quota_mismatches]}"
    )

    # 6. Every idempotency_keys.reservation_id exists.
    broken_idempotency = await conn.fetch(
        """
        SELECT k.user_id, k.key, k.reservation_id
        FROM idempotency_keys k
        LEFT JOIN reservations r ON k.reservation_id = r.id
        WHERE k.reservation_id IS NOT NULL AND r.id IS NULL
        """
    )
    assert len(broken_idempotency) == 0, (
        f"Idempotency keys with invalid reservation_id: {[dict(r) for r in broken_idempotency]}"
    )
