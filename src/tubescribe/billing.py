"""Persist provider charges at receipt time, including failed/cached stage runs."""

import json
import math
import uuid
from contextlib import contextmanager
from contextvars import ContextVar

_ACTIVE = ContextVar("billing_attempt", default=None)


def amount(value):
    return value if type(value) in (int, float) and math.isfinite(value) and value >= 0 else None


@contextmanager
def tracking(store, attempt):
    token = _ACTIVE.set((store, attempt))
    try:
        yield
    finally:
        _ACTIVE.reset(token)


def charge(key, response=None, *, generation_id=None, rejected=False):
    response = response or {}
    usage = response.get("usage") or {}
    if not isinstance(usage, dict):
        usage = {}
    identifier = generation_id or response.get("id")
    return {
        "key": "openrouter:" + identifier if isinstance(identifier, str) and identifier else key,
        "request_key": key,
        "provider": "openrouter",
        "generation_id": identifier if isinstance(identifier, str) else None,
        "model": response.get("model"),
        "cost_usd": 0 if rejected else amount(usage.get("cost")),
        "total_tokens": amount(usage.get("total_tokens")),
        "state": "rejected"
        if rejected
        else "reported"
        if amount(usage.get("cost")) is not None
        else "unreported",
    }


def record(value):
    active = _ACTIVE.get()
    if not active:
        return
    store, identifier = active
    with store.db:
        row = store.db.execute("SELECT metrics FROM attempts WHERE id=?", (identifier,)).fetchone()
        if not row:
            return
        metrics = json.loads(row[0] or "{}")
        charges = metrics.get("charges", [])
        # Replace the intent entry when its generation ID becomes known.
        charges = [c for c in charges if c["request_key"] != value["request_key"]]
        charges.append(value)
        metrics["charges"] = charges
        store.db.execute(
            "UPDATE attempts SET metrics=? WHERE id=?", (json.dumps(metrics), identifier)
        )
        # A cost can arrive between stage transitions. Invalidate the small live
        # revision marker without changing the video or restarting any work.
        store.db.execute(
            "UPDATE catalogue_revisions SET revision=? WHERE scope='feed'", (uuid.uuid4().hex,)
        )


def result_billing(result):
    """Preserve all charge identities; never turn an unreported cost into zero."""
    charges = result.get("charges")
    if isinstance(charges, list):
        return {
            "charges": charges,
            "cost": result.get("cost"),
            "cost_complete": result.get("cost_complete", False),
        }
    usage = result.get("usage") or {}
    total = usage.get("total", usage) if isinstance(usage, dict) else {}
    cost = amount(result.get("cost"))
    if cost is None and isinstance(total, dict):
        cost = amount(total.get("cost"))
    return {"cost": cost, "cost_complete": cost is not None}
