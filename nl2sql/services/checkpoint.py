"""Short-term thread store. PostgresSaver checks connections out of psycopg's pool."""
from langgraph.checkpoint.postgres import PostgresSaver
from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool

from .. import config


def open_pool() -> ConnectionPool:
    dsn = config.checkpoint_dsn()
    if not dsn:
        raise RuntimeError("NL2SQL_CHECKPOINT_DSN is required for the Postgres checkpointer.")
    # Min and max stay at psycopg's defaults. The saver calls pool.connection() itself.
    return ConnectionPool(
        dsn,
        kwargs={"autocommit": True, "prepare_threshold": 0, "row_factory": dict_row},
    )


def prepare(pool: ConnectionPool) -> PostgresSaver:
    checkpointer = PostgresSaver(pool)
    checkpointer.setup()
    return checkpointer
