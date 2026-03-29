from fastapi import Request, HTTPException
from fastapi.security import HTTPBearer
from jose import jwt, JWTError
from typing import Optional
from dataclasses import dataclass


@dataclass
class CurrentUser:
    user_id: int
    company_id: Optional[int]
    role: str
    email: str


security = HTTPBearer()


class AuthMiddleware:
    """Extract and validate JWT from Authorization header.

    Note: Real authentication is handled by MCAPI upstream.
    This middleware decodes the token to extract user context
    for guardrails and audit logging.
    """

    def __init__(self, secret_key: str = "", algorithm: str = "HS256"):
        self.secret_key = secret_key
        self.algorithm = algorithm

    async def get_current_user(self, request: Request) -> CurrentUser:
        auth_header = request.headers.get("Authorization", "")
        if not auth_header.startswith("Bearer "):
            raise HTTPException(status_code=401, detail="Missing or invalid Authorization header")

        token = auth_header[7:]  # Strip "Bearer "

        try:
            # Decode without verification — MCAPI handles real auth upstream
            payload = jwt.decode(
                token,
                self.secret_key or "noverify",
                algorithms=[self.algorithm],
                options={"verify_signature": bool(self.secret_key)},
            )
        except JWTError as e:
            raise HTTPException(status_code=401, detail=f"Invalid token: {e}")

        user_id = payload.get("user_id") or payload.get("sub")
        if user_id is None:
            raise HTTPException(status_code=401, detail="Token missing user_id")

        return CurrentUser(
            user_id=int(user_id),
            company_id=int(payload["company_id"]) if payload.get("company_id") else None,
            role=payload.get("role", "seller"),
            email=payload.get("email", ""),
        )
