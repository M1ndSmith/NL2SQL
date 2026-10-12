"""Sandboxed SQL execution.

Layers:
  1. Child process (spawn) with RLIMIT_AS / RLIMIT_CPU and a hard wall-clock kill.
  2. Least-privilege DB login for the caller's role, passed in by the graph.
  3. Per-session limits: statement_timeout, lock_timeout, read-only transaction for reads.
  4. Writes are dry-run in a transaction that is ROLLED BACK; commit only after approval.

For stronger isolation, run this worker in a container (no network except the DB,
read-only FS, cgroup limits) or against a read replica / ephemeral clone.
"""
import multiprocessing as mp
import queue

from .. import config

# SQLSTATE classes the LLM can plausibly fix. 42501 (permission denied) is fatal.
_RETRYABLE_CLASSES = {"42", "22", "57"}


class SandboxError(Exception):
    def __init__(self, message: str, retryable: bool):
        super().__init__(message)
        self.retryable = retryable


def _j(v):
    return v if v is None or isinstance(v, (int, float, str, bool)) else str(v)


def _worker(q, dsn: str, sql: str, mode: str, max_rows: int):
    try:
        import resource

        import psycopg

        resource.setrlimit(resource.RLIMIT_AS, (config.MEM_BYTES, config.MEM_BYTES))
        resource.setrlimit(resource.RLIMIT_CPU, (config.WALL_CLOCK_S, config.WALL_CLOCK_S))
        opts = (
            f"-c statement_timeout={config.STATEMENT_TIMEOUT_MS} "
            "-c lock_timeout=2000 -c idle_in_transaction_session_timeout=10000"
        )
        with psycopg.connect(dsn, options=opts, autocommit=False) as conn:
            if mode in ("read", "explain"):
                conn.read_only = True
            with conn.cursor() as cur:
                if mode == "explain":
                    cur.execute("EXPLAIN (FORMAT JSON) " + sql)
                    plan = cur.fetchone()[0]
                    q.put({"ok": True, "cost": float(plan[0]["Plan"]["Total Cost"])})
                elif mode == "read":
                    cur.execute(sql)
                    cols = [d.name for d in cur.description]
                    rows = [[_j(v) for v in r] for r in cur.fetchmany(max_rows)]
                    q.put({"ok": True, "columns": cols, "rows": rows})
                elif mode == "dry_write":
                    cur.execute(sql)
                    n = cur.rowcount
                    conn.rollback()
                    q.put({"ok": True, "rowcount": n})
                elif mode == "write":
                    cur.execute(sql)
                    n = cur.rowcount
                    conn.commit()
                    q.put({"ok": True, "rowcount": n})
                else:
                    q.put({"ok": False, "msg": f"unknown mode {mode}", "sqlstate": None})
    except Exception as e:  # noqa: BLE001 - everything is reported to the parent
        q.put({"ok": False, "msg": str(e)[:400], "sqlstate": getattr(e, "sqlstate", None)})


def run(mode: str, sql: str, *, max_rows: int = 500, dsn: str) -> dict:
    if not dsn:
        raise SandboxError("A database login is required.", False)
    ctx = mp.get_context("spawn")
    q = ctx.Queue()
    p = ctx.Process(target=_worker, args=(q, dsn, sql, mode, max_rows), daemon=True)
    p.start()
    try:
        out = q.get(timeout=config.WALL_CLOCK_S)
    except queue.Empty:
        p.kill()
        p.join(2)
        raise SandboxError("Execution exceeded the wall-clock limit or the worker died (memory/CPU limit).", True)
    p.join(2)
    if p.is_alive():
        p.kill()
    if not out["ok"]:
        state = out.get("sqlstate") or ""
        retryable = state[:2] in _RETRYABLE_CLASSES and state != "42501"
        raise SandboxError(f"DB error [{state or 'n/a'}]: {out['msg']}", retryable)
    return out
