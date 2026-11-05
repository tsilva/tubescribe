"""Durable selection is the outbox; missed dispatches are repaired by discovery."""

import asyncio
import json
import os
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from tempfile import TemporaryDirectory

from vercel import workflow

from .execution import ClaimBusy, lease
from .manual_queue import lane_filter
from .storage import Store
from .workflow_runtime import enabled, validate_runtime

_BOOTSTRAP_LOCK = threading.Lock()
_BOOTSTRAP_AT = 0


def bootstrap(*, request_token=None):
    """Bound owner-visit repair without polling or starting a job for every read."""
    global _BOOTSTRAP_AT
    if not enabled():
        return
    with _BOOTSTRAP_LOCK:
        if time.monotonic() < _BOOTSTRAP_AT:
            return
        if call("wake", request_token=request_token).get("notified"):
            _BOOTSTRAP_AT = time.monotonic() + 900


@contextmanager
def archive():
    with TemporaryDirectory(prefix="tubescribe-dispatch-") as directory:
        root = Path(directory)
        validate_runtime(root)
        store = Store(root)
        try:
            yield store
        finally:
            store.db.close()


async def running(store, key, *, current_deployment=False):
    if not store.exists(key):
        return False
    saved = json.loads(store.read_key(key))
    if current_deployment and saved.get("deployment_id") != os.environ.get("VERCEL_DEPLOYMENT_ID"):
        return False
    # Unknown availability is an error, never proof that a run can be duplicated.
    state = await workflow.Run(saved["run_id"]).status()
    return state in {"pending", "running"}


def registration(run):
    return {"run_id": run.run_id, "deployment_id": os.environ.get("VERCEL_DEPLOYMENT_ID")}


async def rotate_clock(expected_run_id):
    from .workflows import discovery_clock

    with archive() as store, lease(store.root, "cloud-discovery-dispatch"):
        key = "cloud/control/discovery.json"
        if store.exists(key):
            previous = json.loads(store.read_key(key))
            if previous["run_id"] != expected_run_id:
                return previous
        run = await workflow.start(discovery_clock)
        store.blobs.put(key, json.dumps(registration(run)).encode())
        return {"run_id": run.run_id}


async def maintain(*, clock=True):
    from .workflows import discovery_clock, process_selection

    if not enabled():
        return {"configured": False}
    started = 0
    with archive() as store:
        if clock:
            with lease(store.root, "cloud-discovery-dispatch"):
                key = "cloud/control/discovery.json"
                if not await running(store, key, current_deployment=True):
                    run = await workflow.start(discovery_clock)
                    store.blobs.put(key, json.dumps(registration(run)).encode())
        rows = store.db.execute(
            "SELECT id FROM submissions WHERE state IN ('queued','active') AND "
            + lane_filter(True) + " ORDER BY created LIMIT 10",
        ).fetchall()
        for row in rows:
            identifier = row[0]
            try:
                with lease(store.root, "cloud-dispatch:" + identifier):
                    key = "cloud/control/selections/" + identifier + ".json"
                    if await running(store, key):
                        continue
                    run = await workflow.start(process_selection, identifier)
                    store.blobs.put(key, json.dumps(registration(run)).encode())
                    started += 1
            except ClaimBusy:
                continue
    return {"configured": True, "notified": True, "started": started}


async def signal():
    """Admission requests only start coordination; they never wait for queue scans."""
    from .workflows import discovery_clock, dispatch_queue

    with archive() as store:
        for key, resource, operation in (
            ("cloud/control/discovery.json", "cloud-discovery-dispatch", discovery_clock),
            ("cloud/control/dispatcher.json", "cloud-queue-dispatch", dispatch_queue),
        ):
            with lease(store.root, resource):
                if not await running(store, key, current_deployment=operation is discovery_clock):
                    run = await workflow.start(operation)
                    store.blobs.put(key, json.dumps(registration(run)).encode())
    return {"configured": True, "notified": True}


def call(operation, *, request_token=None, **_):
    if not enabled():
        return {"configured": False}
    if operation == "status":
        return {"configured": True, "backend": "workflow", "media": "sandbox"}
    if operation != "wake":
        raise ValueError("Unknown Workflow operation")
    try:
        from vercel.headers import HeadersContext

        with HeadersContext({"x-vercel-oidc-token": request_token} if request_token else None).use():
            return asyncio.run(signal())
    except Exception:
        # Selection is committed already. Discovery and the daily repair cron
        # rescan this outbox even if dispatch acknowledgement was lost.
        return {"configured": True, "notified": False}
