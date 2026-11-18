import atexit
from functools import lru_cache

from psycopg.rows import dict_row
from psycopg.types.json import Jsonb
from psycopg_pool import ConnectionPool

from .config import settings


@lru_cache
def pool() -> ConnectionPool:
    p = ConnectionPool(
        settings().linkmind_database_url,
        kwargs={"row_factory": dict_row, "autocommit": False},
        min_size=1,
        max_size=5,
        open=True,
    )
    atexit.register(p.close)  # close before interpreter shutdown, not in __del__
    return p


def log_inbound(tool: str, payload: dict) -> None:
    """Phase 0 probe: keep a verbatim record of every MCP call."""
    with pool().connection() as conn:
        conn.execute("INSERT INTO inbound_event (tool, payload) VALUES (%s, %s)", (tool, Jsonb(payload)))
