"""Durable, expiring ownership with heartbeats and fenced state commits."""

import sqlite3
import threading
import time
import uuid
from contextlib import ExitStack, contextmanager
from functools import wraps
from pathlib import Path

_LOCAL = threading.local()
SCHEMA = """CREATE TABLE IF NOT EXISTS claims (
 resource TEXT PRIMARY KEY, owner TEXT NOT NULL, expires REAL NOT NULL);
 CREATE TABLE IF NOT EXISTS submissions (
 id TEXT PRIMARY KEY, dedupe TEXT NOT NULL, request TEXT NOT NULL,
 state TEXT NOT NULL, pid INTEGER, error TEXT, created REAL NOT NULL, updated REAL NOT NULL);
 CREATE UNIQUE INDEX IF NOT EXISTS submission_pending ON submissions(dedupe)
 WHERE state IN ('queued','active');"""


class ClaimBusy(RuntimeError):
    pass


class LeaseLost(RuntimeError):
    pass


class Claims:
    def __init__(self, root):
        self.root = Path(root).resolve()
        self.db = sqlite3.connect(self.root / "archive.sqlite3", timeout=30)
        self.db.executescript(SCHEMA)

    def acquire(self, resource, owner, *, ttl=90, clock=None):
        stamp = time.time() if clock is None else clock
        with self.db:
            result = self.db.execute(
                "INSERT INTO claims VALUES(?,?,?) ON CONFLICT(resource) DO UPDATE SET owner=excluded.owner,expires=excluded.expires WHERE claims.expires<=? OR claims.owner=?",
                (resource, owner, stamp + ttl, stamp, owner),
            )
        return result.rowcount == 1

    def renew(self, resource, owner, *, ttl=90, clock=None):
        stamp = time.time() if clock is None else clock
        with self.db:
            return (
                self.db.execute(
                    "UPDATE claims SET expires=? WHERE resource=? AND owner=? AND expires>?",
                    (stamp + ttl, resource, owner, stamp),
                ).rowcount
                == 1
            )

    def release(self, resource, owner):
        with self.db:
            self.db.execute("DELETE FROM claims WHERE resource=? AND owner=?", (resource, owner))

    def close(self):
        self.db.close()


def assert_owned(db, root):
    """The conditional write takes the DB write lock before any state mutation."""
    for (archive, resource), owner in getattr(_LOCAL, "owners", {}).items():
        if archive == str(Path(root).resolve()):
            if (
                db.execute(
                    "UPDATE claims SET owner=owner WHERE resource=? AND owner=? AND expires>?",
                    (resource, owner, time.time()),
                ).rowcount
                != 1
            ):
                raise LeaseLost("Worker ownership expired; checkpoints retained for another worker")


@contextmanager
def lease(root, resource, *, ttl=90, reentrant=True):
    root = Path(root).resolve()
    key = (str(root), resource)
    owners = getattr(_LOCAL, "owners", {})
    if key in owners:
        if not reentrant:
            raise ClaimBusy("Another worker owns this archive; nested execution is unavailable")
        yield owners[key]
        return
    claims = Claims(root)
    owner = uuid.uuid4().hex
    if not claims.acquire(resource, owner, ttl=ttl):
        claims.close()
        raise ClaimBusy(
            "Another worker owns this archive/job; wait for its lease to finish or expire"
        )
    _LOCAL.owners = {**owners, key: owner}
    stop = threading.Event()

    def heartbeat():
        connection = Claims(root)
        try:
            while not stop.wait(ttl / 3):
                if not connection.renew(resource, owner, ttl=ttl):
                    break
        finally:
            connection.close()

    thread = threading.Thread(target=heartbeat, daemon=True)
    thread.start()
    try:
        yield owner
        with claims.db:
            assert_owned(claims.db, root)
    finally:
        stop.set()
        thread.join(timeout=5)
        _LOCAL.owners = owners
        claims.release(resource, owner)
        claims.close()


def job_stage(function):
    @wraps(function)
    def run(store, job, *args, **kwargs):
        job_id = job["id"] if isinstance(job, dict) else job
        with lease(store.root, "archive"), lease(store.root, f"job:{job_id}"):
            return function(store, job, *args, **kwargs)

    return run


@contextmanager
def submission_gate(root, resource):
    """Briefly serialize concurrent submissions without treating a duplicate as a failure."""
    deadline = time.monotonic() + 5
    with ExitStack() as stack:
        while True:
            try:
                stack.enter_context(lease(root, resource))
                break
            except ClaimBusy:
                if time.monotonic() >= deadline:
                    raise
                time.sleep(0.02)
        yield
