from typing import Any, Dict, List

from fastapi import APIRouter, Depends, status
from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.auth import verify_admin_auth
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
