"""Opaque Redis sessions. The token stores a user id, never a role."""
import secrets

from .. import config
from . import limits


def create_session(user_id: str, *, store=None) -> tuple[str, int]:
    token = secrets.token_urlsafe(32)
    ttl = config.session_ttl_seconds()
    (store or limits.client()).set(f"session:{token}", user_id, ex=ttl)
    return token, ttl


def read_session(token: str, *, store=None) -> str | None:
    if not (token or "").strip():
        return None
    value = (store or limits.client()).get(f"session:{token}")
    if value is None:
        return None
    return str(value)


def delete_session(token: str, *, store=None) -> None:
    if (token or "").strip():
        (store or limits.client()).delete(f"session:{token}")
