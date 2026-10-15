"""Reconcile durable state and clean media independently of a successful worker."""

import json
import time
from pathlib import Path

from .storage import worker_lock


def reconcile(store, *, retention_days=7, purge=False):
    if retention_days < 1:
        raise ValueError("Retention must be at least one day")
    with worker_lock(store.root):
        if purge and store.pending_agent_runs():
            raise ValueError("Wait for queued/active gateway runs before purging bundles")
        queued = store.reclaim_submissions()
        interrupted = store.reconcile_attempts()
        store.recover()
        from .postprocessing_worker import cleanup_video

        for row in store.stage_records("postprocessing", states=("complete",)):
            cleanup_video(store, row["video_id"])
        referenced = {
            r["output_dir"]
            for stage in ("postprocessing", "artifact")
            for r in store.stage_records(stage)
            if r["output_dir"]
        }
        referenced.update(
            r[0]
            for r in store.db.execute(
                "SELECT output_dir FROM rolling_summaries WHERE output_dir IS NOT NULL"
            )
        )
        groups = set()
        for key in store.blobs.keys():
            parts = Path(key).parts
            if parts[0] == "transcripts" and "postprocessing" in parts:
                groups.add("/".join(parts[: parts.index("postprocessing") + 2]))
            elif len(parts) >= 5 and parts[:2] == ("artifacts", "rolling"):
                groups.add("/".join(parts[:4]))
            elif len(parts) >= 6 and parts[0] == "artifacts":
                groups.add("/".join(parts[:6]))
        orphaned = []
        threshold = time.time() - retention_days * 86400
        # Unreferenced bundles are reported by default; explicit purge uses a dated ledger.
        key = "maintenance/orphans.json"
        previous = json.loads(store.read_key(key)) if store.exists(key) else {}
        current = {g: previous.get(g, time.time()) for g in groups - referenced}
        for group, seen in current.items():
            removed = purge and seen < threshold
            orphaned.append({"key": group, "first_seen": seen, "deleted": removed})
            if removed:
                for name in store.blobs.keys(group + "/"):
                    store.blobs.delete(name)
        current = {g: t for g, t in current.items() if not (purge and t < threshold)}
        store.blobs.put(key, json.dumps(current).encode())
        result = {
            "requeued_submissions": queued,
            "interrupted_attempts": interrupted,
            "orphaned_bundles": orphaned,
            "purge": purge,
        }
        store.record_event(None, "reconciliation", result)
        return result
