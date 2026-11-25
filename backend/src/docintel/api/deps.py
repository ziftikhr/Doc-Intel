"""FastAPI dependencies: database session and optional API-key auth."""

from __future__ import annotations

import secrets
from typing import Iterator, Optional

from fastapi import Header, HTTPException, Request
from sqlalchemy.orm import Session


def get_session(request: Request) -> Iterator[Session]:
    session = request.app.state.session_factory()
    try:
        yield session
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def require_api_key(request: Request, x_api_key: Optional[str] = Header(None)) -> None:
    expected = request.app.state.settings.api_key
    if expected and not (x_api_key and secrets.compare_digest(x_api_key, expected)):
        raise HTTPException(status_code=401, detail="missing or invalid X-API-Key header")
