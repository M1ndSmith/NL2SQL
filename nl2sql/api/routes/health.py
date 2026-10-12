"""Liveness is the process. Readiness is Redis and the auth database."""
import logging

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from ...services import auth, limits

logger = logging.getLogger(__name__)
router = APIRouter()


@router.get("/health/live")
def live() -> dict:
    return {"status": "ok"}


@router.get("/health/ready")
def ready(request: Request):
    check = getattr(request.app.state, "readiness", None)
    ok = check() if check is not None else _default_ready()
    if not ok:
        return JSONResponse(
            status_code=503,
            content={"error": {"code": "not_ready", "message": "dependencies are unavailable"}},
        )
    return {"status": "ok"}


def _default_ready() -> bool:
    try:
        limits.client().ping()
    except Exception as exc:
        logger.error("redis_ping_failed error_type=%s", type(exc).__name__)
        return False
    return auth.ping()
