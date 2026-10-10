"""Durable speaker enrichment of completed ASR, with retained input/checkpoints."""

import hashlib
import json
import time

import httpx

from .database import database_errors, transient_errors
from .diarization import Client, assign_speakers, validate
from .execution import capacity, job_stage, pipeline_lock
from .storage import ProfileChanged, atomic_write, now
from .youtube import YouTubeError, download

ATTEMPTS = {"download": "download_attempts", "diarization": "attempts", "save": "save_attempts"}


def log(event, **data):
    print(json.dumps({"time": now(), "event": event, **data}, ensure_ascii=False), flush=True)


def update(store, job_id, **values):
    store.stage_update("diarization", job_id, **values)


def read_checkpoint(store, row, field):
    path = store.materialize(row[field + "_path"])
    raw = store.read_key(row[field + "_path"])
    if hashlib.sha256(raw).hexdigest() != row[field + "_sha256"]:
        raise ValueError(f"Corrupt {field} checkpoint: {path}")
    return json.loads(raw)


def bind_profile(store, profile, health):
    store.bind_diarization_profile(profile, health)


def initialize(store, job):
    row = store.ensure_stage("diarization", job["id"])
    if not row["asr_path"]:
        transcript = store.transcript(job["id"])
        source = store.materialize(transcript["json_path"], transcript["json_sha256"])
        raw = source.read_bytes()
        if hashlib.sha256(raw).hexdigest() != transcript["json_sha256"]:
            raise ValueError(f"Saved ASR is corrupt: {source}")
        backup = source.with_name("transcript.asr.json")
        atomic_write(backup, raw)
        update(
            store,
            job["id"],
            asr_path=str(backup.relative_to(store.root)),
            asr_sha256=hashlib.sha256(raw).hexdigest(),
        )
        row = store.stage_row("diarization", job["id"])
    return row


def prepare(store, job):
    """Validate immutable ASR without making inference or download requests."""
    try:
        row = initialize(store, job)
        if row["state"] == "complete":
            return row, None
        asr = read_checkpoint(store, row, "asr")
    except Exception as error:
        existing = store.stage_row("diarization", job["id"])
        attempts = (existing["save_attempts"] if existing else 0) + 1
        update(
            store,
            job["id"],
            state="failed",
            stage="save",
            save_attempts=attempts,
            error=str(error),
            next_retry=time.time() + min(1800, 60 * 2 ** (attempts - 1)),
        )
        log("diarization_failed", video_id=job["video_id"], stage="save", error=str(error))
        raise
    return row, asr


@job_stage
def enrich_one(store, job, client, *, keep_audio=False):
    row, asr = prepare(store, job)
    if row["state"] == "complete":
        return False
    if not row["prediction_path"]:
        health = client.health()
        bind_profile(store, job["profile"], health)
    stage = "save" if row["prediction_path"] else "download"
    path = None
    try:
        prediction = None
        if row["prediction_path"]:
            from .inference import HardwarePolicy

            prediction = validate(
                read_checkpoint(store, row, "prediction"),
                hardware=getattr(client, "hardware", HardwarePolicy()),
            )
        if prediction is None:
            for relative in (row["audio_path"], job.get("audio_path")):
                path = store.cached_media(relative)
                if path is not None:
                    break
            if path is None:
                update(
                    store,
                    job["id"],
                    state="active",
                    stage="download",
                    download_attempts=row["download_attempts"] + 1,
                    error=None,
                )
                log("diarization_download_start", video_id=job["video_id"], title=job["title"])
                with capacity(store.root, "download"):
                    path, metadata = download(store.root, job["video_id"], concurrent_fragments=2)
                if metadata.get("channel_id") != job["channel_id"]:
                    raise ValueError("Downloaded audio channel identity differs")
            update(store, job["id"], audio_path=store.persist_media(path), download_attempts=0)
            # A long YouTube download can outlive the SSH connection checked before it.
            # Reconnect the configured endpoint before sending retained source audio.
            try:
                health = client.health()
            except httpx.TransportError:
                from .diarization_tunnel import endpoint, ready

                if client.endpoint != endpoint(store.root):
                    raise
                try:
                    client = ready(store.root)
                    health = client.health()
                except transient_errors():
                    raise
                except Exception as error:
                    raise httpx.ConnectError(f"Diarization reconnect failed: {error}") from error
            bind_profile(store, job["profile"], health)
            stage = "diarization"
            update(
                store,
                job["id"],
                state="active",
                stage=stage,
                attempts=row["attempts"] + 1,
                error=None,
            )
            log("diarization_start", video_id=job["video_id"], title=job["title"])
            with capacity(store.root, "gpu"):
                prediction = client.diarize(path)
            bind_profile(store, job["profile"], prediction)
            if abs(prediction["duration"] - asr["duration"]) > max(5, asr["duration"] * 0.01):
                raise ValueError("Diarization audio duration differs from saved ASR")
            target = store.root / row["asr_path"]
            target = target.with_name("diarization.json")
            raw = json.dumps(prediction, ensure_ascii=False, indent=2).encode()
            atomic_write(target, raw)
            update(
                store,
                job["id"],
                prediction_path=str(target.relative_to(store.root)),
                prediction_sha256=hashlib.sha256(raw).hexdigest(),
            )
        stage = "save"
        update(
            store, job["id"], state="active", stage=stage, save_attempts=row["save_attempts"] + 1
        )
        labelled = assign_speakers(asr, prediction)
        # All text formats commit before enrichment completion and audio cleanup.
        store.save(job, labelled, keep_audio=True)
        update(
            store,
            job["id"],
            state="complete",
            stage="complete",
            completed_at=now(),
            error=None,
            speaker_count=prediction["speaker_count"],
            processing_seconds=prediction["processing_seconds"],
        )
        if not keep_audio:
            relative = row["audio_path"] or job.get("audio_path")
            if path:
                store.remove_media(path.relative_to(store.root).as_posix())
            elif relative:
                store.remove_media(relative)
        log(
            "diarization_complete",
            video_id=job["video_id"],
            speakers=prediction["speaker_count"],
            processing_seconds=prediction["processing_seconds"],
        )
        return True
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
                **{ATTEMPTS[stage]: row[ATTEMPTS[stage]]},
            )
        raise
    except (KeyboardInterrupt, SystemExit):
        counter = ATTEMPTS[stage]
        update(
            store,
            job["id"],
            state="pending",
            stage=stage,
            error="Interrupted; checkpoints retained",
            **{counter: row[counter]},
        )
        raise
    except httpx.TransportError as error:
        update(
            store,
            job["id"],
            state="pending",
            stage=stage,
            error=f"Connection interrupted: {error}",
            next_retry=time.time() + 60,
            attempts=row["attempts"],
        )
        log(
            "queue_paused",
            video_id=job["video_id"],
            reason="Diarization connection interrupted; audio and ASR retained",
        )
        raise
    except Exception as error:
        counter = ATTEMPTS[stage]
        attempts = row[counter] + 1
        delay = (
            1800
            if isinstance(error, YouTubeError) and error.kind == "rate_limit"
            else min(1800, 60 * 2 ** max(0, attempts - 1))
        )
        update(
            store,
            job["id"],
            state="failed",
            stage=stage,
            error=str(error),
            next_retry=time.time() + delay,
            **{counter: attempts},
        )
        log(
            "diarization_failed",
            video_id=job["video_id"],
            stage=stage,
            error=str(error),
            retry_after_seconds=delay,
        )
        raise


def run(store, endpoint, *, ids=None, profile="turbo-v1", max_attempts=3, wait=False):
    completed = failed = skipped = 0
    from .inference import client_options

    client = Client(endpoint, **client_options())
    with pipeline_lock(store.root):
        store.recover()
        client.health()
        scope = store.stage_inputs("asr", profile=profile, ids=ids, order="id")
        if ids and set(ids) != {r["video_id"] for r in scope}:
            raise ValueError("Requested video is not transcribed in this profile")
        for job in scope:
            try:
                prepare(store, dict(job))
            except transient_errors():
                raise
            except Exception:
                failed += 1
        skipped = sum(
            store.stage_row("diarization", job["id"])["state"] == "complete" for job in scope
        )
        while True:
            due = None
            ran = False
            for job in scope:
                row = store.stage_row("diarization", job["id"])
                if row["state"] == "complete":
                    continue
                if any(
                    row[k] >= max_attempts
                    for k in ("download_attempts", "attempts", "save_attempts")
                ):
                    continue
                if row["next_retry"] > time.time():
                    due = min(due or row["next_retry"], row["next_retry"])
                    continue
                # Reconnect once before touching any job; infrastructure outages must not
                # consume every video's retry budget or trigger further downloads.
                from .diarization_tunnel import ready

                client = ready(store.root)
                try:
                    completed += int(enrich_one(store, dict(job), client))
                except transient_errors():
                    raise
                except Exception as error:
                    failed += 1
                    if isinstance(error, (httpx.TransportError, ProfileChanged)):
                        return {
                            "completed": completed,
                            "failed": failed,
                            "skipped": skipped,
                            "paused": str(error),
                        }
                    if isinstance(error, YouTubeError) and error.kind == "rate_limit":
                        if not wait:
                            return {"completed": completed, "failed": failed, "skipped": skipped}
                        log(
                            "backoff",
                            reason="YouTube rate limit; pausing all diarization downloads",
                        )
                        for _ in range(1800 // 30):
                            time.sleep(30)
                        break
                ran = True
            if not wait:
                break
            pending = [
                dict(r)
                for r in store.stage_records("diarization", states=("pending", "failed"))
                if r["job_id"] in {j["id"] for j in scope}
                and r["download_attempts"] < max_attempts
                and r["attempts"] < max_attempts
                and r["save_attempts"] < max_attempts
            ]
            if not pending:
                break
            if not ran:
                time.sleep(min(30, max(1, (due or time.time() + 1) - time.time())))
        return {"completed": completed, "failed": failed, "skipped": skipped}
