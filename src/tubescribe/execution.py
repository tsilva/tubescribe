"""Durable, expiring ownership with heartbeats and fenced state commits."""

import threading
import time
import uuid
from contextlib import ExitStack, contextmanager
from contextvars import ContextVar
from functools import wraps
from pathlib import Path

from .repository import SQLClaims as Claims

CURRENT_SUBMISSION = ContextVar("submission", default=None)
_LOCAL = threading.local()


class ClaimBusy(RuntimeError):
    pass


class LeaseLost(RuntimeError):
    pass


def assert_owned(db, root):
    """The conditional write takes the DB write lock before any state mutation."""
    from .database import identity

    for (archive, resource), owner in getattr(_LOCAL, "owners", {}).items():
        if archive == identity(root):
            if not Claims.fence(db, resource, owner):
                raise LeaseLost("Worker ownership expired; checkpoints retained for another worker")


@contextmanager
def lease(root, resource, *, ttl=90, reentrant=True, wait=False):
    root = Path(root).resolve()
    from .database import identity

    key = (identity(root), resource)
    owners = getattr(_LOCAL, "owners", {})
    if key in owners:
        if not reentrant:
            raise ClaimBusy("Another worker owns this archive; nested execution is unavailable")
        yield owners[key]
        return
    claims = Claims(root)
    owner = uuid.uuid4().hex
    try:
        while not claims.acquire(resource, owner, ttl=ttl):
            if not wait:
                raise ClaimBusy(
                    "Another worker owns this archive/job; wait for its lease to finish or expire"
                )
            time.sleep(0.2)
    except BaseException:
        claims.close()
        raise
    _LOCAL.owners = {**owners, key: owner}
    stop = threading.Event()

    def heartbeat():
        from .database import database_errors

        connection = None
        try:
            connection = Claims(root)
            while not stop.wait(ttl / 3):
                if not connection.renew(resource, owner, ttl=ttl):
                    break
        except database_errors():
            # Ownership will expire; subsequent state commits still require fencing.
            pass
        finally:
            if connection:
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
        from .database import database_errors

        try:
            claims.release(resource, owner)
        except database_errors():
            pass
        finally:
            claims.close()


def job_stage(function):
    @wraps(function)
    def run(store, job, *args, **kwargs):
        job_id = job["id"] if isinstance(job, dict) else job
        from contextlib import nullcontext

        from .storage import durable_writes

        video_id = store.video_id(job_id)
        with (
            pipeline_lock(store.root) if store.config.network else lease(store.root, "archive"),
            lease(store.root, f"video:{video_id}", wait=store.config.network)
            if store.config.network
            else nullcontext(),
            lease(store.root, f"job:{job_id}", wait=store.config.network),
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
                from contextlib import suppress

                from .database import database_errors

                with suppress(*database_errors()):
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
def pipeline_lock(root):
    """Local mode retains its archive lock; network workers own separate lifetimes."""
    from .config import RuntimeConfig
    from .storage import worker_lock

    config = RuntimeConfig.load(root)
    if config.network:
        # Each network execution thread owns its own lifetime. Nested stages on the
        # same thread remain reentrant; sibling videos retain independent fencing.
        resource = f"worker:{config.worker_id}:{threading.get_ident()}"
        with lease(root, resource, wait=True):
            yield
    else:
        with worker_lock(root):
            yield


@contextmanager
def recovery_job(store, job_id):
    """Repair one idle job; never race a network worker's live publication."""
    if not store.config.network:
        yield True
        return
    with ExitStack() as stack:
        try:
            stack.enter_context(lease(store.root, f"video:{store.video_id(job_id)}"))
            stack.enter_context(lease(store.root, f"job:{job_id}"))
        except ClaimBusy:
            yield False
            return
        yield True


def defer_downloads(root, seconds=1800):
    """Share YouTube's cooldown across workers without consuming stage attempts."""
    from contextlib import closing

    from .config import RuntimeConfig

    if RuntimeConfig.load(root).network:
        with closing(Claims(root)) as claims:
            claims.defer_downloads(seconds)


@contextmanager
def capacity(root, kind):
    """Independent database leases bound downloads, GPU inference, and agent calls."""
    import sys

    from .config import RuntimeConfig

    config = RuntimeConfig.load(root)
    if not config.network:
        yield
        return
    slots = {"download": config.downloads, "gpu": config.gpu_slots, "agent": config.agent_slots}[
        kind
    ]
    cooldown = Claims(root) if kind == "download" else None
    try:
        while True:
            if cooldown and (delay := cooldown.download_delay()) > 0:
                time.sleep(min(30, max(1, delay)))
                continue
            for slot in range(slots):
                context = lease(root, f"capacity:{kind}:{slot}")
                try:
                    context.__enter__()
                except ClaimBusy:
                    continue
                try:
                    # A previous downloader can install a cooldown while this one
                    # waits for the slot. Recheck before any YouTube request starts.
                    if cooldown and cooldown.download_delay() > 0:
                        continue
                    yield
                    return
                finally:
                    context.__exit__(*sys.exc_info())
            time.sleep(0.2)
    finally:
        if cooldown:
            cooldown.close()


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
