from datetime import timedelta

from fastapi import APIRouter, status
from pydantic import BaseModel, Field, field_validator

from app.auth import create_user_token

router = APIRouter(prefix="/auth", tags=["auth"])

DEFAULT_EXPIRATION_DAYS = 7


class TokenRequest(BaseModel):
    user_id: str = Field(..., description="User ID for which to mint a JWT")

    @field_validator("user_id")
    @classmethod
    def validate_user_id(cls, v: str) -> str:
        cleaned = v.strip()
        if not cleaned:
            raise ValueError("user_id must not be empty or whitespace")
        if len(cleaned) > 128:
            raise ValueError("user_id must not exceed 128 characters")
        return cleaned


class TokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    expires_in: int


@router.post("/token", response_model=TokenResponse, status_code=status.HTTP_200_OK)
async def generate_token(body: TokenRequest):
    expires_delta = timedelta(days=DEFAULT_EXPIRATION_DAYS)
    token = create_user_token(user_id=body.user_id, expires_delta=expires_delta)
    return TokenResponse(
        access_token=token,
        token_type="bearer",
        expires_in=int(expires_delta.total_seconds()),
    )
