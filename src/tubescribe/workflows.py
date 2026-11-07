"""Vercel Workflows coordinate small Functions and media-only Sandboxes."""

import asyncio

from vercel import workflow

wf = workflow.Workflows(sandbox_policy=workflow.SandboxPolicy(share_sandboxes=True))


async def execute(operation, *arguments):
    from .execution import ClaimBusy
    from .workflow_runtime import with_store

    try:
        return await asyncio.to_thread(with_store, operation, *arguments)
    except ClaimBusy:
        raise workflow.RetryableError("Selected resource is already owned", retry_after="90s") from None
    except Exception:
        # Workflow/queue SDKs log failed step exceptions. Do not propagate a
        # provider exception that could include request details or credentials.
        raise RuntimeError("Cloud step unavailable; durable checkpoints retained") from None


@wf.step(max_retries=5)
async def selected_step(identifier: str):
    return await execute("advance", identifier, workflow.get_workflow_metadata().run_id)


@wf.step(max_retries=5)
async def fail_selection(identifier: str):
    return await execute("fail", identifier, workflow.get_workflow_metadata().run_id)


@wf.workflow
async def process_selection(identifier: str):
    try:
        # Bound event-log growth. Completed work remains in Neon/R2 independently
        # of Workflow retention, so a later manual retry can safely pick it up.
        for _ in range(3000):
            result = await selected_step(identifier)
            if result["done"]:
                if result.get("state") == "complete":
                    await reconcile_queue()
                return result
            if result.get("delay"):
                await workflow.sleep(result["delay"])
        await fail_selection(identifier)
        return {"done": True, "state": "limit_reached"}
    except Exception:
        await fail_selection(identifier)
        raise


async def reconcile():
    from .workflow_dispatch import maintain

    try:
        result = await maintain(clock=False)
        if not result["configured"]:
            return {**result, "digest_check": "unconfigured"}
        digest = await execute("rollups_schedule")
        if digest["scheduled"]:
            result["started"] += (await maintain(clock=False))["started"]
        return {**result, "digest_check": digest["outcome"]}
    except Exception:
        raise RuntimeError("Cloud dispatch unavailable; selections retained") from None


@wf.step(max_retries=5)
async def reconcile_queue():
    return await reconcile()


@wf.step(max_retries=3)
async def discover_batch():
    owner = await execute("discovery_owner", workflow.get_workflow_metadata().run_id)
    if owner != "current":
        return {"owner": owner}
    channels = await execute("channels")
    semaphore = asyncio.Semaphore(4)

    async def check(channel):
        async with semaphore:
            try:
                return await execute("discover", channel)
            except Exception:
                return {"failed": 1}

    results = await asyncio.gather(*(check(channel) for channel in channels))
    queue = await reconcile()
    return {"owner": "current", "discovered": sum(r.get("discovered", 0) for r in results),
            "failed": sum(r.get("failed", 0) for r in results), "digest_check": queue["digest_check"]}


@wf.workflow
async def dispatch_queue():
    return await reconcile_queue()


@wf.step(max_retries=5)
async def continue_discovery():
    from .workflow_dispatch import rotate_clock

    try:
        return await rotate_clock(workflow.get_workflow_metadata().run_id)
    except Exception:
        raise RuntimeError("Discovery renewal unavailable; daily repair retained") from None


@wf.workflow
async def discovery_clock():
    # Rotate well before the per-run event limit. Sleeping uses no Sandbox and
    # leaves no Function running. The daily cron repairs a lost clock bootstrap.
    for _ in range(8):
        result = await discover_batch()
        if result["owner"] == "pending":
            await workflow.sleep("10s")
            continue
        if result["owner"] != "current":
            return {"state": "superseded"}
        await workflow.sleep("1h")
    if result["owner"] != "current":
        return {"state": "unregistered"}
    return await continue_discovery()
