"""Durable, expiring ownership with heartbeats and fenced state commits."""

import threading
import time
import uuid
from contextlib import ExitStack, contextmanager
from contextvars import ContextVar
from functools import wraps
from pathlib import Path

from .repository import SQLiteClaims as Claims

CURRENT_SUBMISSION = ContextVar("submission", default=None)
_LOCAL = threading.local()


class ClaimBusy(RuntimeError):
    pass


class LeaseLost(RuntimeError):
    pass


def assert_owned(db, root):
    """The conditional write takes the DB write lock before any state mutation."""
    from .config import database_path

    for (archive, resource), owner in getattr(_LOCAL, "owners", {}).items():
        if archive == str(database_path(root)):
            if not Claims.fence(db, resource, owner):
                raise LeaseLost("Worker ownership expired; checkpoints retained for another worker")


@contextmanager
def lease(root, resource, *, ttl=90, reentrant=True):
    root = Path(root).resolve()
    from .config import database_path

    key = (str(database_path(root)), resource)
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
        from .storage import durable_writes

        with (
            lease(store.root, "archive"),
            lease(store.root, f"job:{job_id}"),
            durable_writes(store),
        ):
            attempt = store.begin_attempt(job_id, function.__name__, kwargs.get("name"))
            try:
                result = function(store, job, *args, **kwargs)
                stage = (
                    "artifact"
                    if function.__name__ == "build_one"
                    else "postprocessing"
                    if function.__name__ == "process_one"
                    else "diarization"
                )
                row = store.stage_row(
                    stage,
                    job_id,
                    workflow=kwargs.get("name", args[0] if args else None)
                    if stage == "artifact"
                    else None,
                )
                metrics = {
                    k: row[k]
                    for k in ("agent_seconds", "total_tokens", "figures", "processing_seconds")
                    if row and k in row.keys()
                }
                run_id = row.get("output_dir") if row else None
                if run_id and store.exists(run_id + "/report.json"):
                    import json

                    report = json.loads(store.read_key(run_id + "/report.json"))
                    metrics.update(
                        {
                            k: report[k]
                            for k in [
                                "usage",
                                "elapsed_seconds",
                                "agent_seconds",
                                "agent_elapsed_seconds",
                                "words",
                                "schema_version",
                                "prompt_sha256",
                            ]
                            if k in report
                        }
                    )
                metrics["publication_key"] = run_id
                store.finish_attempt(attempt, "complete", metrics=metrics, run_id=run_id)
                return result
            except BaseException as error:
                store.finish_attempt(
                    attempt,
                    "interrupted"
                    if isinstance(error, (KeyboardInterrupt, SystemExit))
                    else "failed",
                    error=str(error),
                )
                raise

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
