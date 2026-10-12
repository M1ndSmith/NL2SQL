"""Append-only record of SQL that actually ran. Analyst and ops cannot write it."""
import logging

from .. import config

logger = logging.getLogger(__name__)

_INSERT = (
    "INSERT INTO events (user_id, role, sql, rowcount, decision) "
    "VALUES (%s, %s, %s, %s, %s)"
)
_SELECT = (
    "SELECT created_at, sql, rowcount, decision FROM events "
    "WHERE user_id = %s ORDER BY created_at DESC, id DESC"
)


def record(user_id: str, role: str, sql: str, rowcount: int | None, decision: str, *, connect=None) -> None:
    dsn = config.audit_dsn()
    if not dsn:
        return
    opener = connect or _connect
    try:
        with opener(dsn) as conn:
            with conn.cursor() as cur:
                cur.execute(_INSERT, (user_id or "", role, sql, rowcount, decision))
            conn.commit()
    except Exception:
        logger.exception("audit_write_failed")


def list_for(user_id: str, *, connect=None) -> list[dict]:
    dsn = config.audit_dsn()
    if not dsn:
        return []
    opener = connect or _connect
    with opener(dsn) as conn:
        with conn.cursor() as cur:
            cur.execute(_SELECT, (user_id or "",))
            found = cur.fetchall()
    return [_event(row) for row in found]


def _event(row) -> dict:
    created = row[0]
    stamp = created.isoformat() if hasattr(created, "isoformat") else str(created)
    return {"created_at": stamp, "sql": row[1], "rowcount": row[2], "decision": row[3]}


def _connect(dsn: str):
    import psycopg

    return psycopg.connect(dsn)
