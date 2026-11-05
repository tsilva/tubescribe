"""One bounded unit of owner-selected processing per Workflow step."""

import base64
import json
import math
import time
from contextlib import ExitStack
from datetime import UTC, datetime

from .agent_gateway import DurableRunPending
from .asr import validate
from .billing import tracking
from .config import OPENROUTER_PROFILE
from .execution import CURRENT_SUBMISSION, lease
from .inference import PostprocessingUnavailable
from .manual_queue import authorize_execution
from .openrouter import MODEL, OpenRouter, TranscriptionRateLimited, cached_response, cues
from .pipeline import Plan
from .repository import now
from .speaker_continuity import RATE, SpeakerContinuity
from .storage import durable_writes
from .submission import JobRequest


def prefix(job):
    return f"transcripts/{job['channel_id']}/{job['video_id']}/{job['profile']}"


def asr_step(store, job, media, send):
    key = prefix(job) + "/workflow/asr.json"
    state = json.loads(store.read_key(key)) if store.exists(key) else {
        "part": 0, "segments": [], "generations": [], "costs": [], "languages": [],
        "bank": {}, "started_at": now(), "seconds": 0,
    }
    bank_values = state["bank"]
    bank = SpeakerContinuity(**{**bank_values, "anchors": {
        name: base64.b64decode(audio) for name, audio in bank_values.get("anchors", {}).items()
    }})
    if state["part"] < len(media["parts"]):
        started = time.monotonic()
        index = state["part"]
        part = media["parts"][index]
        pcm = store.read_key(part["key"], part["sha256"])
        if len(pcm) != round(part["seconds"] * RATE) * 2:
            raise ValueError("Prepared audio chunk length differs from its manifest")
        audio, reference_seconds, references = bank.request(pcm)
        item = cached_response(store, f"{prefix(job)}/openrouter/{index:06d}", None, audio,
                               send=send)
        response = item["response"]
        cues(response, 0, part["seconds"] + reference_seconds)
        cleaned, labels = bank.apply(response, pcm, reference_seconds, references)
        offset = sum(p["seconds"] for p in media["parts"][:index])
        state["segments"].extend(cues(cleaned, offset, part["seconds"], speaker_labels=labels))
        cost = (response.get("usage") or {}).get("cost")
        state["costs"].append(cost if type(cost) in (int, float)
                              and math.isfinite(cost) and cost >= 0 else None)
        state["generations"].append(item.get("generation_id"))
        if response.get("language"):
            state["languages"].append(response["language"])
        state["bank"] = {**vars(bank), "anchors": {
            name: base64.b64encode(audio).decode() for name, audio in bank.anchors.items()
        }}
        state["part"] += 1
        state["seconds"] += time.monotonic() - started
        store.blobs.put(key, json.dumps(state).encode())
        if state["part"] < len(media["parts"]):
            return False
    result = validate({
        **OpenRouter().health(), "segments": state["segments"], "duration": media["duration"],
        "language": state["languages"][0] if state["languages"] else "und",
        "processing_seconds": max(.001, state["seconds"]), "started_at": state["started_at"],
        "finished_at": now(), "generation_ids": state["generations"],
        "timing_resolution": "audio_part" if any(
            segment.get("timing_resolution") == "audio_part" for segment in state["segments"]
        ) else "word", "cost": sum(state["costs"])
        if all(c is not None for c in state["costs"]) else None,
        "speaker_continuity": {"method": "audio-references-v1",
                               "reference_seconds": bank.reference_seconds,
                               "matched_references": bank.matched,
                               "unresolved_references": bank.unresolved},
        "speaker_name_provenance": "Anonymous voices matched using audio reference clips; "
                                   "ambiguous matches remain unidentified",
    })
    store.bind_profile(job["profile"], result)
    speakers = {segment["speaker_id"] for segment in result["segments"]}
    store.save(job, result, hosted_speakers=len(speakers) if None not in speakers else None)
    return True


def audio_send(audio):
    from .cloud_auth import workflow_send

    # WAV is already prepared; no ffmpeg or media machine is needed during ASR.
    return workflow_send({
        "model": MODEL,
        "input_audio": {"data": base64.b64encode(audio).decode(), "format": "wav"},
        "response_format": "verbose_json", "timestamp_granularities": ["word", "segment"],
        "provider": {"options": {"azure": {"diarization": {"enabled": True}}}},
    })


def agent_runner(store, endpoint, run_key, *, profile=None):
    from .cloud_auth import workflow_send
    from .inference import CapacityAgentRunner
    from .openrouter_agent import OpenRouterAgentRunner

    return CapacityAgentRunner(OpenRouterAgentRunner(
        store, run_key, send=lambda body: workflow_send({"operation": "chat", "request": body}),
        max_new_calls=1), store.root, defer=True)


def advance_rollup(store, identifier, row, request):
    """Preserve the existing seven-day digest using already selected video summaries."""
    from . import rollups

    if request.encode() != JobRequest("rollups", {}).encode() or row["dedupe"] != request.dedupe:
        raise ValueError("Unsupported cloud digest request")
    with lease(store.root, "submission:" + identifier), lease(store.root, "rolling-summaries"):
        if rollups.video_work_pending(store):
            return {"done": False, "delay": 60}
        clock = datetime.fromtimestamp(row["created"], UTC)
        entries = rollups.index(store, clock=clock)["items"]
        entry = next((entry for entry in entries if entry["state"] != "ready" and entry["scope"] != "all"), None)
        entry = entry or next((entry for entry in entries if entry["state"] != "ready"), None)
        if entry is None:
            store.update_submission(identifier, state="complete", worker_id=None,
                                    result=json.dumps({"completed": 1, "failed": 0}))
            return {"done": True, "state": "complete"}
        saved = rollups.records(store).get(entry["scope"], {})
        if saved.get("state") == "failed" and saved.get("attempts", 0) >= 3:
            raise ValueError("Digest attempt budget exhausted")
        if saved.get("next_retry", 0) > time.time():
            return {"done": False, "delay": 60}
        store.update_submission(identifier, state="active", worker_id="workflow", pid=None)
        try:
            rollups.build(store, entry["scope"], runner_factory=agent_runner, clock=clock)
        except DurableRunPending as error:
            return {"done": False, "delay": error.retry_after}
        return {"done": False, "delay": 0}


def advance(store, identifier, *, send=None, media_prepare=None):
    """No input video IDs: every operation revalidates durable manual admission."""
    from .cloud_sandbox import RemoteScreenshots, prepare

    row = store.submission(identifier)
    if not row or row["state"] not in {"queued", "active", "complete"}:
        return {"done": True, "state": "cancelled"}
    request = JobRequest(**json.loads(row["request"]))
    if request.command == "rollups":
        if row["state"] == "complete":
            return {"done": True, "state": "complete"}
        return advance_rollup(store, identifier, row, request)
    authorize_execution(store, request, identifier)
    if request.options.get("profile") != OPENROUTER_PROFILE:
        raise ValueError("Cloud Workflows support the current MAI profile only")
    if row["state"] == "complete":
        return {"done": True, "state": "complete"}
    with ExitStack() as stack:
        stack.enter_context(lease(store.root, "submission:" + identifier))
        token = CURRENT_SUBMISSION.set(identifier)
        stack.callback(CURRENT_SUBMISSION.reset, token)
        job = store.pipeline_jobs(ids=request.options["video"], profile=OPENROUTER_PROFILE)[0]
        stack.enter_context(lease(store.root, "video:" + job["video_id"]))
        stack.enter_context(lease(store.root, "job:" + str(job["id"])))
        stack.enter_context(durable_writes(store))
        store.update_submission(identifier, state="active", worker_id="workflow", pid=None)
        stage = Plan().next_stage(job)
        asr_started = False
        if stage is None:
            store.update_submission(identifier, state="complete", worker_id=None,
                                    result=json.dumps({"completed": 1, "failed": 0}))
            return {"done": True, "state": "complete"}
        try:
            if stage.name == "asr":
                store.bind_profile(job["profile"], OpenRouter().health())
                store.update(job["id"], state="active", download_state="active")
                media = (media_prepare or prepare)(store, job, identifier)
                store.update(job["id"], download_state="complete", asr_state="active")
                asr_started = True
                attempt = store.begin_attempt(job["id"], "asr")
                try:
                    with tracking(store, attempt):
                        completed = asr_step(store, job, media, send or audio_send)
                except BaseException:
                    store.finish_attempt(attempt, "failed", error="Chunk step suspended or failed")
                    raise
                store.finish_attempt(attempt, "complete", metrics={"chunk_step": True,
                                                                    "asr_complete": completed})
            else:
                from . import artifacts, postprocessing_worker

                # Existing stage handlers own publication, charges and recovery.
                # Explicit injection avoids changing any other runtime's transport.
                if stage.name == "postprocessing":
                    media = (media_prepare or prepare)(store, job, identifier)
                    postprocessing_worker.process_one(
                        store, job, runner_factory=agent_runner,
                        media_factory=lambda output, duration, budgets: RemoteScreenshots(
                            store, media, output, duration, budgets),
                    )
                else:
                    artifacts.build_one(store, job["id"], "summary", runner_factory=agent_runner)
            return {"done": False, "delay": 0}
        except DurableRunPending as error:
            return {"done": False, "delay": error.retry_after}
        except TranscriptionRateLimited:
            store.update(job["id"], state="pending", asr_state="pending", next_retry=0)
            return {"done": False, "delay": 60}
        except PostprocessingUnavailable as error:
            if "unknown" not in str(error).lower() and "review" not in str(error).lower():
                return {"done": False, "delay": 60}
            store.update_submission(identifier, state="failed", worker_id=None,
                                    error="Provider outcome needs review; saved results retained")
            return {"done": True, "state": "needs_review"}
        except Exception as error:
            from .blobs import StorageUnavailable
            from .database import transient_errors

            if isinstance(error, (StorageUnavailable, *transient_errors())):
                raise
            needs_review = "outcome unknown" in str(error).lower() or "check billing" in str(error).lower()
            message = "Provider outcome needs review; saved intent retained" if needs_review else "Cloud stage failed; saved results retained"
            if stage.name == "asr":
                store.update(job["id"], state="failed", error=message,
                             error_kind="asr" if asr_started else "download",
                             **({"asr_state": "failed"} if asr_started else {"download_state": "failed"}))
            store.update_submission(identifier, state="failed", worker_id=None,
                                    error=message)
            if needs_review:
                return {"done": True, "state": "needs_review"}
            raise
