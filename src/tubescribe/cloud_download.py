"""Durable Apify acquisition for one manually admitted video.

The journal is shared across Workflow retries and later manual retries. A lost
start acknowledgement is reconciled from INPUT; it never triggers a blind POST.
"""

import json
import time
import uuid
from datetime import UTC, datetime

from .agent_gateway import DurableRunPending
from .apify_client import ACTOR, MAX_COST, stored_file
from .billing import amount


def acquire(store, job, *, send=None, settle=False):
    if send is None:
        from .cloud_auth import workflow_send

        def send(request):
            return workflow_send(request, service="apify")

    key = f"transcripts/{job['channel_id']}/{job['video_id']}/{job['profile']}/cloud-media/apify.json"
    state = json.loads(store.read_key(key)) if store.exists(key) else None

    def save():
        store.blobs.put(key, json.dumps(state).encode())

    def call(operation, **arguments):
        result = send({"operation": operation, **arguments})
        status = result.get("status")
        if status == 429:
            raise DurableRunPending("Media provider is at capacity", retry_after=60)
        if status in {401, 402, 403}:
            raise ValueError("Media provider access or free allowance unavailable")
        if status != 200:
            raise DurableRunPending("Media provider acknowledgement unavailable", retry_after=30)
        return result

    if state is None or state.get("phase") == "ready":
        state = {"nonce": uuid.uuid4().hex, "started_at": datetime.now(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z"),
                 "created": time.time(), "phase": "starting"}
        save()
        # This call is never repeated, including if persisting its response fails.
        result = send({"operation": "start", "video_id": job["video_id"], "nonce": state["nonce"]})
        if result.get("status") == 429:
            state["phase"] = "ready"
            save()
            raise DurableRunPending("Media provider is at capacity", retry_after=60)
        if result.get("status") in {400, 401, 402, 403, 404, 422}:
            state["phase"] = "ready"  # Explicit rejection: no run was accepted.
            save()
            raise ValueError("Media provider rejected the request; check access or free allowance")
        if result.get("status") != 200:
            raise DurableRunPending("Media start acknowledgement unavailable", retry_after=30)
        state["run"] = result["run"]
        state["phase"] = "running"
        save()
        raise DurableRunPending("Media acquisition started", retry_after=20)
    if state.get("file"):
        if settle and not state.get("billing_settled"):
            run = call("status", run_id=state["run"]["id"])["run"]
            state["run"] = run
            save()
            finished = datetime.fromisoformat(run["finishedAt"].replace("Z", "+00:00")).timestamp()
            cost = amount(run.get("usageTotalUsd"))
            events = sum(amount(e.get("eventTotalUsd")) or 0
                         for e in (run.get("eventUsage") or {}).values())
            # Apify can return SUCCEEDED before its final charge is visible.
            # A delayed metadata GET does not repeat the download or model work.
            if time.time() - finished < 60 or cost is None or cost + 1e-9 < events:
                raise DurableRunPending("Waiting for media usage settlement", retry_after=30)
            state["billing_settled"] = True
            save()
        return state
    if not state.get("run"):
        result = call("recover", nonce=state["nonce"], started_at=state["started_at"])
        if result.get("ambiguous") or (not result.get("run") and time.time() - state["created"] > 300):
            raise ValueError("Media request outcome unknown; check billing before retry")
        if not result.get("run"):
            raise DurableRunPending("Reconciling media submission", retry_after=30)
        state["run"] = result["run"]
        save()
    run = call("status", run_id=state["run"]["id"])["run"]
    if run.get("actId") != ACTOR:
        raise ValueError("Unexpected media actor")
    state["run"] = run
    save()
    status = run["status"]
    if status in {"READY", "RUNNING", "TIMING-OUT", "ABORTING"}:
        if time.time() - state["created"] > 3600:
            raise ValueError("Media acquisition exceeded the recovery deadline")
        raise DurableRunPending("Media acquisition running", retry_after=20)
    if status in {"SUCCEEDED", "TIMED-OUT"}:
        result = call("output", run_id=run["id"], video_id=job["video_id"])
        if result.get("file"):
            state["file"] = stored_file(result["file"])
            state["phase"] = "complete"
            save()
            return state
    if (status in {"FAILED", "SUCCEEDED"} and amount(run.get("usageTotalUsd")) == 0
            and all(amount(event.get("eventTotalUsd")) == 0
                    for event in (run.get("eventUsage") or {}).values())):
        from .execution import CURRENT_SUBMISSION

        selected = CURRENT_SUBMISSION.get()
        selection = store.submission(selected) if selected else None
        # SUCCEEDED can still mean an empty output (for example, budget refusal).
        # Check for reusable output above before replacing a zero-cost run.
        # A fresh owner Retry after a confirmed zero-cost failure may create a
        # new run with repaired permissions. Same-submission retries never do.
        # Re-read the provider status above: a stale failed checkpoint is not
        # permission to duplicate a run an operator has since resumed.
        if selection and selection["created"] > state["created"]:
            history = key.removesuffix(".json") + "/" + state["nonce"] + ".json"
            store.blobs.put(history, json.dumps(state).encode())
            state = {"phase": "ready", "previous_run": run["id"], "created": time.time()}
            save()
            raise DurableRunPending("Manual retry admitted after a confirmed zero-cost failure")
    if status == "TIMED-OUT":
        count = (run.get("stats") or {}).get("resurrectCount", 0)
        # Persist each observed resume intent. If its acknowledgement is lost,
        # the client checks the original terminal marker before sending again.
        marker = run.get("finishedAt")
        if count < 2 and (amount(run.get("usageTotalUsd")) or 0) < MAX_COST:
            if state.get("resume_marker") != marker:
                state.update(resume_marker=marker, resume_count=count, resume_intent=time.time())
                save()
                state["run"] = call("resurrect", run_id=run["id"], count=count, finished_at=marker)["run"]
                save()
                raise DurableRunPending("Timed-out media run resumed", retry_after=20)
            if time.time() - state["resume_intent"] < 90:
                raise DurableRunPending("Reconciling media resume", retry_after=20)
            # Do not reissue a non-idempotent resume after an ambiguous outcome.
            raise ValueError("Media resume outcome unknown; check billing before retry")
    if status != "SUCCEEDED":
        raise ValueError("Media acquisition stopped; saved run and reported cost retained")
    raise ValueError("Media run returned no complete stored video")


def download_metrics(state):
    run = state.get("run") or {}
    if not run.get("id"):
        return {"hosted_media": True, "charges": []}
    cost = amount(run.get("usageTotalUsd"))
    return {"hosted_media": True, "charges": [{
        "key": "apify:" + run["id"], "request_key": "apify:" + state["nonce"],
        "provider": "apify", "generation_id": run["id"], "model": ACTOR,
        "cost_usd": cost, "total_tokens": 0,
        "state": "reported" if cost is not None else "unreported",
        "revision": (run.get("stats") or {}).get("resurrectCount", 0),
        "observed_at": time.time(),
    }]}


def acquisition(store, job, *, settle=False):
    key = f"transcripts/{job['channel_id']}/{job['video_id']}/{job['profile']}/cloud-media/apify.json"
    if settle and not store.exists(key):
        return None  # Existing non-Apify publications have no media bill to refresh.
    attempt = store.begin_attempt(job["id"], "download", workflow="settlement" if settle else None)
    state = "failed"
    try:
        result = acquire(store, job, settle=settle)
        state = "complete"
        return result
    except DurableRunPending:
        state = "pending"
        raise
    finally:
        value = json.loads(store.read_key(key)) if store.exists(key) else {}
        store.finish_attempt(attempt, state, metrics=download_metrics(value))
