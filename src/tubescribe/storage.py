"""SQLite owns state; fsynced files own transcript content."""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
from contextvars import ContextVar
from pathlib import Path

from .asr import validate
from .blobs import ArtifactRef
from .blobs import atomic_write as file_write
from .repository import JobRepository, now
from .repository import ProfileChanged as ProfileChanged
from .subtitles import render_markdown, render_srt

_WRITES = ContextVar("durable_writes", default=None)


def atomic_write(path, content):
    file_write(path, content)
    store = _WRITES.get()
    if store is not None:
        store.checkpoint(path)


@contextlib.contextmanager
def durable_writes(store):
    token = _WRITES.set(store)
    try:
        yield
    finally:
        _WRITES.reset(token)


@contextlib.contextmanager
def worker_lock(root: Path):
    root.mkdir(parents=True, exist_ok=True)
    from .execution import lease

    with lease(root, "archive", reentrant=False):
        (root / "worker.lock").write_text(str(os.getpid()))
        yield


class Store:
    def __init__(self, root: Path, *, blobs=None, database=None, records=None):
        self.root = Path(root).resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self.records = records or JobRepository(self.root, database=database)
        from .blobs import DirectoryObjects, LocalArtifacts
        from .config import RuntimeConfig

        self.config = RuntimeConfig.load(self.root)
        self._uploaded = {}

        self.blobs = blobs or (
            DirectoryObjects(self.config.objects)
            if self.config.objects
            else LocalArtifacts(self.root, writer=lambda path, content: atomic_write(path, content))
        )
        self.local_blobs = isinstance(self.blobs, LocalArtifacts) and self.blobs.root == self.root

    def __getattr__(self, name):
        return getattr(self.records, name)

    def read_key(self, key, checksum=None):
        return self.blobs.read(ArtifactRef(key, checksum)) if checksum else self.blobs.get(key)

    def exists(self, key, checksum=None):
        try:
            if checksum:
                self.read_key(key, checksum)
            else:
                self.blobs.stat(key)
            return True
        except (OSError, ValueError):
            return False

    def materialize(self, key, checksum=None):
        from .blobs import checked_key

        key = checked_key(key)
        path = (self.root / key).resolve()
        if not path.is_relative_to(self.root):
            raise ValueError("Scratch path escapes runtime directory")
        if not self.local_blobs:
            self.blobs.copy_to(key, path, checksum)
        elif checksum:
            from .blobs import file_digest

            if file_digest(path) != checksum:
                raise ValueError("Artifact content does not match its reference")
        return path

    def materialize_prefix(self, key):
        from .blobs import checked_key

        key = checked_key(key)
        if not (self.root / key).resolve().is_relative_to(self.root):
            raise ValueError("Scratch prefix escapes runtime directory")
        if not self.local_blobs:
            for name in self.blobs.keys(key.rstrip("/") + "/"):
                self.materialize(name)
        return self.root / key

    def persist_media(self, path):
        key = Path(path).resolve().relative_to(self.root).as_posix()
        if not key.startswith(("audio/", "videos/")):
            raise ValueError("Media must belong to the archive cache")
        if not self.local_blobs:
            ref = self.blobs.put_file(key, path)
            self.blobs.put(key + ".reference.json", json.dumps({"sha256": ref.sha256}).encode())
        return key

    def cached_media(self, key):
        if not key or not self.exists(key):
            return None
        checksum = None
        if self.exists(key + ".reference.json"):
            checksum = json.loads(self.read_key(key + ".reference.json"))["sha256"]
        return self.materialize(key, checksum)

    def remove_media(self, key):
        from .blobs import checked_key

        key = checked_key(key)
        if not key.startswith(("audio/", "videos/")):
            raise ValueError("Media must belong to the archive cache")
        self.blobs.delete(key)
        self.blobs.delete(key + ".reference.json")
        if not self.local_blobs:
            path = (self.root / key).resolve()
            if not path.is_relative_to(self.root):
                raise ValueError("Media path escapes scratch")
            path.unlink(missing_ok=True)

    def checkpoint(self, path):
        """Persist scratch checkpoints before their state pointer or tool response escapes."""
        if self.local_blobs:
            return
        path = Path(path).resolve()
        if not path.is_relative_to(self.root):
            raise ValueError("Checkpoint escapes scratch")
        directory = path.parent
        # Frame tools create PNGs with ffmpeg, then atomically save the assets ledger.
        candidates = [path]
        if path.name in {"assets.json", "tool-calls.json", "searches.json"}:
            candidates += list((directory / "figures").glob("*.png"))
        for candidate in candidates:
            if candidate.is_file():
                key = candidate.relative_to(self.root).as_posix()
                content = candidate.read_bytes()
                checksum = hashlib.sha256(content).hexdigest()
                if self._uploaded.get(key) != checksum:
                    self.blobs.put(key, content)
                    self._uploaded[key] = checksum

    def publish(self, staging):
        import shutil
        import uuid

        from .blobs import publish_bundle

        # Each commit points at a fresh bundle; readers never observe a repaired bundle mid-write.
        target = staging.parent / uuid.uuid4().hex
        shutil.copytree(staging, target)
        ref = publish_bundle(
            self.blobs, target, target.relative_to(self.root).as_posix(), writer=atomic_write
        )
        return target, ref

    def bundle_valid(self, key, checksum):
        try:
            manifest = json.loads(self.read_key(key + "/manifest.json", checksum))
            from .blobs import checked_key

            return bool(manifest["files"]) and all(
                self.exists(key + "/" + checked_key(name), digest)
                for name, digest in manifest["files"].items()
            )
        except (OSError, ValueError, KeyError, TypeError):
            return False

    def recover(self):
        self.reset_interrupted()
        # Reopen completed jobs only if their authoritative content has vanished/corrupted.
        for r in self.transcript_records():
            valid = all(self.exists(r[k + "_path"], r[k + "_sha256"]) for k in ("json", "text"))
            if not valid:
                checkpoint = self.stage_row("diarization", r["id"])
                recoverable = checkpoint and all(
                    checkpoint[k + "_path"]
                    and self.exists(checkpoint[k + "_path"], checkpoint[k + "_sha256"])
                    for k in ("asr", "prediction")
                )
                if recoverable:
                    self.stage_update(
                        "diarization",
                        r["id"],
                        state="pending",
                        stage="save",
                        next_retry=0,
                        save_attempts=0,
                        error="Output repair queued from checkpoints",
                    )
                    continue
                self.update(
                    r["id"],
                    state="pending",
                    asr_state="pending",
                    asr_attempts=0,
                    next_retry=0,
                    error="Saved transcript missing or corrupt; repair queued",
                )
                if checkpoint:
                    # An ASR rebuild must not inherit completion from unusable enrichment.
                    # Leave the old files as evidence, but establish new hashes/checkpoints.
                    self.stage_update(
                        "diarization",
                        r["id"],
                        state="pending",
                        stage=None,
                        asr_path=None,
                        asr_sha256=None,
                        prediction_path=None,
                        prediction_sha256=None,
                        download_attempts=0,
                        attempts=0,
                        save_attempts=0,
                        next_retry=0,
                        error="Unusable checkpoints; ASR repair queued",
                    )
            else:
                # Subtitles are derived content: repair them without another download or ASR.
                self._subtitle(r)
        from .postprocessing_worker import recover

        recover(self)
        from .artifacts import recover as recover_artifacts

        recover_artifacts(self)

    def save(self, job: dict, result: dict, *, keep_audio=False):
        import uuid

        folder = (
            Path("transcripts")
            / job["channel_id"]
            / job["video_id"]
            / job["profile"]
            / "sources"
            / uuid.uuid4().hex
        )
        result = {
            **result,
            "schema_version": 1,
            "source": "asr",
            "video_id": job["video_id"],
            "title": job["title"],
            "channel_id": job["channel_id"],
            "source_url": job["url"],
            "kind": job["kind"],
            "profile": job["profile"],
            "saved_at": now(),
        }
        js = json.dumps(result, ensure_ascii=False, indent=2).encode()

        def timestamp(t):
            return f"{int(t) // 3600:02}:{int(t) % 3600 // 60:02}:{t % 60:06.3f}"

        lines = [
            result["title"],
            result["source_url"],
            f"Language: {result['language']} | ASR: {result['model']} @ {result['revision']}",
            "",
        ]
        lines += [
            f"[{timestamp(s['start'])} --> {timestamp(s['end'])}] "
            + (
                f"{s.get('speaker_name') or s.get('speaker_id')}: "
                if s.get("speaker_name") or s.get("speaker_id")
                else ""
            )
            + s["text"].strip()
            for s in result["segments"]
        ]
        txt = ("\n".join(lines) + "\n").encode()
        jp, tp = folder / "transcript.json", folder / "transcript.txt"
        for key, content in (
            (jp, js),
            (tp, txt),
            (folder / "transcript.srt", render_srt(result["segments"])),
            (folder / "transcript.md", render_markdown(result["segments"])),
        ):
            ref = self.blobs.put(str(key), content)
            if self.blobs.read(ref) != content:
                raise ValueError("Artifact store failed transcript publication verification")
        self.commit_transcript(
            (
                job["id"],
                str(jp),
                str(tp),
                hashlib.sha256(js).hexdigest(),
                hashlib.sha256(txt).hexdigest(),
                result["language"],
                result["model"],
                result["revision"],
                result["processing_seconds"],
                result["duration"],
                result["saved_at"],
            )
        )
        # Never delete the retry source until all formats and the SQLite commit are durable.
        if job.get("audio_path") and not keep_audio:
            self.remove_media(job["audio_path"])

    def _subtitle(self, transcript, destination=None):
        source = self.materialize(transcript["json_path"], transcript["json_sha256"])
        raw = self.read_key(transcript["json_path"], transcript["json_sha256"])
        if hashlib.sha256(raw).hexdigest() != transcript["json_sha256"]:
            raise ValueError(f"Saved JSON is corrupt: {source}")
        result = validate(json.loads(raw))
        path = (
            destination / f"{result['video_id']}.{result['profile']}.srt"
            if destination is not None
            else source.with_suffix(".srt")
        )
        content = render_srt(result["segments"])
        if not path.is_file() or path.read_bytes() != content:
            atomic_write(path, content)
        if destination is None:
            self.blobs.put(path.relative_to(self.root).as_posix(), content)
        markdown = path.with_suffix(".md")
        content = render_markdown(result["segments"])
        if not markdown.is_file() or markdown.read_bytes() != content:
            atomic_write(markdown, content)
        if destination is None:
            self.blobs.put(markdown.relative_to(self.root).as_posix(), content)
        return [str(path), str(markdown)]

    def subtitles(self, *, ids=None, profile=None, destination=None):
        rows = self.transcript_records(ids=ids, profile=profile)
        if ids and set(ids) != {r["video_id"] for r in rows}:
            raise ValueError(
                "One or more requested videos have no completed transcript in this scope"
            )
        return [path for row in rows for path in self._subtitle(row, destination)]
