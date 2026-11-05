"""Read-only per-video progress and recorded usage, without fetching artifacts."""

import json
import math
import sqlite3
from contextlib import closing

from .config import OPENROUTER_PROFILES
from .database import connect


def number(value):
    return value if type(value) in (int, float) and math.isfinite(value) and value >= 0 else None


def metrics(value):
    try:
        result = json.loads(value or "{}")
        return result if isinstance(result, dict) else {}
    except (ValueError, TypeError):
        return {}


def usage_totals(value):
    usage = value.get("usage")
    if not isinstance(usage, dict):
        return {}
    total = usage.get("total", usage)
    return total if isinstance(total, dict) else {}


def recorded_cost(value):
    for source in (value, usage_totals(value)):
        if (cost := number(source.get("cost"))) is not None:
            return cost
    return None


def billing_totals(values):
    requests, legacy = {}, []
    for value in values:
        charges = value.get("charges")
        if not isinstance(charges, list):
            legacy.append(value)
            continue
        for item in charges:
            if isinstance(item, dict) and item.get("state") == "rejected":
                legacy.append({"cost": 0, "total_tokens": 0})
                continue
            if not isinstance(item, dict) or not isinstance(item.get("request_key"), str):
                legacy.append({})
                continue
            previous = requests.get(item["request_key"])
            if (previous is None or number(previous.get("cost_usd")) is None
                    or (item.get("provider") == "apify" and
                        item.get("observed_at", 0) > previous.get("observed_at", 0))):
                requests[item["request_key"]] = item
    generations = {}
    for item in requests.values():
        identity = (
            item.get("key")
            if isinstance(item.get("key"), str) and item["key"]
            else item["request_key"]
        )
        previous = generations.get(identity)
        if previous is None or number(previous.get("cost_usd")) is None:
            generations[identity] = item

    def generation_id(key, item):
        value = item.get("generation_id")
        return value if isinstance(value, str) and value else key.removeprefix("openrouter:")

    known_ids = {
        generation_id(key, item)
        for key, item in generations.items()
        if number(item.get("cost_usd")) is not None
    }
    costs, tokens, covered_cost, covered_tokens = [], [], set(), set()
    for value in legacy:
        identifiers = value.get("generation_ids")
        identifiers = (
            set(identifiers)
            if isinstance(identifiers, list)
            and identifiers
            and all(isinstance(i, str) and i for i in identifiers)
            else set()
        )
        if identifiers and identifiers <= known_ids:
            continue  # Individual receipts provide the complete aggregate.
        cost = recorded_cost(value)
        count = (
            number(value.get("total_tokens"))
            if value.get("total_tokens") is not None
            else number(usage_totals(value).get("totalTokens"))
        )
        if not identifiers or not identifiers <= covered_cost:
            costs.append(None if identifiers & covered_cost else cost)
        if cost is not None and not identifiers & covered_cost:
            covered_cost.update(identifiers)
        if not identifiers or not identifiers <= covered_tokens:
            tokens.append(count)
        if count is not None:
            covered_tokens.update(identifiers)
    for key, item in generations.items():
        identifier = generation_id(key, item)
        if identifier not in covered_cost:
            costs.append(number(item.get("cost_usd")))
        if identifier not in covered_tokens:
            tokens.append(number(item.get("total_tokens")))
    return costs, tokens


def details(root, video_id, profile=None):
    """Select one profile and its attempts in a consistent database snapshot."""
    with closing(connect(root, readonly=True)) as db:
        db.row_factory = sqlite3.Row
        db.execute("BEGIN")
        video = db.execute("SELECT duration FROM videos WHERE id=?", (video_id,)).fetchone()
        if video is None:
            raise FileNotFoundError(video_id)
        job = db.execute(
            "SELECT * FROM jobs WHERE video_id=?"
            + (" AND profile=?" if profile else "")
            + " ORDER BY id DESC LIMIT 1",
            (video_id, profile) if profile else (video_id,),
        ).fetchone()
        if job is None and profile:
            raise FileNotFoundError(profile)
        job = dict(job) if job else {}
        job_id = job.get("id")
        rows = {}
        for key, table in (
            ("transcription", "transcripts"),
            ("diarization", "diarizations"),
            ("postprocessing", "postprocessings"),
            ("summary", "artifacts"),
        ):
            row = db.execute(
                f"SELECT * FROM {table} WHERE job_id=?"
                + (" AND workflow='summary'" if key == "summary" else ""),
                (job_id,),
            ).fetchone()
            rows[key] = dict(row) if row else {}
        attempts = [
            dict(row)
            for row in db.execute(
                "SELECT stage,workflow,run_id,state,started,finished,elapsed,metrics FROM attempts "
                "WHERE job_id=? ORDER BY started,id",
                (job_id,),
            )
        ]
        db.rollback()

    hosted = job.get("profile") in OPENROUTER_PROFILES
    definitions = (
        ("download", "Audio download", job.get("download_state"), "download"),
        ("transcription", "Transcription", job.get("asr_state"), "asr"),
        ("diarization", "Speaker attribution", rows["diarization"].get("state"), "enrich_one"),
        (
            "postprocessing",
            "Transcript enrichment",
            rows["postprocessing"].get("state"),
            "process_one",
        ),
        ("summary", "Summary", rows["summary"].get("state"), "build_one"),
    )
    steps = []
    pipeline_attempts = []
    for key, label, state, attempt_stage in definitions:
        history = [
            a
            for a in attempts
            if a["stage"] == attempt_stage
            and (
                key != "summary"
                or a["workflow"] == "summary"
                or (a["workflow"] is None and "/summary/" in (a["run_id"] or ""))
            )
        ]
        pipeline_attempts.extend(history)
        row = job if key in ("download", "transcription") else rows[key]
        integrated = hosted and key == "diarization"
        state = "included" if integrated else state or "pending"
        if (
            not integrated
            and state == "active"
            and history
            and history[-1]["state"] == "interrupted"
        ):
            state = "interrupted"
        if (
            key == "download"
            and job.get("failed_stage") == "download"
            and job.get("state") == "failed"
        ):
            state = "failed"
        if (
            key == "transcription"
            and job.get("failed_stage") == "asr"
            and job.get("state") == "failed"
        ):
            state = "failed"
        values, seen = [], {}
        for attempt in history:
            value = metrics(attempt["metrics"])
            # Reusing a saved publication records another attempt, not another bill.
            publication = value.get("publication_key")
            generations = value.get("generation_ids")
            billing_key = (
                ("publication", publication)
                if isinstance(publication, str) and publication
                else None
            )
            if (
                isinstance(generations, list)
                and generations
                and all(isinstance(g, str) and g for g in generations)
            ):
                billing_key = ("generations", tuple(sorted(generations)))
            if billing_key:
                if billing_key in seen:
                    previous = values[seen[billing_key]]

                    def score(item):
                        costs, _ = billing_totals([item])
                        return (
                            bool(costs) and all(c is not None for c in costs),
                            sum(c is not None for c in costs),
                        )

                    if score(value) > score(previous):
                        values[seen[billing_key]] = value
                    continue
                seen[billing_key] = len(values)
            values.append(value)
        costs, tokens = billing_totals(values)
        # Hosted media retrieval has a provider receipt; legacy local download
        # attempts remain unbilled. A missing hosted receipt is never zero.
        hosted_download = key == "download" and any(v.get("hosted_media") for v in values)
        not_billable = (key == "download" and not hosted_download) or integrated
        if not_billable:
            costs = [0]
        recorded = [cost for cost in costs if cost is not None]
        elapsed = [number(a["elapsed"]) for a in history]
        fallback = (
            rows["transcription"].get("processing_seconds")
            if key == "transcription"
            else (
                row.get("processing_seconds") if key == "diarization" else row.get("agent_seconds")
            )
        )
        steps.append(
            {
                "key": key,
                "label": label,
                "state": state,
                "note": "Included in hosted transcription" if integrated else None,
                "attempts": len(history) if history else None,
                "elapsed_seconds": sum(v for v in elapsed if v is not None)
                if any(v is not None for v in elapsed)
                else number(fallback),
                "cost_usd": sum(recorded) if recorded else None,
                "cost_complete": bool(costs) and all(cost is not None for cost in costs),
                "cost_source": "not_billable"
                if not_billable
                else "provider_reported"
                if costs and all(c is not None for c in costs)
                else "unreported",
                "total_tokens": sum(t for t in tokens if t is not None)
                if any(t is not None for t in tokens)
                else number(row.get("total_tokens")),
                "started_at": history[0]["started"] if history else None,
                "completed_at": row.get("completed_at")
                if state == "complete" and key != "download"
                else None,
                "retry_at": (number(row.get("next_retry")) or None)
                if state in ("pending", "failed", "interrupted")
                else None,
            }
        )
    known_costs = [
        s["cost_usd"]
        for s in steps
        if s["cost_usd"] is not None and s["cost_source"] != "not_billable"
    ]
    started = [a["started"] for a in pipeline_attempts]
    finished = [
        a["finished"]
        for a in pipeline_attempts
        if a["stage"] == "build_one" and a["finished"] is not None
    ]
    complete = bool(job) and all(s["state"] in ("complete", "included") for s in steps)
    return {
        "video_id": video_id,
        "profile": job.get("profile"),
        "complete": complete,
        "cost_usd": sum(known_costs) if known_costs else None,
        # Local compute and subscription usage have no recorded dollar amount.
        "cost_complete": complete
        and all(s["cost_complete"] for s in steps if s["state"] != "included"),
        "cost_note": "Recorded charges only. Unreported provider charges, subscriptions, local compute and storage are not included.",
        "duration_seconds": number(video[0]) or number(rows["transcription"].get("duration")),
        "processing_seconds": sum(s["elapsed_seconds"] or 0 for s in steps)
        if any(s["elapsed_seconds"] is not None for s in steps)
        else None,
        "started_at": min(started) if started else None,
        "completed_at": (
            rows["summary"].get("completed_at") or (max(finished) if finished else None)
        )
        if complete
        else None,
        "model": rows["transcription"].get("model"),
        "language": rows["transcription"].get("language"),
        "speakers": number(rows["diarization"].get("speaker_count")),
        "figures": number(rows["postprocessing"].get("figures")),
        "total_tokens": sum(s["total_tokens"] or 0 for s in steps)
        if any(s["total_tokens"] is not None for s in steps)
        else None,
        "steps": steps,
    }
