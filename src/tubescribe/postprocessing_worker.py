"""Resumable final archive stage; ASR/diarization content remains authoritative."""

import hashlib
import json
import os
import re
import time
import uuid
from pathlib import Path
from urllib.parse import urlsplit

import httpx

from . import postprocessing as engine
from .storage import atomic_write, now, worker_lock
from .youtube import YouTubeError, download_video

COUNTERS = {"download": "download_attempts", "process": "attempts", "save": "save_attempts"}


def retryable(row, max_attempts):
    return all(
        row[counter] < max_attempts
        for stage, counter in COUNTERS.items()
        if stage != "process" or row["stage"] != "save"
    )


class PostprocessingUnavailable(RuntimeError):
    pass


def log(event, **data):
    print(json.dumps({"time": now(), "event": event, **data}, ensure_ascii=False), flush=True)


def bridge_endpoint(root):
    if os.environ.get("TUBESCRIBE_AGENTBRIDGE"):
        return os.environ["TUBESCRIBE_AGENTBRIDGE"]
    path = root / "postprocessing.json"
    return (
        json.loads(path.read_text()).get("bridge", "http://127.0.0.1:8082")
        if path.exists()
        else "http://127.0.0.1:8082"
    )


def ready(bridge):
    parsed = urlsplit(bridge)
    if (
        parsed.scheme != "http"
        or parsed.hostname not in {"localhost", "127.0.0.1", "::1"}
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
        or parsed.path not in {"", "/"}
    ):
        raise PostprocessingUnavailable(
            "Use the loopback HTTP AgentBridge base URL; callbacks run on this machine"
        )
    try:
        response = httpx.get(bridge.rstrip("/") + "/openapi.json", timeout=10, trust_env=False)
        response.raise_for_status()
        if "/api/v1/agent/runs" not in response.json().get("paths", {}):
            raise ValueError("This AgentBridge does not expose /api/v1/agent/runs")
    except Exception as error:
        raise PostprocessingUnavailable(
            f"AgentBridge unavailable: {error}; start a build supporting native agent tools"
        ) from error


def update(store, job_id, **values):
    values["updated_at"] = now()
    with store.db:
        store.db.execute(
            f"UPDATE postprocessings SET {','.join(k + '=?' for k in values)} WHERE job_id=?",
            (*values.values(), job_id),
        )


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def archive_path(store, relative):
    path = (store.root / relative).resolve()
    if not path.is_relative_to(store.root / "transcripts"):
        raise ValueError("Postprocessing artifact is outside archive transcripts")
    return path


def cleanup_video(store, video_id):
    """Remove only archive-owned media after durable completion; failures are retryable."""
    directory = store.root / "videos"
    if not re.fullmatch(r"[A-Za-z0-9_-]{11}", video_id):
        return
    try:
        if not directory.exists():
            return
        if not directory.resolve().is_relative_to(store.root):
            raise ValueError("Video cache points outside the archive")
        removed = []
        for suffix in (".mp4", ".webm", ".mkv"):
            path = directory / (video_id + suffix)
            try:
                path.unlink()
                removed.append(path.name)
            except FileNotFoundError:
                pass
        if removed:
            fd = os.open(directory, os.O_RDONLY)
            try:
                os.fsync(fd)
            finally:
                os.close(fd)
            log("video_cleanup_complete", video_id=video_id, files=removed)
    except (OSError, ValueError) as error:
        # A cleanup failure must never invalidate a completed bundle or rerun inference.
        log("video_cleanup_failed", video_id=video_id, error=str(error))


def recover(store):
    for row in store.db.execute(
        "SELECT p.*,j.video_id FROM postprocessings p JOIN jobs j ON j.id=p.job_id WHERE p.state='complete'"
    ).fetchall():
        source_changed = False
        try:
            source = archive_path(store, row["source_path"])
            source_changed = (
                digest(source) != row["source_sha256"]
                or digest(source.with_suffix(".srt")) != row["srt_sha256"]
            )
            manifest = archive_path(store, row["manifest_path"])
            valid = not source_changed and digest(manifest) == row["manifest_sha256"]
            if valid:
                entries = json.loads(manifest.read_text())["files"]
                directory = archive_path(store, row["output_dir"])
                valid = all(
                    (directory / path).resolve().is_relative_to(directory)
                    and digest(directory / path) == checksum
                    for path, checksum in entries.items()
                )
            if valid:
                cleanup_video(store, row["video_id"])
                continue
        except (OSError, ValueError, KeyError, TypeError):
            pass
        update(
            store,
            row["job_id"],
            state="pending",
            stage="save",
            completed_at=None,
            download_attempts=0,
            attempts=0,
            save_attempts=0,
            next_retry=0,
            error="Output or source changed; repair queued",
            **({"output_dir": None} if source_changed else {}),
        )


def initialize(store, job, mode):
    source = store.db.execute("SELECT * FROM transcripts WHERE job_id=?", (job["id"],)).fetchone()
    diarization = store.db.execute(
        "SELECT state FROM diarizations WHERE job_id=?", (job["id"],)
    ).fetchone()
    if not source or not diarization or diarization[0] != "complete":
        raise ValueError("Postprocessing requires a completed diarized transcript")
    path = archive_path(store, source["json_path"])
    if digest(path) != source["json_sha256"]:
        raise ValueError("Authoritative transcript is corrupt; repair it before postprocessing")
    with store.db:
        store.db.execute(
            "INSERT OR IGNORE INTO postprocessings(job_id,mode,updated_at) VALUES(?,?,?)",
            (job["id"], mode, now()),
        )
    return dict(
        store.db.execute("SELECT * FROM postprocessings WHERE job_id=?", (job["id"],)).fetchone()
    ), path


def cached_video(store, job, cache):
    if cache and cache.startswith("ssh://"):
        source = cache.rstrip("/") + "/" + job["video_id"] + ".mp4"
        engine.validate_cached_video(source)
        return source
    directory = Path(cache).expanduser().resolve() if cache else store.root / "videos"
    for suffix in (".mp4", ".webm", ".mkv"):
        path = directory / (job["video_id"] + suffix)
        if path.is_file():
            engine.validate_cached_video(str(path))
            return str(path)
    if cache:
        raise ValueError(
            f"No cached video for {job['video_id']} in {directory}; custom caches are read-only"
        )
    path, metadata = download_video(store.root, job["video_id"])
    if metadata.get("channel_id") != job["channel_id"]:
        path.rename(path.with_name(f"{path.stem}.rejected-{uuid.uuid4().hex}{path.suffix}"))
        raise ValueError("Video download channel identity differs")
    engine.validate_cached_video(str(path))
    return str(path)


def process_one(store, job, *, bridge=None, mode="video", video_cache=None):
    if mode not in {"video", "text"}:
        raise ValueError("Postprocessing mode must be video or text")
    row, source = initialize(store, job, mode)
    if row["state"] == "complete":
        return False
    bridge = bridge or bridge_endpoint(store.root)
    stage = "save"
    checkpoint = False
    try:
        current = (digest(source), digest(source.with_suffix(".srt")))
        output = archive_path(store, row["output_dir"]) if row["output_dir"] else None
        if output and (row["source_sha256"], row["srt_sha256"], row["mode"]) != (*current, mode):
            output = None
        checkpoint = (
            output
            and (output / "response.json").exists()
            and (output / "run-context.json").exists()
        )
        video = None
        if not checkpoint:
            stage = "process"
            ready(bridge)
            if mode == "video":
                stage = "download"
                update(
                    store,
                    job["id"],
                    state="active",
                    stage=stage,
                    download_attempts=row["download_attempts"] + 1,
                    error=None,
                )
                video = cached_video(store, job, video_cache)
                update(store, job["id"], download_attempts=0)
            output = source.parent / "postprocessing" / uuid.uuid4().hex
        elif mode == "video":
            # Recovery uses persisted frames; no video access or service readiness needed.
            video = "checkpoint-video"
        stage = "save" if checkpoint else "process"
        update(
            store,
            job["id"],
            state="active",
            stage=stage,
            mode=mode,
            source_path=str(source.relative_to(store.root)),
            source_sha256=current[0],
            srt_sha256=current[1],
            output_dir=str(output.relative_to(store.root)),
            error=None,
            **(
                {"save_attempts": row["save_attempts"] + 1}
                if checkpoint
                else {"attempts": row["attempts"] + 1}
            ),
        )
        log("postprocessing_start", video_id=job["video_id"], resume=bool(checkpoint), mode=mode)

        def saving_response():
            nonlocal stage
            stage = "save"
            update(store, job["id"], stage=stage, save_attempts=row["save_attempts"] + 1)

        report = engine.process(
            bridge=bridge,
            transcript=source,
            output=output,
            video=video,
            resume=bool(checkpoint),
            on_response=saving_response,
        )
        stage = "save"
        update(store, job["id"], stage=stage, save_attempts=row["save_attempts"] + 1)
        # Fsync every checkpoint/output before publishing the SQLite completion pointer.
        files = {}
        for path in output.rglob("*"):
            if path.is_file() and path.name != "manifest.json" and not path.name.endswith(".tmp"):
                with path.open("rb") as handle:
                    os.fsync(handle.fileno())
                files[path.relative_to(output).as_posix()] = digest(path)
        manifest = output / "manifest.json"
        for directory in [p for p in output.rglob("*") if p.is_dir()] + [
            output,
            output.parent,
            source.parent,
        ]:
            fd = os.open(directory, os.O_RDONLY)
            try:
                os.fsync(fd)
            finally:
                os.close(fd)
        atomic_write(manifest, json.dumps({"version": 1, "files": files}, sort_keys=True).encode())
        update(
            store,
            job["id"],
            state="complete",
            stage="complete",
            completed_at=now(),
            next_retry=0,
            manifest_path=str(manifest.relative_to(store.root)),
            manifest_sha256=digest(manifest),
            error=None,
            figures=report["figures"],
            agent_seconds=report["agent_elapsed_seconds"],
            total_tokens=report["usage"]["total"]["totalTokens"],
        )
    except (KeyboardInterrupt, SystemExit):
        update(
            store,
            job["id"],
            state="pending",
            stage=stage,
            error="Interrupted; checkpoints retained",
            **{COUNTERS[stage]: row[COUNTERS[stage]]},
        )
        raise
    except (PostprocessingUnavailable, httpx.TransportError) as error:
        update(
            store,
            job["id"],
            state="pending",
            stage=stage,
            error=str(error),
            next_retry=time.time() + 60,
            **{COUNTERS[stage]: row[COUNTERS[stage]]},
        )
        raise
    except Exception as error:
        if stage == "save" and not checkpoint and isinstance(error, ValueError):
            # A rejected new response needs a fresh model attempt, rather than repackaging.
            stage = "process"
        if isinstance(error, httpx.HTTPStatusError) and error.response.status_code >= 500:
            update(
                store,
                job["id"],
                state="pending",
                stage=stage,
                error=str(error),
                next_retry=time.time() + 60,
                **{COUNTERS[stage]: row[COUNTERS[stage]]},
            )
            raise PostprocessingUnavailable(f"AgentBridge paused: {error}") from error
        attempts = row[COUNTERS[stage]] + 1
        delay = (
            1800
            if isinstance(error, YouTubeError) and error.kind == "rate_limit"
            else min(1800, 60 * 2 ** min(5, attempts - 1))
        )
        update(
            store,
            job["id"],
            state="failed",
            stage=stage,
            error=str(error),
            next_retry=time.time() + delay,
            **{COUNTERS[stage]: attempts},
            **(
                {"output_dir": None, "save_attempts": row["save_attempts"]}
                if stage == "process" and isinstance(error, ValueError)
                else {}
            ),
        )
        log("postprocessing_failed", video_id=job["video_id"], stage=stage, error=str(error))
        raise
    # Post-commit work sits outside failure handling, including interruption handling.
    log(
        "postprocessing_complete",
        video_id=job["video_id"],
        figures=report["figures"],
        agent_seconds=report["agent_elapsed_seconds"],
    )
    cleanup_video(store, job["video_id"])
    return True


def run(
    store,
    *,
    bridge=None,
    ids=None,
    channel=None,
    profile="turbo-v1",
    limit=None,
    max_attempts=3,
    wait=False,
    mode="video",
    video_cache=None,
    force=False,
):
    completed = failed = skipped = 0
    with worker_lock(store.root):
        store.recover()
        where = ["j.profile=?", "j.state='complete'", "d.state='complete'"]
        values = [profile]
        if ids:
            where.append(f"j.video_id IN ({','.join('?' for _ in ids)})")
            values.extend(ids)
        if channel:
            where.append("v.channel_id=?")
            values.append(channel)
        jobs = [
            dict(r)
            for r in store.db.execute(
                f"SELECT j.*,v.channel_id,v.title,v.url,v.kind FROM jobs j JOIN videos v ON v.id=j.video_id JOIN diarizations d ON d.job_id=j.id WHERE {' AND '.join(where)} ORDER BY COALESCE(v.published_at,'') DESC,j.id",
                values,
            )
        ]
        if ids and set(ids) != {r["video_id"] for r in jobs}:
            raise ValueError("Requested videos must have completed diarization in this profile")
        forced = set()
        for job in jobs:
            row, _ = initialize(store, job, mode)
            if not force and row["state"] == "complete":
                skipped += 1
        while True:
            due = None
            for job in jobs:
                if limit is not None and completed + failed >= limit:
                    return {"completed": completed, "failed": failed, "skipped": skipped}
                if force and job["id"] not in forced:
                    update(
                        store,
                        job["id"],
                        state="pending",
                        output_dir=None,
                        next_retry=0,
                        attempts=0,
                        save_attempts=0,
                        download_attempts=0,
                        completed_at=None,
                    )
                    forced.add(job["id"])
                row = dict(
                    store.db.execute(
                        "SELECT * FROM postprocessings WHERE job_id=?", (job["id"],)
                    ).fetchone()
                )
                if row["state"] == "complete" or not retryable(row, max_attempts):
                    continue
                if row["next_retry"] > time.time():
                    due = min(due or row["next_retry"], row["next_retry"])
                    continue
                try:
                    completed += int(
                        process_one(store, job, bridge=bridge, mode=mode, video_cache=video_cache)
                    )
                except Exception as error:
                    failed += 1
                    if isinstance(error, (PostprocessingUnavailable, httpx.TransportError)) or (
                        isinstance(error, YouTubeError) and error.kind == "rate_limit"
                    ):
                        return {
                            "completed": completed,
                            "failed": failed,
                            "skipped": skipped,
                            "paused": str(error),
                        }
                    row = store.db.execute(
                        "SELECT * FROM postprocessings WHERE job_id=?", (job["id"],)
                    ).fetchone()
                    if retryable(row, max_attempts):
                        due = min(due or row["next_retry"], row["next_retry"])
            if not wait or due is None:
                break
            time.sleep(min(30, max(1, due - time.time())))
    return {"completed": completed, "failed": failed, "skipped": skipped}
