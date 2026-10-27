"""Local, content-free result measurements and provider quota warnings.

Result value bytes are a lower bound on egress, not Neon's billing counter.
Nothing in this module connects to Postgres or the Neon API.
"""

import json
import os
import sqlite3
import sys
import threading
import time
from contextlib import closing, contextmanager
from contextvars import ContextVar
from datetime import UTC, datetime
from functools import wraps
from pathlib import Path

PROJECT = "twilight-dream-42009655"
THRESHOLDS = (3_000_000_000, 4_000_000_000)
_operation = ContextVar("database_operation", default="database.other")


def operation(name):
    """Label a synchronous operation with a fixed, source-owned name."""
    if name not in {
        "feed.read",
        "feed.snapshot",
        "rollup.index",
        "rollup.schedule",
        "agent.request",
        "agent.status",
        "agent.recover",
        "queue.read",
        "jobs.read",
        "backup.export",
        "operations.readiness",
        "operations.metrics",
    }:
        raise ValueError("Unknown database operation")

    def decorate(function):
        @wraps(function)
        def run(*args, **kwargs):
            token = _operation.set(name)
            try:
                return function(*args, **kwargs)
            finally:
                _operation.reset(token)

        return run

    return decorate


def _path(root):
    return Path(root) / "monitoring" / "transfer.sqlite3"


@contextmanager
def _open(root):
    path = _path(root)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    descriptor = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
    os.fchmod(descriptor, 0o600)
    os.close(descriptor)
    with closing(sqlite3.connect(path, timeout=3)) as db:
        db.executescript("""
        CREATE TABLE IF NOT EXISTS measurements (
          month TEXT, operation TEXT, queries INTEGER, rows INTEGER,
          result_bytes INTEGER, errors INTEGER, seconds REAL,
          PRIMARY KEY(month, operation));
        CREATE TABLE IF NOT EXISTS quota (
          project TEXT, period_start TEXT, period_end TEXT, used_bytes INTEGER,
          observed_at TEXT, warned_bytes INTEGER,
          PRIMARY KEY(project, period_start));
        """)
        with db:
            yield db


class Meter:
    """Batch per connection; flush on close, 1 MB, or the next query after 60s."""

    def __init__(self, root):
        self.root = root
        self.pending = {}
        self.last_flush = time.monotonic()
        self.lock = threading.RLock()
        self.reported_failure = False

    def record(self, cursor, seconds, *, error=False):
        # Instrumentation failures must never replace a query result or exception.
        try:
            with self.lock:
                rows = size = 0
                result = cursor.pgresult if cursor is not None else None
                if result is not None:
                    rows = result.ntuples
                    for row in range(rows):
                        for column in range(result.nfields):
                            value = result.get_value(row, column)
                            if value is not None:
                                size += len(value)
                month = datetime.now(UTC).strftime("%Y-%m")
                key = (month, _operation.get())
                counts = self.pending.setdefault(key, [0, 0, 0, 0, 0.0])
                for index, value in enumerate((1, rows, size, int(error), seconds)):
                    counts[index] += value
                if (
                    sum(c[2] for c in self.pending.values()) >= 1_000_000
                    or time.monotonic() - self.last_flush >= 60
                ):
                    self.flush()
        except Exception:
            self._failure()

    def _failure(self):
        if not self.reported_failure:
            self.reported_failure = True
            try:
                print('{"event":"database_transfer_measurement_unavailable"}', file=sys.stderr)
            except Exception:
                pass

    def flush(self):
        with self.lock:
            if not self.pending:
                return
            try:
                with _open(self.root) as db:
                    for (month, name), counts in self.pending.items():
                        db.execute(
                            """
                            INSERT INTO measurements VALUES (?,?,?,?,?,?,?)
                            ON CONFLICT(month,operation) DO UPDATE SET
                              queries=queries+excluded.queries, rows=rows+excluded.rows,
                              result_bytes=result_bytes+excluded.result_bytes,
                              errors=errors+excluded.errors, seconds=seconds+excluded.seconds
                        """,
                            (month, name, *counts),
                        )
                    db.execute("""DELETE FROM measurements WHERE month NOT IN
                        (SELECT DISTINCT month FROM measurements ORDER BY month DESC LIMIT 3)""")
                completed, self.pending = self.pending, {}
                self.last_flush = time.monotonic()
                for (month, name), counts in completed.items():
                    print(
                        json.dumps(
                            dict(
                                event="database_transfer",
                                month=month,
                                operation=name,
                                queries=counts[0],
                                rows=counts[1],
                                result_bytes=counts[2],
                                errors=counts[3],
                                seconds=round(counts[4], 6),
                            )
                        ),
                        file=sys.stderr,
                        flush=True,
                    )
                self.reported_failure = False
            except Exception:
                self._failure()


def metrics(root):
    """Read persisted counters without touching the network database."""
    path = _path(root)
    if not path.exists():
        return {"measurement": "postgres_result_value_bytes", "operations": []}
    with closing(sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)) as db:
        db.row_factory = sqlite3.Row
        rows = db.execute("SELECT * FROM measurements ORDER BY month DESC,result_bytes DESC")
        return {"measurement": "postgres_result_value_bytes", "operations": [dict(r) for r in rows]}


def observe(root, *, project, used_bytes, period_start, period_end, now=None):
    """Emit each 3/4 GB warning once per authoritative billing period.

    Call only with current provider metadata, never application estimates.
    """
    now = now or datetime.now(UTC)
    start = datetime.fromisoformat(period_start.replace("Z", "+00:00"))
    end = datetime.fromisoformat(period_end.replace("Z", "+00:00"))
    if (
        project != PROJECT
        or type(used_bytes) is not int
        or used_bytes < 0
        or start.tzinfo is None
        or end.tzinfo is None
        or not start <= now < end
        or not 27 <= (end - start).days <= 32
    ):
        raise ValueError("Expected current TubeScribe project usage and billing period")
    period_start, period_end = start.astimezone(UTC).isoformat(), end.astimezone(UTC).isoformat()
    with _open(root) as db:
        db.execute("BEGIN IMMEDIATE")
        previous = db.execute(
            "SELECT used_bytes,warned_bytes,period_end FROM quota WHERE project=? AND period_start=?",
            (project, period_start),
        ).fetchone()
        if previous and previous[2] != period_end:
            raise ValueError("Billing period end changed; verify provider metadata")
        used_bytes = max(used_bytes, previous[0] if previous else 0)
        warned = previous[1] if previous else 0
        warnings = [value for value in THRESHOLDS if warned < value <= used_bytes]
        db.execute(
            "INSERT OR REPLACE INTO quota VALUES (?,?,?,?,?,?)",
            (
                project,
                period_start,
                period_end,
                used_bytes,
                now.isoformat(),
                max([warned, *warnings]),
            ),
        )
    return dict(
        project=project,
        used_bytes=used_bytes,
        limit_bytes=5_000_000_000,
        period_start=period_start,
        period_end=period_end,
        warnings=warnings,
    )
