"""One archive worker, bounded attempts, recoverable stage transitions."""

from __future__ import annotations

import json
import time

from .asr import Client
from .storage import Store, now, worker_lock
from .youtube import YouTubeError, download


def log(event, **data):
    print(json.dumps({"time": now(), "event": event, **data}, ensure_ascii=False), flush=True)


def work(
    store: Store,
    endpoint: str,
    *,
    ids=None,
    channel=None,
    profile="turbo-v1",
    limit=None,
    max_attempts=3,
    wait=False,
    keep_audio=False,
):
    done = failed = 0
    with worker_lock(store.root):
        store.recover()
        scope = "profile=? AND state='complete'"
        values = [profile]
        if ids:
            scope += f" AND video_id IN ({','.join('?' for _ in ids)})"
            values.extend(ids)
            found = store.db.execute(
                f"SELECT COUNT(DISTINCT video_id) FROM jobs WHERE profile=? AND video_id IN ({','.join('?' for _ in ids)})",
                [profile, *ids],
            ).fetchone()[0]
            if found != len(set(ids)):
                raise ValueError("One or more requested video IDs are not indexed for this profile")
        if channel:
            scope += " AND video_id IN (SELECT id FROM videos WHERE channel_id=?)"
            values.append(channel)
        skipped = store.db.execute("SELECT COUNT(*) FROM jobs WHERE " + scope, values).fetchone()[0]
        log("skip_completed", count=skipped)
        client = Client(endpoint)
        health = None
        while True:
            jobs = store.jobs(ids=ids, channel=channel, profile=profile, max_attempts=max_attempts)
            if not jobs:
                if wait:
                    # Wait only for retryable jobs in this worker's scope, never forever for exhausted jobs.
                    sql = "SELECT MIN(next_retry) FROM jobs WHERE profile=? AND state='failed' AND download_attempts<? AND asr_attempts<?"
                    args = [profile, max_attempts, max_attempts]
                    if ids:
                        sql += f" AND video_id IN ({','.join('?' for _ in ids)})"
                        args.extend(ids)
                    if channel:
                        sql += " AND video_id IN (SELECT id FROM videos WHERE channel_id=?)"
                        args.append(channel)
                    due = store.db.execute(sql, args).fetchone()[0]
                    if due is not None:
                        time.sleep(min(30, max(1, due - time.time())))
                        continue
                break
            if health is None:
                health = client.health()
                store.bind_profile(profile, health)
                log("asr_ready", **health)
            for row in jobs:
                if limit is not None and done + failed >= limit:
                    return {"completed": done, "failed": failed, "skipped": skipped}
                job = dict(row)
                stage = "download"
                try:
                    path = store.root / job["audio_path"] if job["audio_path"] else None
                    if not path or not path.is_file():
                        store.update(
                            job["id"],
                            state="active",
                            download_state="active",
                            download_attempts=job["download_attempts"] + 1,
                        )
                        log("download_start", video_id=job["video_id"], title=job["title"])
                        path, metadata = download(store.root, job["video_id"])
                        if metadata.get("channel_id") != job["channel_id"]:
                            raise YouTubeError(
                                "Downloaded video channel does not match indexed channel"
                            )
                        job["title"] = metadata.get("title") or job["title"]
                        job["duration"] = metadata.get("duration")
                        with store.db:
                            store.db.execute(
                                "UPDATE videos SET title=?,duration=?,published_at=? WHERE id=?",
                                (
                                    job["title"],
                                    metadata.get("duration"),
                                    str(
                                        metadata.get("timestamp")
                                        or metadata.get("upload_date")
                                        or ""
                                    ),
                                    job["video_id"],
                                ),
                            )
                        job["audio_path"] = str(path.relative_to(store.root))
                        store.update(
                            job["id"],
                            audio_path=job["audio_path"],
                            download_state="complete",
                            download_attempts=0,
                        )
                        log(
                            "download_complete", video_id=job["video_id"], bytes=path.stat().st_size
                        )
                    stage = "asr"
                    store.update(
                        job["id"],
                        state="active",
                        asr_state="active",
                        asr_attempts=job["asr_attempts"] + 1,
                    )
                    log("asr_start", video_id=job["video_id"])
                    result = client.transcribe(path)
                    # Recheck each result: a remote deployment may change mid-import.
                    store.bind_profile(profile, result)
                    expected = job.get("duration")
                    if expected and abs(result["duration"] - expected) > max(5, expected * 0.01):
                        raise ValueError(
                            "Decoded audio duration differs from video metadata; audio retained for inspection"
                        )
                    stage = "save"
                    store.save(job, result, keep_audio=keep_audio)
                    done += 1
                    log(
                        "complete",
                        video_id=job["video_id"],
                        duration=result["duration"],
                        processing_seconds=result["processing_seconds"],
                        speed=result["duration"] / result["processing_seconds"],
                    )
                except (KeyboardInterrupt, SystemExit):
                    store.update(
                        job["id"],
                        state="pending",
                        **{("download_state" if stage == "download" else "asr_state"): "pending"},
                    )
                    raise
                except Exception as e:
                    kind = e.kind if isinstance(e, YouTubeError) else stage
                    attempts = (
                        job["download_attempts" if stage == "download" else "asr_attempts"] + 1
                    )
                    delay = 1800 if kind == "rate_limit" else min(1800, 60 * 2 ** (attempts - 1))
                    store.update(
                        job["id"],
                        state="failed",
                        failed_stage=stage,
                        error_kind=kind,
                        error=str(e),
                        next_retry=time.time() + delay,
                        **{("download_state" if stage == "download" else "asr_state"): "failed"},
                    )
                    failed += 1
                    log(
                        "failed",
                        video_id=job["video_id"],
                        stage=stage,
                        kind=kind,
                        error=str(e),
                        retry_after_seconds=delay,
                    )
                    if kind == "rate_limit":
                        log("backoff", reason="YouTube rate limit or bot check; pausing the queue")
                        if not wait:
                            return {"completed": done, "failed": failed, "skipped": skipped}
                        for _ in range(delay // 30):
                            time.sleep(30)
                        break
    log("worker_finished", completed=done, failed=failed, skipped=skipped)
    return {"completed": done, "failed": failed, "skipped": skipped}
