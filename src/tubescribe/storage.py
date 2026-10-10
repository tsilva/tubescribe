"""SQLite owns state; fsynced files own transcript content."""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
from pathlib import Path

from .asr import validate
from .blobs import atomic_write
from .repository import JobRepository, now
from .repository import ProfileChanged as ProfileChanged
from .subtitles import render_markdown, render_srt


@contextlib.contextmanager
def worker_lock(root: Path):
    root.mkdir(parents=True, exist_ok=True)
    from .execution import lease

    with lease(root, "archive", reentrant=False):
        (root / "worker.lock").write_text(str(os.getpid()))
        yield


class Store(JobRepository):
    def __init__(self, root: Path, *, blobs=None):
        super().__init__(root)
        from .blobs import LocalArtifacts

        self.blobs = blobs or LocalArtifacts(
            self.root, writer=lambda path, content: atomic_write(path, content)
        )

    def recover(self):
        self.reset_interrupted()
        # Reopen completed jobs only if their authoritative content has vanished/corrupted.
        for r in self.db.execute(
            "SELECT j.id,t.* FROM jobs j JOIN transcripts t ON j.id=t.job_id WHERE j.state='complete'"
        ).fetchall():
            valid = all(
                (self.root / r[k + "_path"]).is_file()
                and hashlib.sha256((self.root / r[k + "_path"]).read_bytes()).hexdigest()
                == r[k + "_sha256"]
                for k in ("json", "text")
            )
            if not valid:
                checkpoint = self.db.execute(
                    "SELECT * FROM diarizations WHERE job_id=?", (r["id"],)
                ).fetchone()
                recoverable = checkpoint and all(
                    checkpoint[k + "_path"]
                    and (self.root / checkpoint[k + "_path"]).is_file()
                    and hashlib.sha256(
                        (self.root / checkpoint[k + "_path"]).read_bytes()
                    ).hexdigest()
                    == checkpoint[k + "_sha256"]
                    for k in ("asr", "prediction")
                )
                if recoverable:
                    with self.db:
                        self.db.execute(
                            "UPDATE diarizations SET state='pending',stage='save',next_retry=0,save_attempts=0,error='Output repair queued from checkpoints' WHERE job_id=?",
                            (r["id"],),
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
                    with self.db:
                        self.db.execute(
                            "UPDATE diarizations SET state='pending',stage=NULL,asr_path=NULL,asr_sha256=NULL,prediction_path=NULL,prediction_sha256=NULL,download_attempts=0,attempts=0,save_attempts=0,next_retry=0,error='Unusable checkpoints; ASR repair queued' WHERE job_id=?",
                            (r["id"],),
                        )
            else:
                # Subtitles are derived content: repair them without another download or ASR.
                self._subtitle(r)
        from .postprocessing_worker import recover

        recover(self)
        from .artifacts import recover as recover_artifacts

        recover_artifacts(self)

    def save(self, job: dict, result: dict, *, keep_audio=False):
        from .execution import assert_owned

        with self.db:
            assert_owned(self.db, self.root)
        folder = Path("transcripts") / job["channel_id"] / job["video_id"] / job["profile"]
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
        with self.db:
            assert_owned(self.db, self.root)
            for key, content in (
                (jp, js),
                (tp, txt),
                (folder / "transcript.srt", render_srt(result["segments"])),
                (folder / "transcript.md", render_markdown(result["segments"])),
            ):
                ref = self.blobs.put(str(key), content)
                if self.blobs.read(ref) != content:
                    raise ValueError("Artifact store failed transcript publication verification")
            self.db.execute(
                "INSERT OR REPLACE INTO transcripts VALUES(?,?,?,?,?,?,?,?,?,?,?)",
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
                ),
            )
            self.db.execute(
                "UPDATE jobs SET state='complete',asr_state='complete',completed_at=?,updated_at=?,error=NULL,error_kind=NULL,failed_stage=NULL WHERE id=?",
                (now(), now(), job["id"]),
            )
        # Never delete the retry source until all formats and the SQLite commit are durable.
        if job.get("audio_path") and not keep_audio:
            (self.root / job["audio_path"]).unlink(missing_ok=True)

    def _subtitle(self, transcript, destination=None):
        source = self.root / transcript["json_path"]
        raw = source.read_bytes()
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
        markdown = path.with_suffix(".md")
        content = render_markdown(result["segments"])
        if not markdown.is_file() or markdown.read_bytes() != content:
            atomic_write(markdown, content)
        return [str(path), str(markdown)]

    def subtitles(self, *, ids=None, profile=None, destination=None):
        conditions = ["j.state='complete'"]
        values = []
        if ids:
            conditions.append(f"j.video_id IN ({','.join('?' for _ in ids)})")
            values.extend(ids)
        if profile:
            conditions.append("j.profile=?")
            values.append(profile)
        rows = self.db.execute(
            f"SELECT t.*,j.video_id FROM transcripts t JOIN jobs j ON j.id=t.job_id WHERE {' AND '.join(conditions)} ORDER BY j.id",
            values,
        ).fetchall()
        if ids and set(ids) != {r["video_id"] for r in rows}:
            raise ValueError(
                "One or more requested videos have no completed transcript in this scope"
            )
        return [path for row in rows for path in self._subtitle(row, destination)]
