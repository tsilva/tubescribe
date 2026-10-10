"""SQLite job state and selection; content lives in the artifact store."""

from __future__ import annotations

import json
import sqlite3
import time
from datetime import UTC, datetime
from pathlib import Path

from .pipeline import STAGES, Plan


class ProfileChanged(RuntimeError):
    """An archive profile cannot silently switch model/settings."""


def now():
    return datetime.now(UTC).isoformat()


class JobRepository:
    def __init__(self, root: Path):
        self.root = root.resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(self.root / "archive.sqlite3", timeout=30)
        self.db.row_factory = sqlite3.Row
        from .execution import SCHEMA

        self.db.executescript(SCHEMA)
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
          CREATE TABLE IF NOT EXISTS diarization_profiles (
            profile TEXT PRIMARY KEY, identity TEXT NOT NULL);
          CREATE TABLE IF NOT EXISTS diarizations (
            job_id INTEGER PRIMARY KEY REFERENCES jobs(id), state TEXT NOT NULL DEFAULT 'pending',
            stage TEXT, audio_path TEXT, download_attempts INTEGER NOT NULL DEFAULT 0,
            attempts INTEGER NOT NULL DEFAULT 0, save_attempts INTEGER NOT NULL DEFAULT 0, next_retry REAL NOT NULL DEFAULT 0,
            error TEXT, updated_at TEXT NOT NULL, completed_at TEXT,
            asr_path TEXT, asr_sha256 TEXT, prediction_path TEXT, prediction_sha256 TEXT,
            speaker_count INTEGER, processing_seconds REAL);
          CREATE TABLE IF NOT EXISTS postprocessings (
            job_id INTEGER PRIMARY KEY REFERENCES jobs(id), state TEXT NOT NULL DEFAULT 'pending',
            stage TEXT, mode TEXT, source_path TEXT, source_sha256 TEXT, srt_sha256 TEXT,
            output_dir TEXT, manifest_path TEXT, manifest_sha256 TEXT,
            download_attempts INTEGER NOT NULL DEFAULT 0, attempts INTEGER NOT NULL DEFAULT 0,
            save_attempts INTEGER NOT NULL DEFAULT 0, next_retry REAL NOT NULL DEFAULT 0,
            error TEXT, updated_at TEXT NOT NULL, completed_at TEXT,
            figures INTEGER, agent_seconds REAL, total_tokens INTEGER);
          CREATE TABLE IF NOT EXISTS artifacts (
            job_id INTEGER NOT NULL REFERENCES jobs(id), workflow TEXT NOT NULL,
            state TEXT NOT NULL DEFAULT 'pending', stage TEXT,
            source_path TEXT, input_sha256 TEXT, workflow_sha256 TEXT,
            output_dir TEXT, response_sha256 TEXT, manifest_sha256 TEXT,
            attempts INTEGER NOT NULL DEFAULT 0, save_attempts INTEGER NOT NULL DEFAULT 0,
            error TEXT, updated_at TEXT NOT NULL, completed_at TEXT,
            PRIMARY KEY(job_id,workflow));
        """)

        if "save_attempts" not in {
            r[1] for r in self.db.execute("PRAGMA table_info(diarizations)")
        }:
            self.db.execute(
                "ALTER TABLE diarizations ADD COLUMN save_attempts INTEGER NOT NULL DEFAULT 0"
            )
            self.db.commit()
        if "run_pid" not in {r[1] for r in self.db.execute("PRAGMA table_info(artifacts)")}:
            self.db.execute("ALTER TABLE artifacts ADD COLUMN run_pid INTEGER")
            self.db.commit()
        if "next_retry" not in {r[1] for r in self.db.execute("PRAGMA table_info(artifacts)")}:
            self.db.execute("ALTER TABLE artifacts ADD COLUMN next_retry REAL NOT NULL DEFAULT 0")
            self.db.commit()

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
        from .execution import assert_owned

        with self.db:
            assert_owned(self.db, self.root)
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
                raise ProfileChanged(
                    f"ASR model/settings changed for {profile}; use an explicit new --profile to upgrade"
                )
            self.db.execute("INSERT OR IGNORE INTO profiles VALUES(?,?)", (profile, identity))

    def retry(self, ids=None):
        sql = "UPDATE jobs SET state='pending',next_retry=0,download_attempts=0,asr_attempts=0,error=NULL,error_kind=NULL WHERE state='failed'"
        args = []
        if ids:
            sql += f" AND video_id IN ({','.join('?' for _ in ids)})"
            args = ids
        with self.db:
            count = self.db.execute(sql, args).rowcount
            sql = "UPDATE diarizations SET state='pending',next_retry=0,download_attempts=0,attempts=0,save_attempts=0,error=NULL WHERE state='failed'"
            if ids:
                sql += f" AND job_id IN (SELECT id FROM jobs WHERE video_id IN ({','.join('?' for _ in ids)}))"
            count += self.db.execute(sql, args).rowcount
            sql = "UPDATE postprocessings SET state='pending',next_retry=0,download_attempts=0,attempts=0,save_attempts=0,error=NULL WHERE state='failed'"
            if ids:
                sql += f" AND job_id IN (SELECT id FROM jobs WHERE video_id IN ({','.join('?' for _ in ids)}))"
            count += self.db.execute(sql, args).rowcount
            sql = "UPDATE artifacts SET state='pending',next_retry=0,attempts=0,save_attempts=0,error=NULL WHERE state='failed'"
            if ids:
                sql += f" AND job_id IN (SELECT id FROM jobs WHERE video_id IN ({','.join('?' for _ in ids)}))"
            return count + self.db.execute(sql, args).rowcount

    def pipeline_jobs(self, *, profile="turbo-v1", ids=None, channel=None):
        conditions, args = ["j.profile=?"], [profile]
        if ids:
            conditions.append(f"v.id IN ({','.join('?' for _ in ids)})")
            args.extend(ids)
        if channel:
            conditions.append("v.channel_id=?")
            args.append(channel)
        columns = [
            "j.*",
            "v.title",
            "v.channel_id",
            "v.url",
            "v.kind",
            "v.duration",
            "v.published_at",
        ]
        for stage, alias in zip(STAGES[1:], ("d", "p", "a")):
            for field in ("state", "stage", "next_retry", *stage.counters):
                columns.append(f"{alias}.{field} AS {stage.name}_{field}")
        return [
            dict(r)
            for r in self.db.execute(
                f"SELECT {','.join(columns)} FROM jobs j JOIN videos v ON j.video_id=v.id LEFT JOIN diarizations d ON d.job_id=j.id LEFT JOIN postprocessings p ON p.job_id=j.id LEFT JOIN artifacts a ON a.job_id=j.id AND a.workflow='summary' WHERE {' AND '.join(conditions)} ORDER BY COALESCE(v.published_at,'') DESC,j.id",
                args,
            )
        ]

    def jobs(
        self,
        *,
        profile="turbo-v1",
        ids=None,
        channel=None,
        max_attempts=3,
        require_diarization=False,
        require_postprocessing=False,
        require_summary=False,
    ):
        plan = Plan(require_diarization, require_postprocessing, require_summary)
        return [
            r
            for r in self.pipeline_jobs(profile=profile, ids=ids, channel=channel)
            if (due := plan.due(r, max_attempts)) is not None and due <= time.time()
        ]

    def next_due(self, plan, *, max_attempts=3, **scope):
        return min(
            (
                due
                for r in self.pipeline_jobs(**scope)
                if (due := plan.due(r, max_attempts)) is not None
            ),
            default=None,
        )

    def completed(self, plan, **scope):
        rows = self.pipeline_jobs(**scope)
        if scope.get("ids") and set(scope["ids"]) != {r["video_id"] for r in rows}:
            raise ValueError("One or more requested video IDs are not indexed for this profile")
        return sum(plan.complete(r) for r in rows)

    def stage_update(self, stage_name, job_id, *, workflow=None, **values):
        tables = {
            "diarization": "diarizations",
            "postprocessing": "postprocessings",
            "artifact": "artifacts",
        }
        table = tables[stage_name]
        columns = {r[1] for r in self.db.execute(f"PRAGMA table_info({table})")}
        if not set(values).issubset(columns - {"job_id", "workflow"}):
            raise ValueError("Unknown stage field")
        from .execution import assert_owned

        with self.db:
            assert_owned(self.db, self.root)
            values["updated_at"] = now()
            self.db.execute(
                f"UPDATE {table} SET {','.join(k + '=?' for k in values)} WHERE job_id=?"
                + (" AND workflow=?" if workflow is not None else ""),
                (*values.values(), job_id, *([workflow] if workflow is not None else [])),
            )

    def transcript(self, job_id):
        return self.db.execute("SELECT * FROM transcripts WHERE job_id=?", (job_id,)).fetchone()

    def stage_row(self, stage_name, job_id, *, workflow=None):
        table = {
            "diarization": "diarizations",
            "postprocessing": "postprocessings",
            "artifact": "artifacts",
        }[stage_name]
        row = self.db.execute(
            f"SELECT * FROM {table} WHERE job_id=?" + (" AND workflow=?" if workflow else ""),
            (job_id, *([workflow] if workflow else [])),
        ).fetchone()
        return dict(row) if row else None

    def ensure_stage(self, stage_name, job_id, *, workflow=None, mode=None):
        from .execution import assert_owned

        table = {
            "diarization": "diarizations",
            "postprocessing": "postprocessings",
            "artifact": "artifacts",
        }[stage_name]
        values = {"job_id": job_id, "updated_at": now()}
        if workflow:
            values["workflow"] = workflow
        if mode:
            values["mode"] = mode
        with self.db:
            assert_owned(self.db, self.root)
            self.db.execute(
                f"INSERT OR IGNORE INTO {table}({','.join(values)}) VALUES({','.join('?' for _ in values)})",
                tuple(values.values()),
            )
        return self.stage_row(stage_name, job_id, workflow=workflow)

    def artifact_source(self, job_id):
        return self.db.execute(
            "SELECT p.*,v.title,v.url,j.video_id,j.profile,v.channel_id FROM postprocessings p JOIN jobs j ON j.id=p.job_id JOIN videos v ON v.id=j.video_id WHERE p.job_id=? AND p.state='complete'",
            (job_id,),
        ).fetchone()

    def update_video(self, job, metadata):
        from .execution import assert_owned

        with self.db:
            assert_owned(self.db, self.root)
            self.db.execute(
                "UPDATE videos SET title=?,duration=?,published_at=? WHERE id=?",
                (
                    job["title"],
                    metadata.get("duration"),
                    str(metadata.get("timestamp") or metadata.get("upload_date") or ""),
                    job["video_id"],
                ),
            )

    def bind_diarization_profile(self, profile, health):
        keys = (
            "model",
            "revision",
            "backend",
            "backend_version",
            "torch_version",
            "compute_type",
            "options",
            "exclusive_policy",
        )
        identity = json.dumps({key: health.get(key) for key in keys}, sort_keys=True)
        from .execution import assert_owned

        with self.db:
            assert_owned(self.db, self.root)
            existing = self.db.execute(
                "SELECT identity FROM diarization_profiles WHERE profile=?", (profile,)
            ).fetchone()
            if existing and existing[0] != identity:
                raise ProfileChanged(
                    "Diarization model/settings changed; use an explicit new profile"
                )
            self.db.execute(
                "INSERT OR IGNORE INTO diarization_profiles VALUES(?,?)", (profile, identity)
            )

    def stage_records(self, stage_name, *, states=None):
        table = {
            "diarization": "diarizations",
            "postprocessing": "postprocessings",
            "artifact": "artifacts",
        }[stage_name]
        where = " WHERE s.state IN (" + ",".join("?" for _ in states) + ")" if states else ""
        return [
            dict(r)
            for r in self.db.execute(
                f"SELECT s.*,j.video_id FROM {table} s JOIN jobs j ON j.id=s.job_id" + where,
                states or [],
            )
        ]

    def reset_interrupted(self, stage_name=None):
        from .execution import assert_owned

        tables = {
            "asr": "jobs",
            "diarization": "diarizations",
            "postprocessing": "postprocessings",
            "artifact": "artifacts",
        }
        with self.db:
            assert_owned(self.db, self.root)
            for stage, table in tables.items():
                if stage_name is not None and stage_name != stage:
                    continue
                values = "state='pending'"
                if stage == "asr":
                    values += ",download_state=CASE WHEN download_state='active' THEN 'pending' ELSE download_state END,asr_state=CASE WHEN asr_state='active' THEN 'pending' ELSE asr_state END"
                if stage == "artifact":
                    values += ",run_pid=NULL"
                key = "id" if stage == "asr" else "job_id"
                self.db.execute(
                    f"UPDATE {table} SET {values} WHERE state='active' AND NOT EXISTS(SELECT 1 FROM claims WHERE resource='job:'||{table}.{key} AND expires>?)",
                    (time.time(),),
                )

    def stage_inputs(self, completed_stage, *, order="published", **scope):
        plans = {
            "asr": Plan(False, False, False),
            "diarization": Plan(True, False, False),
            "postprocessing": Plan(True, True, False),
        }
        jobs = [r for r in self.pipeline_jobs(**scope) if plans[completed_stage].complete(r)]
        return sorted(jobs, key=lambda r: r["id"]) if order == "id" else jobs
