"""Stage dependencies and retry policy, shared by execution and presentation."""

from dataclasses import dataclass

from .blobs import ArtifactRef
from .config import OPENROUTER_PROFILES


@dataclass(frozen=True)
class Stage:
    name: str
    state: str
    active: str
    waiting: str
    counters: tuple[str, ...]


STAGES = (
    Stage("asr", "state", "transcribing", "queued", ("download_attempts", "asr_attempts")),
    Stage(
        "diarization",
        "diarization_state",
        "diarizing",
        "awaiting_diarization",
        ("download_attempts", "attempts", "save_attempts"),
    ),
    Stage(
        "postprocessing",
        "postprocessing_state",
        "postprocessing",
        "awaiting_postprocessing",
        ("download_attempts", "attempts", "save_attempts"),
    ),
    Stage(
        "summary", "summary_state", "summarizing", "awaiting_summary", ("attempts", "save_attempts")
    ),
)


@dataclass(frozen=True)
class Plan:
    diarization: bool = True
    postprocessing: bool = True
    summary: bool = True

    @property
    def stages(self):
        # Later stages always imply their dependencies.
        return STAGES[
            : 4 if self.summary else 3 if self.postprocessing else 2 if self.diarization else 1
        ]

    def next_stage(self, job):
        stages = self.stages
        if job.get("profile") in OPENROUTER_PROFILES:
            # Hosted transcription can carry speaker labels, but has no separate diarization step.
            stages = (stage for stage in stages if stage.name != "diarization")
        return next((stage for stage in stages if job.get(stage.state) != "complete"), None)

    def complete(self, job):
        return self.next_stage(job) is None

    def due(self, job, max_attempts=3):
        stage = self.next_stage(job)
        if stage is None:
            return None
        prefix = "" if stage.name == "asr" else stage.name + "_"
        state = job.get(stage.state, "pending")
        if state not in ("pending", "failed", None):
            return None
        saving = job.get(prefix + "stage") == "save"
        if any(
            (job.get(prefix + counter) or 0) >= max_attempts
            for counter in stage.counters
            if not (
                saving and counter == "attempts" and stage.name in ("postprocessing", "summary")
            )
        ):
            return None
        return job.get(prefix + "next_retry") or 0


def retryable(row, max_attempts=3, *, download=False):
    """A saved response does not consume another model attempt."""
    return (
        (row.get("stage") == "save" or row.get("attempts", 0) < max_attempts)
        and row.get("save_attempts", 0) < max_attempts
        and (not download or row.get("download_attempts", 0) < max_attempts)
    )


def status(job):
    stage = Plan().next_stage(job)
    if stage is None:
        return "complete"
    state = job.get(stage.state)
    if state == "failed":
        return "failed"
    if stage.name == "asr":
        if job.get("asr_state") == "active":
            return "transcribing"
        if job.get("download_state") == "active":
            return "downloading"
        return "queued" if job.get("state") else "indexed"
    if state == "active":
        return "downloading" if job.get(stage.name + "_stage") == "download" else stage.active
    return stage.waiting


@dataclass(frozen=True)
class StageInput:
    job_id: int
    stage: str
    source: ArtifactRef | None = None


@dataclass(frozen=True)
class StageResult:
    input: StageInput
    changed: bool
    publication: ArtifactRef | None = None


def finish(
    store,
    job,
    *,
    bridge=None,
    mode="video",
    video_cache=None,
    summary=True,
    max_attempts=3,
    on_stage=None,
):
    """Run the final dependent stages; their checkpoint handlers own retries."""
    from .artifacts import build_one
    from .postprocessing_worker import process_one

    results = []
    if mode != "off":
        if on_stage:
            on_stage("postprocessing")
        source = store.transcript(job["id"])
        stage_input = StageInput(
            job["id"], "postprocessing", ArtifactRef(source["json_path"], source["json_sha256"])
        )
        changed = process_one(store, job, bridge=bridge, mode=mode, video_cache=video_cache)
        row = store.stage_row("postprocessing", job["id"])
        publication = ArtifactRef(row["manifest_path"], row["manifest_sha256"])
        results.append(StageResult(stage_input, changed, publication))
        if summary:
            if on_stage:
                on_stage("summary")
            stage_input = StageInput(job["id"], "summary", publication)
            changed = build_one(
                store, job["id"], "summary", bridge=bridge, max_attempts=max_attempts
            )
            row = store.stage_row("artifact", job["id"], workflow="summary")
            publication = ArtifactRef(row["output_dir"] + "/manifest.json", row["manifest_sha256"])
            results.append(StageResult(stage_input, changed, publication))
    return results
