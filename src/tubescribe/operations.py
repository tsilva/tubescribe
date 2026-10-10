"""Authenticated readiness and low-cardinality metrics from the durable ledger."""

from contextlib import closing

from .blobs import DirectoryObjects, LocalArtifacts
from .config import RuntimeConfig
from .database import clock, connect
from .migrations import check_schema


def readiness(root):
    checks = {"database": False, "schema": False, "objects": False}
    try:
        with closing(connect(root, readonly=True)) as db:
            db.execute("SELECT 1").fetchone()
            checks["database"] = True
            check_schema(db)
            checks["schema"] = True
    except Exception:
        pass
    try:
        config = RuntimeConfig.load(root)
        if config.s3_bucket:
            import boto3
            from botocore.config import Config

            from .s3 import S3Artifacts

            client = boto3.client(
                "s3",
                endpoint_url=config.s3_endpoint,
                config=Config(
                    connect_timeout=1,
                    read_timeout=2,
                    retries={"max_attempts": 0},
                    s3={"addressing_style": "path"},
                ),
            )
            objects = S3Artifacts(config.s3_bucket, client=client)
        else:
            objects = DirectoryObjects(config.objects) if config.objects else LocalArtifacts(root)
        objects.probe()
        checks["objects"] = True
    except Exception:
        pass
    return {"ready": all(checks.values()), "checks": checks}


def metrics(root):
    with closing(connect(root, readonly=True)) as db:
        check_schema(db)
        stamp = clock(db)
        queue = db.execute(
            "SELECT state,COUNT(*),MIN(created) FROM submissions GROUP BY state"
        ).fetchall()
        attempts = db.execute(
            "SELECT stage,state,COUNT(*),COALESCE(SUM(elapsed),0) FROM attempts GROUP BY stage,state"
        ).fetchall()
        expired = db.execute("SELECT COUNT(*) FROM claims WHERE expires<=?", (stamp,)).fetchone()[0]
        active = db.execute("SELECT COUNT(*) FROM claims WHERE expires>?", (stamp,)).fetchone()[0]
        if getattr(db, "dialect", None) == "postgres":
            publication = "COALESCE(metrics::jsonb->>'publication_key',id)"
            tokens = "COALESCE((metrics::jsonb->>'total_tokens')::double precision,(metrics::jsonb->'usage'->'total'->>'totalTokens')::double precision,0)"
            inference = "COALESCE((metrics::jsonb->>'agent_seconds')::double precision,(metrics::jsonb->>'processing_seconds')::double precision,0)"
        else:
            publication = "COALESCE(json_extract(metrics,'$.publication_key'),id)"
            tokens = "COALESCE(json_extract(metrics,'$.total_tokens'),json_extract(metrics,'$.usage.total.totalTokens'),0)"
            inference = "COALESCE(json_extract(metrics,'$.agent_seconds'),json_extract(metrics,'$.processing_seconds'),0)"
        usage = db.execute(
            f"SELECT stage,SUM(tokens),SUM(seconds) FROM (SELECT stage,{publication} AS publication,MAX({tokens}) AS tokens,MAX({inference}) AS seconds FROM attempts WHERE state='complete' GROUP BY stage,{publication}) AS publications GROUP BY stage"
        ).fetchall()
    result = {
        "queue": {},
        "oldest_queued_seconds": 0,
        "expired_leases": expired,
        "active_leases": active,
        "attempts": [],
        "usage": [],
    }
    for state, count, created in queue:
        state = (
            state if state in {"queued", "active", "complete", "failed", "interrupted"} else "other"
        )
        result["queue"][state] = result["queue"].get(state, 0) + count
        if state == "queued":
            result["oldest_queued_seconds"] = max(0, stamp - created)
    known = {
        "asr",
        "diarization",
        "postprocessing",
        "artifact",
        "enrich_one",
        "process_one",
        "build_one",
    }
    for stage, state, count, elapsed in attempts:
        result["attempts"].append(
            {
                "stage": stage if stage in known else "other",
                "state": state
                if state in {"active", "complete", "failed", "interrupted"}
                else "other",
                "count": count,
                "seconds": elapsed,
            }
        )
    for stage, tokens, seconds in usage:
        result["usage"].append(
            {
                "stage": stage if stage in known else "other",
                "tokens": tokens,
                "inference_seconds": seconds,
            }
        )
    return result


def prometheus(data):
    samples = {}

    def add(name, value, labels=""):
        key = name + labels
        samples[key] = samples.get(key, 0) + value

    for state, count in data["queue"].items():
        add("tubescribe_submissions", count, f'{{state="{state}"}}')
    add("tubescribe_oldest_queued_seconds", data["oldest_queued_seconds"])
    add("tubescribe_expired_leases", data["expired_leases"])
    add("tubescribe_active_leases", data["active_leases"])
    for attempt in data["attempts"]:
        labels = f'{{stage="{attempt["stage"]}",state="{attempt["state"]}"}}'
        add("tubescribe_attempts", attempt["count"], labels)
        add("tubescribe_attempt_seconds", attempt["seconds"], labels)
    for usage in data["usage"]:
        labels = f'{{stage="{usage["stage"]}"}}'
        add("tubescribe_tokens", usage["tokens"], labels)
        add("tubescribe_inference_seconds", usage["inference_seconds"], labels)
    lines = []
    names = sorted({key.split("{")[0] for key in samples})
    for name in names:
        lines.append(f"# TYPE {name} gauge")
        lines.extend(
            f"{key} {value}" for key, value in sorted(samples.items()) if key.split("{")[0] == name
        )
    return "\n".join(lines) + "\n"
