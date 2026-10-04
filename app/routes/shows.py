import json
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, Header, Request, Response, status
from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.auth import get_current_user_id, verify_admin_auth
from app.errors import ValidationException
from app.services.reserve import reserve_seats
from app.services.shows import create_show, get_show_by_id

router = APIRouter(prefix="/shows", tags=["shows"])


class CreateShowRequest(BaseModel):
    model_config = ConfigDict(strict=True)

    name: str = Field(..., min_length=1, max_length=200, description="Show title")
    price_paise: int = Field(..., gt=0, description="Seat price in integer paise")
    per_user_limit: int = Field(default=4, ge=1, le=100, description="Maximum seats per user")
    seats: List[str] = Field(
        ..., min_length=1, max_length=100_000, description="List of seat labels"
    )

    @field_validator("name")
    @classmethod
    def validate_name(cls, v: str) -> str:
        cleaned = v.strip()
        if not cleaned:
            raise ValueError("Show name cannot be empty or whitespace only")
        return cleaned

    @field_validator("seats")
    @classmethod
    def validate_seats(cls, v: List[str]) -> List[str]:
        if not v:
            raise ValueError("At least one seat label must be provided")
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


class ShowResponse(BaseModel):
    id: str
    name: str
    price_paise: int
    per_user_limit: int
    total_seats: int
    created_at: str


class ShowStateResponse(BaseModel):
    id: str
    name: str
    price_paise: int
    per_user_limit: int
    total_seats: int
    available: int
    held: int
    confirmed: int
    seats: Dict[str, str]


class ShowReserveRequest(BaseModel):
    model_config = ConfigDict(extra="ignore")

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
    response_model=ShowResponse,
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(verify_admin_auth)],
)
async def create_new_show(body: CreateShowRequest) -> Dict[str, Any]:
    return await create_show(
        name=body.name,
        price_paise=body.price_paise,
        per_user_limit=body.per_user_limit,
        seats=body.seats,
    )


@router.get(
    "/{show_id}",
    response_model=ShowStateResponse,
    status_code=status.HTTP_200_OK,
)
async def get_show_state(show_id: str) -> Dict[str, Any]:
    return await get_show_by_id(show_id)


@router.post(
    "/{show_id}/reserve",
    status_code=status.HTTP_201_CREATED,
)
async def reserve_show_seats(
    show_id: str,
    request: Request,
    body: ShowReserveRequest,
    idempotency_key_header: Optional[str] = Header(None, alias="Idempotency-Key"),
    user_id: str = Depends(get_current_user_id),
) -> Response:
    request.state.show_id = str(show_id)
    request.state.seats = body.seats
    request.state.user_id = user_id

    effective_idempotency_key = idempotency_key_header or body.idempotency_key
    if not effective_idempotency_key or not effective_idempotency_key.strip():
        request.state.reason = "validation_error"
        raise ValidationException(
            "Idempotency key must be provided via 'Idempotency-Key' header "
            "or 'idempotency_key' in request body"
        )
    effective_idempotency_key = effective_idempotency_key.strip()

    res_body, status_code, is_replay = await reserve_seats(
        user_id=user_id,
        show_id_str=show_id,
        seats=body.seats,
        idempotency_key=effective_idempotency_key,
    )

    headers = {}
    if is_replay:
        headers["Idempotent-Replayed"] = "true"
        request.state.reason = "idempotent_replay"
    else:
        request.state.reason = None

    return Response(
        content=json.dumps(res_body),
        status_code=status_code,
        headers=headers,
        media_type="application/json",
    )
