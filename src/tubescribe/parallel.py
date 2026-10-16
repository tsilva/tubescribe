"""Bounded network video executions with independent connections and shared stage leases."""

import time
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from contextvars import copy_context

from .execution import pipeline_lock
from .pipeline import Plan
from .storage import Store


def work_one(root, endpoint, options):
    from .worker import work

    # Connections, upload caches and ownership are never shared across threads.
    store = Store(root)
    try:
        return work(store, endpoint, **options, _recover=False, _parallel=False)
    finally:
        store.db.close()


def work_parallel(store, endpoint, **options):
    from .worker import log

    scope = {k: options[k] for k in ("ids", "channel", "profile")}
    plan = Plan(True, options["postprocessing"] != "off", options["summary"])
    slots = store.config.agent_concurrency
    completed = failed = 0
    paused = None
    limit = options["limit"]
    active = {}
    with pipeline_lock(store.root):
        log("recovery_start")
        store.recover()
        log("recovery_complete")
        skipped = store.completed(plan, **scope)
        log("parallel_start", concurrency=slots, skipped=skipped)
        # On interruption, drain already submitted work before releasing the parent
        # lifetime/submission barrier. Durable callers retain their run journals.
        with ThreadPoolExecutor(max_workers=slots, thread_name_prefix="video") as pool:
            while True:
                allowance = slots - len(active)
                if limit is not None:
                    allowance = min(allowance, limit - completed - failed - len(active))
                if not paused and allowance > 0:
                    jobs = store.jobs(
                        **scope,
                        max_attempts=options["max_attempts"],
                        require_diarization=True,
                        require_postprocessing=plan.postprocessing,
                        require_summary=plan.summary,
                    )
                    busy = set(active.values())
                    for job in [j for j in jobs if j["video_id"] not in busy][:allowance]:
                        child = {**options, "ids": [job["video_id"]], "limit": 1, "wait": False}
                        # Carry submission/event context, but thread-local leases remain
                        # independent and are acquired by each existing stage handler.
                        future = pool.submit(
                            copy_context().run, work_one, store.root, endpoint, child
                        )
                        active[future] = job["video_id"]
                        log("parallel_video_start", video_id=job["video_id"], in_flight=len(active))
                if active:
                    ready, _ = wait(active, timeout=1, return_when=FIRST_COMPLETED)
                    for future in ready:
                        active.pop(future)
                        result = future.result()
                        completed += result["completed"]
                        failed += result["failed"]
                        paused = paused or result.get("paused")
                    continue
                if paused or (limit is not None and completed + failed >= limit):
                    break
                due = store.next_due(plan, max_attempts=options["max_attempts"], **scope)
                if not options["wait"] or due is None:
                    break
                time.sleep(min(30, max(1, due - time.time())))
    result = {"completed": completed, "failed": failed, "skipped": skipped}
    if paused:
        result["paused"] = paused
    log("worker_finished", **result)
    return result
