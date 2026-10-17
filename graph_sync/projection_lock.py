"""Cross-process lock shared by all PostgreSQL-to-Neo4j projection writers."""

from contextlib import contextmanager
from collections.abc import Iterator

import psycopg


# Stable namespace key (ASCII "THPTPROJ"). It is intentionally a single global
# lock so full syncs cannot replay a stale snapshot over a lifecycle job.
GRAPH_PROJECTION_LOCK_KEY = 0x5448505450524F4A


@contextmanager
def graph_projection_lock(database_url: str) -> Iterator[None]:
    """Serialize graph writes without adding a database table or migration."""
    with psycopg.connect(database_url) as connection:
        connection.execute("SELECT pg_advisory_lock(%s)", (GRAPH_PROJECTION_LOCK_KEY,))
        connection.commit()
        try:
            yield
        finally:
            connection.execute("SELECT pg_advisory_unlock(%s)", (GRAPH_PROJECTION_LOCK_KEY,))
            connection.commit()
