"""Password check against the auth database. The role is reloaded on every request."""
import logging
from dataclasses import dataclass

from .. import config

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class Principal:
    user_id: str
    role: str


class AuthError(Exception):
    def __init__(self, message: str = "authentication failed"):
        super().__init__(message)


def authenticate(user_id: str, password: str, *, lookup=None) -> Principal:
    if not (password or "").strip():
        raise AuthError()
    finder = lookup or _lookup_password
    role = _call(finder, user_id, password)
    return _principal(user_id, role)


def get_user(user_id: str, *, lookup=None) -> Principal:
    finder = lookup or _lookup_user
    role = _call(finder, user_id)
    return _principal(user_id, role)


def _call(finder, user_id: str, password: str | None = None) -> str | None:
    cleaned = (user_id or "").strip()
    if not cleaned:
        raise AuthError()
    try:
        if password is None:
            return finder(cleaned)
        return finder(cleaned, password)
    except AuthError:
        raise
    except Exception as exc:
        logger.error("auth_lookup_failed error_type=%s", type(exc).__name__)
        raise AuthError() from None


def _principal(user_id: str, role: str | None) -> Principal:
    cleaned = (user_id or "").strip()
    if not cleaned or not role or role not in config.ROLES:
        raise AuthError()
    return Principal(cleaned, role)


def _connect():
    dsn = config.auth_dsn()
    if not dsn:
        raise RuntimeError("NL2SQL_AUTH_DSN is required.")
    import psycopg

    return psycopg.connect(dsn, connect_timeout=5)


def _lookup_password(user_id: str, password: str) -> str | None:
    with _connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT role FROM users WHERE user_id = %s AND password_hash = crypt(%s, password_hash)",
                (user_id, password),
            )
            row = cur.fetchone()
    return row[0] if row else None


def _lookup_user(user_id: str) -> str | None:
    with _connect() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT role FROM users WHERE user_id = %s", (user_id,))
            row = cur.fetchone()
    return row[0] if row else None


def ping() -> bool:
    try:
        with _connect() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT 1")
                cur.fetchone()
        return True
    except Exception as exc:
        logger.error("auth_ping_failed error_type=%s", type(exc).__name__)
        return False
