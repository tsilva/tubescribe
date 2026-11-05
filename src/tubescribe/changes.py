"""Small durable revision reads and an idle PostgreSQL notification subscription."""

import os
import time
from contextlib import contextmanager
from urllib.parse import urlsplit


def revisions(db):
    return dict(tuple(row) for row in db.execute("SELECT scope,revision FROM catalogue_revisions"))


@contextmanager
def notifications(*, stop=None):
    """LISTEN must use a direct session, never a transaction-pool connection."""
    import psycopg

    url = (
        os.environ.get("TUBESCRIBE_DATABASE_UNPOOLED_URL") or os.environ["TUBESCRIBE_DATABASE_URL"]
    )
    if "-pooler." in (urlsplit(url).hostname or ""):
        raise ValueError("Queue notifications require TUBESCRIBE_DATABASE_UNPOOLED_URL")
    with psycopg.connect(url, autocommit=True, connect_timeout=5) as connection:
        schema = connection.execute("SELECT current_schema()").fetchone()[0]
        connection.execute("LISTEN tubescribe_changes")

        def wait(deadline=None):
            # Notifications are hints; the durable queue is checked after every
            # wake and reconnect. Subscribe BEFORE checking it to close the race.
            while True:
                if stop is not None and stop.is_set():
                    return
                timeout = None if deadline is None else max(0, deadline - time.time())
                if timeout == 0:
                    return
                # Only the local socket wait is bounded for graceful shutdown.
                # A quiet timeout must not wake the dispatcher and query the DB.
                if stop is not None:
                    timeout = min(timeout, 1) if timeout is not None else 1
                stream = connection.notifies(timeout=timeout, stop_after=1)
                try:
                    notice = next(stream, None)
                finally:
                    stream.close()
                if notice is not None and notice.payload == schema:
                    return
                if notice is None and (
                    stop is None or (deadline is not None and time.time() >= deadline)
                ):
                    return

        yield wait


def queue_deadline(store):
    """Only actual expiry/retry deadlines cause idle timers; no periodic scans."""
    from .database import clock

    stamp = clock(store.db)
    row = store.db.execute(
        "SELECT MIN(deadline) FROM ("
        "SELECT expires AS deadline FROM claims WHERE expires>? AND "
        "(resource='archive' OR resource LIKE 'video:%' OR resource LIKE 'job:%' "
        "OR resource LIKE 'submission:%' OR resource LIKE 'submission-retry:%') UNION ALL "
        "SELECT updated+90 AS deadline FROM submissions WHERE "
        "(state='active' OR (state='queued' AND pid IS NOT NULL)) AND updated+90>=?"
        ") AS deadlines",
        (stamp, stamp),
    ).fetchone()
    deadlines = [
        value for value in (row[0], getattr(store, "_rollup_wake_at", None)) if value is not None
    ]
    return min(deadlines) + 0.01 if deadlines else None
