"""Bearer-token authentication for the worker API."""

from __future__ import annotations

import hmac

from fastapi import Header, HTTPException

from . import config


def require_token(authorization: str | None = Header(default=None)) -> None:
    supplied = authorization.removeprefix("Bearer ") if authorization else ""
    if not config.API_TOKEN or not hmac.compare_digest(supplied, config.API_TOKEN):
        raise HTTPException(status_code=401, detail="Invalid worker credential")
