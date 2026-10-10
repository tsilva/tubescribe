"""One archive worker, bounded attempts, recoverable stage transitions."""

from __future__ import annotations

import json
import time

import httpx

from .asr import Client
from .diarization_tunnel import ready as ready_diarization
from .diarization_worker import bind_profile as bind_diarization_profile
from .diarization_worker import enrich_one
from .postprocessing_worker import PostprocessingUnavailable
from .storage import ProfileChanged, Store, now, worker_lock
from .tunnel import tunnel
from .youtube import YouTubeError, download


def log(event, **data):
    print(json.dumps({"time": now(), "event": event, **data}, ensure_ascii=False), flush=True)


class InferenceUnavailable(RuntimeError):
    pass


def ensure_ready(client: Client, store: Store, endpoint: str):
    try:
        return client.health()
    except Exception as original:
        config_path = store.root / "tunnel.json"
        if not config_path.exists():
            raise InferenceUnavailable(
                f"ASR unavailable: {original}; start the service/tunnel and resume"
            ) from original
        config = json.loads(config_path.read_text())
        if config.get("endpoint") != endpoint:
            raise InferenceUnavailable(
                f"ASR unavailable at the explicitly selected endpoint: {original}"
            ) from original
        log("reconnect_start", endpoint=endpoint, reason=str(original))
        try:
            connection = tunnel(
                store.root, "start", config["host"], config["local_port"], config["remote_port"]
            )
            log("reconnect_complete", endpoint=endpoint)
            return connection["health"]
        except Exception as error:
            raise InferenceUnavailable(
                f"ASR reconnect failed: {error}; authorize SSH if prompted, then restart the backfill"
            ) from error


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
    postprocessing="video",
    bridge=None,
    video_cache=None,
    summary=True,
    asr_client=None,
    diarization_factory=None,
):
    if postprocessing not in {"video", "text", "off"}:
        raise ValueError("Postprocessing must be video, text, or off")
    summary = summary and postprocessing != "off"
    done = failed = 0
    with worker_lock(store.root):
        store.recover()
        from .pipeline import Plan

        plan = Plan(True, postprocessing != "off", summary)
        scope = {"profile": profile, "ids": ids, "channel": channel}
        skipped = store.completed(plan, **scope)
        log("skip_completed", count=skipped)
        from .inference import client_options

        client = asr_client or Client(endpoint, **client_options())
        health = None
        while True:
            jobs = store.jobs(
                ids=ids,
                channel=channel,
                profile=profile,
                max_attempts=max_attempts,
                require_diarization=True,
                require_postprocessing=postprocessing != "off",
                require_summary=summary,
            )
            if not jobs:
                if wait:
                    due = store.next_due(plan, max_attempts=max_attempts, **scope)
                    if due is not None:
                        time.sleep(min(30, max(1, due - time.time())))
                        continue
                break
            if health is None and any(row["asr_state"] != "complete" for row in jobs):
                health = ensure_ready(client, store, endpoint)
                store.bind_profile(profile, health)
                log("asr_ready", **health)
            for row in jobs:
                if limit is not None and done + failed >= limit:
                    return {"completed": done, "failed": failed, "skipped": skipped}
                from .execution import lease

                with lease(store.root, f"job:{row['id']}"):
                    job = dict(row)
                    stage = "diarization" if job["asr_state"] == "complete" else "download"

                    def finish():
                        from .pipeline import finish as finish_pipeline

                        def entered(name):
                            nonlocal stage
                            stage = name

                        return any(
                            result.changed
                            for result in finish_pipeline(
                                store,
                                job,
                                bridge=bridge,
                                mode=postprocessing,
                                video_cache=video_cache,
                                summary=summary,
                                max_attempts=max_attempts,
                                on_stage=entered,
                            )
                        )

                    try:
                        if postprocessing != "off" and job.get("diarization_state") == "complete":
                            done += int(finish())
                            continue
                        try:
                            diarization_client = (diarization_factory or ready_diarization)(
                                store.root
                            )
                            bind_diarization_profile(store, profile, diarization_client.health())
                        except Exception as error:
                            raise InferenceUnavailable(
                                f"Diarization unavailable: {error}"
                            ) from error
                        if job["asr_state"] == "complete":
                            stage = "diarization"
                            completed = enrich_one(
                                store, job, diarization_client, keep_audio=keep_audio
                            )
                            if postprocessing != "off":
                                completed = finish() or completed
                            done += int(completed)
                            continue
                        store.bind_profile(profile, ensure_ready(client, store, endpoint))
                        path = store.cached_media(job["audio_path"])
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
                            store.update_video(job, metadata)
                            job["audio_path"] = store.persist_media(path)
                            store.update(
                                job["id"],
                                audio_path=job["audio_path"],
                                download_state="complete",
                                download_attempts=0,
                            )
                            log(
                                "download_complete",
                                video_id=job["video_id"],
                                bytes=path.stat().st_size,
                            )
                        stage = "asr"
                        store.bind_profile(profile, ensure_ready(client, store, endpoint))
                        store.update(
                            job["id"],
                            state="active",
                            asr_state="active",
                            asr_attempts=job["asr_attempts"] + 1,
                        )
                        log("asr_start", video_id=job["video_id"])
                        attempt = store.begin_attempt(job["id"], "asr")
                        try:
                            result = client.transcribe(path)
                        except BaseException as error:
                            store.finish_attempt(
                                attempt,
                                "interrupted"
                                if isinstance(error, (KeyboardInterrupt, SystemExit))
                                else "failed",
                                error=str(error),
                            )
                            raise
                        store.finish_attempt(
                            attempt,
                            "complete",
                            metrics={
                                "processing_seconds": result["processing_seconds"],
                                "duration": result["duration"],
                            },
                        )
                        # Recheck each result: a remote deployment may change mid-import.
                        store.bind_profile(profile, result)
                        expected = job.get("duration")
                        if expected and abs(result["duration"] - expected) > max(
                            5, expected * 0.01
                        ):
                            raise ValueError(
                                "Decoded audio duration differs from video metadata; audio retained for inspection"
                            )
                        stage = "save"
                        store.save(job, result, keep_audio=True)
                        stage = "diarization"
                        enrich_one(store, job, diarization_client, keep_audio=keep_audio)
                        if postprocessing != "off":
                            finish()
                        done += 1
                        log(
                            "complete",
                            video_id=job["video_id"],
                            duration=result["duration"],
                            processing_seconds=result["processing_seconds"],
                            speed=result["duration"] / result["processing_seconds"],
                        )
                    except (KeyboardInterrupt, SystemExit):
                        if stage in {"diarization", "postprocessing", "summary"}:
                            raise
                        store.update(
                            job["id"],
                            state="pending",
                            **{
                                (
                                    "download_state" if stage == "download" else "asr_state"
                                ): "pending"
                            },
                            **{
                                (
                                    "download_attempts" if stage == "download" else "asr_attempts"
                                ): job[
                                    "download_attempts" if stage == "download" else "asr_attempts"
                                ]
                            },
                        )
                        raise
                    except Exception as e:
                        kind = e.kind if isinstance(e, YouTubeError) else stage
                        attempts = (
                            job["download_attempts" if stage == "download" else "asr_attempts"] + 1
                        )
                        delay = (
                            1800 if kind == "rate_limit" else min(1800, 60 * 2 ** (attempts - 1))
                        )
                        if stage in {"diarization", "postprocessing", "summary"}:
                            failed += 1
                            log("failed", video_id=job["video_id"], stage=stage, error=str(e))
                            if isinstance(
                                e, (InferenceUnavailable, PostprocessingUnavailable)
                            ) or isinstance(
                                e, (httpx.TransportError, httpx.HTTPStatusError, ProfileChanged)
                            ):
                                log(
                                    "queue_paused",
                                    reason=f"{stage} paused: {e}; prior checkpoints retained",
                                )
                                return {"completed": done, "failed": failed, "skipped": skipped}
                            # A failure before stage bookkeeping must not spin on the same job.
                            if any(
                                r["id"] == job["id"]
                                for r in store.jobs(
                                    **scope,
                                    max_attempts=max_attempts,
                                    require_diarization=True,
                                    require_postprocessing=postprocessing != "off",
                                    require_summary=summary,
                                )
                            ):
                                log(
                                    "queue_paused",
                                    reason="Stage failed before a retry checkpoint was recorded: "
                                    + str(e),
                                )
                                return {"completed": done, "failed": failed, "skipped": skipped}
                            if kind == "rate_limit":
                                log(
                                    "backoff",
                                    reason=f"YouTube rate limit during {stage}; pausing the queue",
                                )
                                if not wait:
                                    return {"completed": done, "failed": failed, "skipped": skipped}
                                for _ in range(delay // 30):
                                    time.sleep(30)
                                break
                            continue
                        store.update(
                            job["id"],
                            state="failed",
                            failed_stage=stage,
                            error_kind=kind,
                            error=str(e),
                            next_retry=time.time() + delay,
                            **{
                                ("download_state" if stage == "download" else "asr_state"): "failed"
                            },
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
                        if isinstance(e, ProfileChanged):
                            log("queue_paused", reason=str(e))
                            return {"completed": done, "failed": failed, "skipped": skipped}
                        if isinstance(e, InferenceUnavailable):
                            log(
                                "queue_paused",
                                reason="ASR unavailable; audio retained, no further downloads",
                            )
                            return {"completed": done, "failed": failed, "skipped": skipped}
                        if kind == "rate_limit":
                            log(
                                "backoff",
                                reason="YouTube rate limit or bot check; pausing the queue",
                            )
                            if not wait:
                                return {"completed": done, "failed": failed, "skipped": skipped}
                            for _ in range(delay // 30):
                                time.sleep(30)
                            break
    log("worker_finished", completed=done, failed=failed, skipped=skipped)
    return {"completed": done, "failed": failed, "skipped": skipped}
