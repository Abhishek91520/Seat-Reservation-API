import json
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, Header, Response, status
from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.auth import get_current_user_id
from app.errors import ValidationException
from app.services.cancel import cancel_reservation
from app.services.reserve import reserve_seats

router = APIRouter(prefix="/reservations", tags=["reservations"])


class CreateReservationRequest(BaseModel):
    model_config = ConfigDict(extra="ignore")  # Ignores spoofed body fields like 'user_id'

    show_id: str = Field(..., description="Target Show UUID")
    seats: List[str] = Field(..., min_length=1, description="List of seat labels to reserve")
    idempotency_key: Optional[str] = Field(
        None, description="Idempotency key (can also be supplied via Idempotency-Key header)"
    )

    @field_validator("seats")
    @classmethod
    def validate_seats(cls, v: List[str]) -> List[str]:
        if not v:
            raise ValueError("Must provide at least one seat")
        seen = set()
        cleaned: List[str] = []
        for s in v:
            lbl = s.strip()
            if not lbl:
                raise ValueError("Seat label cannot be empty or whitespace")
            if lbl in seen:
                raise ValueError(f"Duplicate seat label in request: '{lbl}'")
            seen.add(lbl)
            cleaned.append(lbl)
        return cleaned


@router.post(
    "",
    status_code=status.HTTP_201_CREATED,
)
async def create_reservation(
    body: CreateReservationRequest,
    idempotency_key_header: Optional[str] = Header(None, alias="Idempotency-Key"),
    user_id: str = Depends(get_current_user_id),
) -> Response:
    # Header takes precedence over body
    effective_idempotency_key = idempotency_key_header or body.idempotency_key
    if not effective_idempotency_key or not effective_idempotency_key.strip():
        raise ValidationException(
            "Idempotency key must be provided via 'Idempotency-Key' header or body field"
        )

    res_body, status_code, is_replay = await reserve_seats(
        user_id=user_id,
        show_id_str=body.show_id,
        seats=body.seats,
        idempotency_key=effective_idempotency_key.strip(),
    )

    headers = {}
    if is_replay:
        headers["Idempotent-Replayed"] = "true"

    return Response(
        content=json.dumps(res_body),
        status_code=status_code,
        headers=headers,
        media_type="application/json",
    )


@router.post(
    "/{reservation_id}/cancel",
    status_code=status.HTTP_200_OK,
)
async def cancel_existing_reservation(
    reservation_id: str,
    user_id: str = Depends(get_current_user_id),
) -> Dict[str, Any]:
    return await cancel_reservation(user_id=user_id, reservation_id_str=reservation_id)
