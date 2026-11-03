"""Resumable final archive stage; ASR/diarization content remains authoritative."""

import hashlib
import json
import os
import re
import time
import uuid
from pathlib import Path

import httpx

from . import postprocessing as engine
from .agent_gateway import DurableRunFailed
from .config import OPENROUTER_PROFILE
from .database import database_errors, transient_errors
from .execution import capacity, job_stage, pipeline_lock
from .inference import PostprocessingUnavailable
from .inference import ready_bridge as ready
from .storage import now
from .youtube import YouTubeError, download_video

COUNTERS = {"download": "download_attempts", "process": "attempts", "save": "save_attempts"}


def retryable(row, max_attempts):
    from .pipeline import retryable as can_retry

    return row is None or can_retry(dict(row), max_attempts, download=True)


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


def update(store, job_id, **values):
    store.stage_update("postprocessing", job_id, **values)


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def archive_path(store, relative):
    path = (store.root / relative).resolve()
    if not path.is_relative_to(store.root / "transcripts"):
        raise ValueError("Postprocessing artifact is outside archive transcripts")
    if hasattr(store, "materialize_prefix"):
        if Path(relative).suffix:
            store.materialize(relative)
            if Path(relative).name == "transcript.json":
                store.materialize(Path(relative).with_suffix(".srt").as_posix())
        else:
            store.materialize_prefix(relative)
    else:
        from .storage import Store

        archive = Store(store.root)
        try:
            archive.materialize_prefix(relative)
        finally:
            archive.db.close()
    return path


def cleanup_video(store, video_id):
    """Remove only archive-owned media after durable completion; failures are retryable."""
    directory = store.root / "videos"
    if not re.fullmatch(r"[A-Za-z0-9_-]{11}", video_id):
        return
    try:
        removed = []
        for suffix in (".mp4", ".webm", ".mkv"):
            key = "videos/" + video_id + suffix
            if not store.local_blobs and store.exists(key):
                store.remove_media(key)
        if not directory.exists():
            return
        if not directory.resolve().is_relative_to(store.root):
            raise ValueError("Video cache points outside the archive")
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


def recover(store, *, job_ids=None):
    for row in store.stage_records("postprocessing", states=("complete",)):
        if job_ids is not None and row["job_id"] not in job_ids:
            continue
        from .execution import recovery_job

        with recovery_job(store, row["job_id"]) as idle:
            if not idle:
                continue
            if store.config.network:
                row = {**row, **store.stage_row("postprocessing", row["job_id"])}
                if row["state"] != "complete":
                    continue
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
            except (FileNotFoundError, ValueError, KeyError, TypeError):
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
    source = store.transcript(job["id"])
    if not source:
        raise ValueError("Postprocessing requires a completed transcript")
    if job.get("profile") != OPENROUTER_PROFILE:
        diarization = store.stage_row("diarization", job["id"])
        if not diarization or diarization["state"] != "complete":
            raise ValueError("Postprocessing requires a completed diarized transcript")
    path = archive_path(store, source["json_path"])
    if digest(path) != source["json_sha256"]:
        raise ValueError("Authoritative transcript is corrupt; repair it before postprocessing")
    return store.ensure_stage("postprocessing", job["id"], mode=mode), path


def cached_video(store, job, cache):
    if cache and cache.startswith("ssh://"):
        source = cache.rstrip("/") + "/" + job["video_id"] + ".mp4"
        engine.validate_cached_video(source)
        return source
    directory = Path(cache).expanduser().resolve() if cache else store.root / "videos"
    for suffix in (".mp4", ".webm", ".mkv"):
        path = directory / (job["video_id"] + suffix)
        if not cache and not store.local_blobs and store.exists("videos/" + path.name):
            path = store.materialize("videos/" + path.name)
        if path.is_file():
            engine.validate_cached_video(str(path))
            return str(path)
    if cache:
        raise ValueError(
            f"No cached video for {job['video_id']} in {directory}; custom caches are read-only"
        )
    with capacity(store.root, "download"):
        path, metadata = download_video(store.root, job["video_id"])
    if metadata.get("channel_id") != job["channel_id"]:
        path.rename(path.with_name(f"{path.stem}.rejected-{uuid.uuid4().hex}{path.suffix}"))
        raise ValueError("Video download channel identity differs")
    engine.validate_cached_video(str(path))
    return str(path)


@job_stage
def process_one(store, job, *, bridge=None, mode="video", video_cache=None, agent_runner=None):
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
        journal = output and (output / "agent-run.json").is_file()
        if not checkpoint:
            stage = "process"
            if agent_runner is None and not store.config.agent_gateway:
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
            if not journal:
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
                else {"attempts": row["attempts"] + (0 if journal else 1)}
            ),
        )
        log("postprocessing_start", video_id=job["video_id"], resume=bool(checkpoint), mode=mode)

        def saving_response():
            nonlocal stage
            stage = "save"
            update(store, job["id"], stage=stage, save_attempts=row["save_attempts"] + 1)

        if agent_runner is None:
            from .inference import configured_runner

            agent_runner = configured_runner(
                store, bridge, output.relative_to(store.root).as_posix()
            )
        remote_context = None
        if store.config.agent_gateway:
            from .postprocessing import budgets_for, load_source

            metadata = load_source(source, None, None, ())
            video_key = None
            if mode == "video":
                if video == "checkpoint-video":
                    video_key = None
                elif str(video).startswith("ssh://"):
                    raise ValueError(
                        "Durable gateway requires an archive video object; SSH video caches are local-only"
                    )
                else:
                    path = Path(video)
                    video_key = "videos/" + path.name
                    store.blobs.put_file(video_key, path)
            remote_context = {
                "kind": "postprocessing",
                "transcript_key": row["source_path"] or source.relative_to(store.root).as_posix(),
                "transcript_sha256": current[0],
                "srt_key": source.with_suffix(".srt").relative_to(store.root).as_posix(),
                "srt_sha256": current[1],
                "output_key": output.relative_to(store.root).as_posix(),
                "video_key": video_key,
                "video_sha256": digest(Path(video)) if video_key else None,
                "duration": metadata["duration"],
                "budgets": budgets_for(metadata["duration"], mode == "video"),
                "compact": True,
            }
        report = engine.process(
            bridge=bridge,
            transcript=source,
            output=output,
            video=video,
            resume=bool(checkpoint),
            on_response=saving_response,
            agent_runner=agent_runner,
            tool_context=remote_context,
        )
        stage = "save"
        update(store, job["id"], stage=stage, save_attempts=row["save_attempts"] + 1)
        output, publication = store.publish(output)
        manifest = output / "manifest.json"
        update(
            store,
            job["id"],
            state="complete",
            stage="complete",
            completed_at=now(),
            next_retry=0,
            output_dir=output.relative_to(store.root).as_posix(),
            manifest_path=str(manifest.relative_to(store.root)),
            manifest_sha256=digest(manifest),
            error=None,
            figures=report["figures"],
            agent_seconds=report["agent_elapsed_seconds"],
            total_tokens=report["usage"]["total"]["totalTokens"],
        )
    except transient_errors():
        from contextlib import suppress

        with suppress(*database_errors()):
            update(
                store,
                job["id"],
                state="pending",
                stage=stage,
                error="Durable service unavailable; checkpoints retained",
                next_retry=0,
                **{COUNTERS[stage]: row[COUNTERS[stage]]},
            )
        raise
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
                if stage == "process" and isinstance(error, (ValueError, DurableRunFailed))
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
    summary=True,
):
    from .artifacts import retryable as artifact_retryable
    from .artifacts import summary_row

    completed = failed = skipped = 0
    with pipeline_lock(store.root):
        store.recover()
        jobs = store.stage_inputs("diarization", profile=profile, ids=ids, channel=channel)
        if ids and set(ids) != {r["video_id"] for r in jobs}:
            raise ValueError("Requested videos must have completed diarization in this profile")
        forced = set()
        for job in jobs:
            row, _ = initialize(store, job, mode)
            artifact = summary_row(store, job["id"])
            if (
                not force
                and row["state"] == "complete"
                and (not summary or (artifact is not None and artifact["state"] == "complete"))
            ):
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
                row = store.stage_row("postprocessing", job["id"])
                if row["state"] != "complete" and not retryable(row, max_attempts):
                    continue
                if row["state"] != "complete" and row["next_retry"] > time.time():
                    due = min(due or row["next_retry"], row["next_retry"])
                    continue
                artifact = summary_row(store, job["id"])
                if row["state"] == "complete":
                    if not summary or not artifact_retryable(artifact, max_attempts):
                        continue
                    if artifact is not None and artifact["next_retry"] > time.time():
                        due = min(due or artifact["next_retry"], artifact["next_retry"])
                        continue
                stage = "postprocessing"
                try:
                    from .pipeline import finish as finish_pipeline

                    def entered(name):
                        nonlocal stage
                        stage = name

                    changed = any(
                        result.changed
                        for result in finish_pipeline(
                            store,
                            job,
                            bridge=bridge,
                            mode=mode,
                            video_cache=video_cache,
                            summary=summary,
                            max_attempts=max_attempts,
                            on_stage=entered,
                        )
                    )
                    completed += int(changed)
                except transient_errors():
                    raise
                except Exception as error:
                    failed += 1
                    if isinstance(
                        error,
                        (PostprocessingUnavailable, httpx.TransportError, httpx.HTTPStatusError),
                    ) or (isinstance(error, YouTubeError) and error.kind == "rate_limit"):
                        return {
                            "completed": completed,
                            "failed": failed,
                            "skipped": skipped,
                            "paused": str(error),
                        }
                    if stage == "summary":
                        artifact = summary_row(store, job["id"])
                        if artifact_retryable(artifact, max_attempts):
                            due = min(due or artifact["next_retry"], artifact["next_retry"])
                        continue
                    row = store.stage_row("postprocessing", job["id"])
                    if retryable(row, max_attempts):
                        due = min(due or row["next_retry"], row["next_retry"])
            if not wait or due is None:
                break
            time.sleep(min(30, max(1, due - time.time())))
    return {"completed": completed, "failed": failed, "skipped": skipped}
