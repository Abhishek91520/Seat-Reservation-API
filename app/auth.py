from datetime import datetime, timedelta, timezone
from typing import Optional

import jwt
from fastapi import Header, Request

from app.config import settings
from app.errors import ForbiddenException, UnauthorizedException
from app.logging import user_id_var


def create_user_token(user_id: str, expires_delta: Optional[timedelta] = None) -> str:
    if expires_delta is None:
        expires_delta = timedelta(days=7)
    now = datetime.now(timezone.utc)
    payload = {
        "sub": user_id,
        "role": "user",
        "iat": now,
        "exp": now + expires_delta,
    }
    return jwt.encode(payload, settings.jwt_secret, algorithm=settings.jwt_algorithm)


def get_current_user_id(
    request: Request,
    authorization: Optional[str] = Header(None),
) -> str:
    if not authorization:
        raise UnauthorizedException("Missing Authorization header")

    parts = authorization.split()
    if len(parts) != 2 or parts[0].lower() != "bearer":
        raise UnauthorizedException(
            "Invalid Authorization header format. Expected 'Bearer <token>'"
        )

    token = parts[1]
    try:
        payload = jwt.decode(
            token,
            settings.jwt_secret,
            algorithms=[settings.jwt_algorithm],
        )
        user_id = payload.get("sub")
        if not user_id:
            raise UnauthorizedException("Token payload missing subject ('sub')")
        user_id_str = str(user_id)
        user_id_var.set(user_id_str)
        request.state.user_id = user_id_str
        return user_id_str
    except jwt.ExpiredSignatureError as err:
        raise UnauthorizedException("Token has expired") from err
    except jwt.InvalidTokenError as err:
        raise UnauthorizedException("Invalid token") from err


def verify_admin_auth(authorization: Optional[str] = Header(None)) -> bool:
    if not authorization:
        raise UnauthorizedException("Missing Authorization header")

    parts = authorization.split()
    if len(parts) != 2 or parts[0].lower() != "bearer":
        raise UnauthorizedException(
            "Invalid Authorization header format. Expected 'Bearer <token>'"
        )

    token = parts[1]
    if token != settings.admin_token:
        raise ForbiddenException("Invalid administrator credentials")

    return True
