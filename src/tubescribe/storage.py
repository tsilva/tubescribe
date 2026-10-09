"""SQLite owns state; fsynced files own transcript content."""

from __future__ import annotations

import contextlib
import fcntl
import hashlib
import json
import os
import sqlite3
import time
from datetime import UTC
from pathlib import Path

from .asr import validate
from .subtitles import render_markdown, render_srt


def now() -> str:
    from datetime import datetime

    return datetime.now(UTC).isoformat()


def atomic_write(path: Path, content: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("wb") as f:
        f.write(content)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)
    fd = os.open(path.parent, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


@contextlib.contextmanager
def worker_lock(root: Path):
    root.mkdir(parents=True, exist_ok=True)
    with (root / "worker.lock").open("a") as f:
        try:
            fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise RuntimeError(
                "Another worker owns this archive; inspect status or stop that worker first."
            )
        f.seek(0)
        f.truncate()
        f.write(str(os.getpid()))
        f.flush()
        yield


class Store:
    def __init__(self, root: Path):
        self.root = root.resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(self.root / "archive.sqlite3", timeout=30)
        self.db.row_factory = sqlite3.Row
        self.db.executescript("""
          PRAGMA journal_mode=WAL;
          PRAGMA synchronous=FULL;
          PRAGMA foreign_keys=ON;
          CREATE TABLE IF NOT EXISTS channels (
            id TEXT PRIMARY KEY, url TEXT NOT NULL, title TEXT NOT NULL,
            added_at TEXT NOT NULL);
          CREATE TABLE IF NOT EXISTS discovery (
            channel_id TEXT REFERENCES channels(id), kind TEXT, state TEXT,
            indexed_count INTEGER DEFAULT 0, updated_at TEXT, error TEXT,
            PRIMARY KEY(channel_id,kind));
          CREATE TABLE IF NOT EXISTS videos (
            id TEXT PRIMARY KEY, channel_id TEXT NOT NULL REFERENCES channels(id),
            title TEXT NOT NULL, url TEXT NOT NULL, kind TEXT NOT NULL,
            duration REAL, published_at TEXT, discovered_at TEXT NOT NULL);
          CREATE TABLE IF NOT EXISTS profiles (name TEXT PRIMARY KEY, identity TEXT NOT NULL);
          CREATE TABLE IF NOT EXISTS jobs (
            id INTEGER PRIMARY KEY, video_id TEXT NOT NULL REFERENCES videos(id),
            profile TEXT NOT NULL, download_state TEXT NOT NULL DEFAULT 'pending',
            asr_state TEXT NOT NULL DEFAULT 'pending', state TEXT NOT NULL DEFAULT 'pending',
            audio_path TEXT, download_attempts INTEGER NOT NULL DEFAULT 0,
            asr_attempts INTEGER NOT NULL DEFAULT 0, next_retry REAL NOT NULL DEFAULT 0,
            failed_stage TEXT, error_kind TEXT, error TEXT, updated_at TEXT NOT NULL,
            completed_at TEXT, UNIQUE(video_id,profile));
          CREATE TABLE IF NOT EXISTS transcripts (
            job_id INTEGER PRIMARY KEY REFERENCES jobs(id), json_path TEXT NOT NULL,
            text_path TEXT NOT NULL, json_sha256 TEXT NOT NULL, text_sha256 TEXT NOT NULL,
            language TEXT NOT NULL, model TEXT NOT NULL, revision TEXT NOT NULL,
            processing_seconds REAL NOT NULL, duration REAL NOT NULL, saved_at TEXT NOT NULL);
        """)

    def channel(self, channel: dict):
        with self.db:
            self.db.execute(
                "INSERT INTO channels VALUES(?,?,?,?) ON CONFLICT(id) DO UPDATE SET url=excluded.url,title=excluded.title",
                (channel["id"], channel["url"], channel["title"], now()),
            )

    def index(self, channel_id: str, kind: str, videos: list[dict], profile: str):
        with self.db:
            for v in videos:
                self.db.execute(
                    """INSERT INTO videos VALUES(?,?,?,?,?,?,?,?) ON CONFLICT(id)
                    DO UPDATE SET title=excluded.title,duration=COALESCE(excluded.duration,videos.duration),
                    published_at=COALESCE(excluded.published_at,videos.published_at)""",
                    (
                        v["id"],
                        channel_id,
                        v["title"],
                        v["url"],
                        kind,
                        v.get("duration"),
                        v.get("published_at"),
                        now(),
                    ),
                )
                self.db.execute(
                    "INSERT OR IGNORE INTO jobs(video_id,profile,updated_at) VALUES(?,?,?)",
                    (v["id"], profile, now()),
                )
            self.discovery(channel_id, kind, "complete", len(videos))

    def discovery(self, channel_id, kind, state, count=0, error=None):
        self.db.execute(
            """INSERT INTO discovery VALUES(?,?,?,?,?,?) ON CONFLICT(channel_id,kind)
            DO UPDATE SET state=excluded.state,indexed_count=excluded.indexed_count,
            updated_at=excluded.updated_at,error=excluded.error""",
            (channel_id, kind, state, count, now(), error),
        )
        self.db.commit()

    def update(self, job_id: int, **values):
        values["updated_at"] = now()
        with self.db:
            self.db.execute(
                f"UPDATE jobs SET {','.join(k + '=?' for k in values)} WHERE id=?",
                (*values.values(), job_id),
            )

    def bind_profile(self, profile: str, health: dict):
        keys = (
            "model",
            "revision",
            "backend",
            "backend_version",
            "ctranslate2_version",
            "compute_type",
            "decoding",
        )
        identity = json.dumps({k: health.get(k) for k in keys}, sort_keys=True)
        with self.db:
            existing = self.db.execute(
                "SELECT identity FROM profiles WHERE name=?", (profile,)
            ).fetchone()
            if existing and existing[0] != identity:
                raise RuntimeError(
                    f"ASR model/settings changed for {profile}; use an explicit new --profile to upgrade"
                )
            self.db.execute("INSERT OR IGNORE INTO profiles VALUES(?,?)", (profile, identity))

    def recover(self):
        with self.db:
            self.db.execute(
                "UPDATE jobs SET state='pending', download_state=CASE WHEN download_state='active' THEN 'pending' ELSE download_state END, asr_state=CASE WHEN asr_state='active' THEN 'pending' ELSE asr_state END WHERE state='active'"
            )
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
                self.update(
                    r["id"],
                    state="pending",
                    asr_state="pending",
                    error="Saved transcript missing or corrupt; repair queued",
                )
            else:
                # Subtitles are derived content: repair them without another download or ASR.
                self._subtitle(r)

    def jobs(self, *, profile="turbo-v1", ids=None, channel=None, max_attempts=3):
        conditions = [
            "j.profile=?",
            "j.state IN ('pending','failed')",
            "j.next_retry<=?",
            "j.download_attempts<?",
            "j.asr_attempts<?",
        ]
        args = [profile, time.time(), max_attempts, max_attempts]
        if ids:
            conditions.append(f"v.id IN ({','.join('?' for _ in ids)})")
            args.extend(ids)
        if channel:
            conditions.append("v.channel_id=?")
            args.append(channel)
        return self.db.execute(
            f"SELECT j.*,v.title,v.channel_id,v.url,v.kind,v.duration,v.published_at FROM jobs j JOIN videos v ON j.video_id=v.id WHERE {' AND '.join(conditions)} ORDER BY COALESCE(v.published_at,'') DESC,j.id",
            args,
        ).fetchall()

    def save(self, job: dict, result: dict, *, keep_audio=False):
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
            f"[{timestamp(s['start'])} --> {timestamp(s['end'])}] {s['text'].strip()}"
            for s in result["segments"]
        ]
        txt = ("\n".join(lines) + "\n").encode()
        jp, tp = folder / "transcript.json", folder / "transcript.txt"
        atomic_write(self.root / jp, js)
        atomic_write(self.root / tp, txt)
        atomic_write(self.root / folder / "transcript.srt", render_srt(result["segments"]))
        atomic_write(self.root / folder / "transcript.md", render_markdown(result["segments"]))
        with self.db:
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

    def retry(self, ids=None):
        sql = "UPDATE jobs SET state='pending',next_retry=0,download_attempts=0,asr_attempts=0,error=NULL,error_kind=NULL WHERE state='failed'"
        args = []
        if ids:
            sql += f" AND video_id IN ({','.join('?' for _ in ids)})"
            args = ids
        with self.db:
            return self.db.execute(sql, args).rowcount
