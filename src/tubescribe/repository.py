"""SQL job state and selection; content lives in the artifact store."""

from __future__ import annotations

import json
import sqlite3
import time
from datetime import UTC, datetime
from pathlib import Path

from .database import clock as database_clock
from .database import columns, connect, database_errors, network_database, serialize
from .pipeline import STAGES, Plan


class ProfileChanged(RuntimeError):
    """An archive profile cannot silently switch model/settings."""


def now():
    return datetime.now(UTC).isoformat()


class SQLJobRepository:
    def __init__(self, root: Path, *, database=None):
        from .config import bind_database, database_path
        from .migrations import open_runtime

        self.root = root.resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self.database = bind_database(self.root, database or database_path(self.root))
        if not network_database():
            self.database.parent.mkdir(parents=True, exist_ok=True)
        self.db = open_runtime(self.root)

    def channels(self, channel_id=None):
        return [
            dict(row)
            for row in self.db.execute(
                "SELECT * FROM channels" + (" WHERE id=?" if channel_id else ""),
                (channel_id,) if channel_id else (),
            )
        ]

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
                    "INSERT INTO jobs(video_id,profile,updated_at) VALUES(?,?,?) ON CONFLICT DO NOTHING",
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
            if getattr(self.db, "dialect", None) == "postgres":
                serialize(self.db, "profile:" + profile)
            existing = self.db.execute(
                "SELECT identity FROM profiles WHERE name=?", (profile,)
            ).fetchone()
            if existing and existing[0] != identity:
                raise ProfileChanged(
                    f"ASR model/settings changed for {profile}; use an explicit new --profile to upgrade"
                )
            self.db.execute(
                "INSERT INTO profiles VALUES(?,?) ON CONFLICT DO NOTHING", (profile, identity)
            )

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
        stamp = database_clock(self.db)
        return [
            r
            for r in self.pipeline_jobs(profile=profile, ids=ids, channel=channel)
            if (due := plan.due(r, max_attempts)) is not None and due <= stamp
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
        fields = set(columns(self.db, table))
        if not set(values).issubset(fields - {"job_id", "workflow"}):
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

        if "state" in values or "stage" in values:
            self.record_event(
                None,
                "stage_transition",
                {
                    "job_id": job_id,
                    "stage": stage_name,
                    "workflow": workflow,
                    "state": values.get("state"),
                    "step": values.get("stage"),
                },
            )

    def status_data(self):
        return {
            "jobs": [
                dict(r)
                for r in self.db.execute(
                    "SELECT profile,state,download_state,asr_state,count(*) AS count FROM jobs GROUP BY profile,state,download_state,asr_state"
                )
            ],
            "discovery": [dict(r) for r in self.db.execute("SELECT * FROM discovery")],
            "transcripts": dict(
                self.db.execute(
                    "SELECT COUNT(*) AS count, SUM(duration) AS audio_seconds,SUM(processing_seconds) AS asr_seconds FROM transcripts"
                ).fetchone()
            ),
            "diarization": [
                dict(r)
                for r in self.db.execute(
                    "SELECT j.profile,d.state,d.stage,COUNT(*) AS count FROM diarizations d JOIN jobs j ON j.id=d.job_id GROUP BY j.profile,d.state,d.stage"
                )
            ],
            "completed_jobs": self.db.execute(
                "SELECT COUNT(*) FROM jobs j JOIN diarizations d ON d.job_id=j.id JOIN postprocessings p ON p.job_id=j.id JOIN artifacts a ON a.job_id=j.id AND a.workflow='summary' WHERE j.state='complete' AND d.state='complete' AND p.state='complete' AND a.state='complete'"
            ).fetchone()[0],
            "awaiting_diarization": self.db.execute(
                "SELECT COUNT(*) FROM jobs j LEFT JOIN diarizations d ON d.job_id=j.id WHERE j.state='complete' AND COALESCE(d.state,'pending')!='complete'"
            ).fetchone()[0],
            "postprocessing": [
                dict(r)
                for r in self.db.execute(
                    "SELECT j.profile,p.state,p.stage,COUNT(*) AS count FROM postprocessings p JOIN jobs j ON j.id=p.job_id GROUP BY j.profile,p.state,p.stage"
                )
            ],
            "awaiting_postprocessing": self.db.execute(
                "SELECT COUNT(*) FROM jobs j JOIN diarizations d ON d.job_id=j.id LEFT JOIN postprocessings p ON p.job_id=j.id WHERE j.state='complete' AND d.state='complete' AND COALESCE(p.state,'pending')!='complete'"
            ).fetchone()[0],
            "artifacts": [
                dict(r)
                for r in self.db.execute(
                    "SELECT j.profile,a.workflow,a.state,a.stage,COUNT(*) AS count FROM artifacts a JOIN jobs j ON j.id=a.job_id GROUP BY j.profile,a.workflow,a.state,a.stage"
                )
            ],
            "awaiting_summary": self.db.execute(
                "SELECT COUNT(*) FROM jobs j JOIN postprocessings p ON p.job_id=j.id LEFT JOIN artifacts a ON a.job_id=j.id AND a.workflow='summary' WHERE p.state='complete' AND COALESCE(a.state,'pending')!='complete'"
            ).fetchone()[0],
        }

    def failures(self):
        return [
            [dict(row) for row in self.db.execute(query)]
            for query in (
                "SELECT j.video_id,j.profile,a.workflow,a.stage AS failed_stage,a.error,a.attempts,a.save_attempts,a.next_retry FROM artifacts a JOIN jobs j ON j.id=a.job_id WHERE a.state='failed'",
                "SELECT video_id,profile,failed_stage,error_kind,error,download_attempts,asr_attempts,next_retry,audio_path FROM jobs WHERE state='failed'",
                "SELECT j.video_id,j.profile,p.stage AS failed_stage,p.error,p.download_attempts,p.attempts,p.save_attempts,p.next_retry FROM postprocessings p JOIN jobs j ON j.id=p.job_id WHERE p.state='failed'",
                "SELECT j.video_id,j.profile,d.stage AS failed_stage,d.error,d.download_attempts,d.attempts AS diarization_attempts,d.save_attempts,d.next_retry,d.audio_path FROM diarizations d JOIN jobs j ON j.id=d.job_id WHERE d.state='failed'",
            )
        ]

    def video_id(self, job_id):
        return self.db.execute("SELECT video_id FROM jobs WHERE id=?", (job_id,)).fetchone()[0]

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
                f"INSERT INTO {table}({','.join(values)}) VALUES({','.join('?' for _ in values)}) ON CONFLICT DO NOTHING",
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
            if getattr(self.db, "dialect", None) == "postgres":
                serialize(self.db, "diarization-profile:" + profile)
            existing = self.db.execute(
                "SELECT identity FROM diarization_profiles WHERE profile=?", (profile,)
            ).fetchone()
            if existing and existing[0] != identity:
                raise ProfileChanged(
                    "Diarization model/settings changed; use an explicit new profile"
                )
            self.db.execute(
                "INSERT INTO diarization_profiles VALUES(?,?) ON CONFLICT DO NOTHING",
                (profile, identity),
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
                    (database_clock(self.db),),
                )

    def stage_inputs(self, completed_stage, *, order="published", **scope):
        plans = {
            "asr": Plan(False, False, False),
            "diarization": Plan(True, False, False),
            "postprocessing": Plan(True, True, False),
        }
        jobs = [r for r in self.pipeline_jobs(**scope) if plans[completed_stage].complete(r)]
        return sorted(jobs, key=lambda r: r["id"]) if order == "id" else jobs

    def submission(self, identifier):
        row = self.db.execute(
            "SELECT s.*,r.result FROM submissions s LEFT JOIN submission_results r ON r.id=s.id WHERE s.id=?",
            (identifier,),
        ).fetchone()
        return dict(row) if row else None

    def enqueue(self, request, dedupe):
        import uuid

        stamp = database_clock(self.db)
        with self.db:
            serialize(self.db, "submission:" + dedupe)
            row = self.db.execute(
                "SELECT * FROM submissions WHERE dedupe=? AND state IN ('queued','active')",
                (dedupe,),
            ).fetchone()
            if row:
                owned = self.db.execute(
                    "SELECT 1 FROM claims WHERE resource=? AND expires>?",
                    ("submission:" + row["id"], stamp),
                ).fetchone()
                # A queued request remains durable until consumed, regardless of age.
                if (
                    row["state"] == "queued"
                    and row["pid"] is None
                    or owned
                    or row["updated"] > stamp - 90
                ):
                    return row["id"], False
                self.db.execute(
                    "UPDATE submissions SET state='interrupted',updated=? WHERE id=?",
                    (stamp, row["id"]),
                )
            identifier = uuid.uuid4().hex
            self.db.execute(
                "INSERT INTO submissions(id,dedupe,request,state,pid,error,created,updated) VALUES(?,?,?,?,?,?,?,?)",
                (identifier, dedupe, request, "queued", None, None, stamp, stamp),
            )
        self.record_event(identifier, "submission_queued", {})
        return identifier, True

    def update_submission(self, identifier, **values):
        if not set(values) <= {"state", "pid", "error", "result", "worker_id"}:
            raise ValueError("Unknown submission field")
        with self.db:
            from .execution import assert_owned

            assert_owned(self.db, self.root)
            if "result" in values:
                self.db.execute(
                    "INSERT INTO submission_results VALUES(?,?) ON CONFLICT(id) DO UPDATE SET result=excluded.result",
                    (identifier, values.pop("result")),
                )
            values["updated"] = database_clock(self.db)
            self.db.execute(
                "UPDATE submissions SET " + ",".join(k + "=?" for k in values) + " WHERE id=?",
                (*values.values(), identifier),
            )

        self.record_event(identifier, "submission_" + values.get("state", "dispatched"), {})

    def submission_records(self):
        return [
            dict(r)
            for r in self.db.execute(
                "SELECT id,state,pid,error,created,updated FROM submissions ORDER BY created DESC LIMIT 100"
            )
        ]

    def queued_submissions(self):
        return [
            dict(r)
            for r in self.db.execute(
                "SELECT * FROM submissions WHERE state='queued' AND pid IS NULL ORDER BY created"
            )
        ]

    def reclaim_submissions(self):
        stamp = database_clock(self.db)
        with self.db:
            result = self.db.execute(
                "UPDATE submissions SET state='queued',pid=NULL,worker_id=NULL,updated=? WHERE state IN ('active','queued') AND (state='active' OR pid IS NOT NULL) AND updated<? AND NOT EXISTS (SELECT 1 FROM claims WHERE resource='submission:' || submissions.id AND expires>?)",
                (stamp, stamp - 90, stamp),
            )
        return result.rowcount

    def owned(self, resource):
        return (
            self.db.execute(
                "SELECT 1 FROM claims WHERE resource=? AND expires>?",
                (resource, database_clock(self.db)),
            ).fetchone()
            is not None
        )

    def pending_agent_runs(self):
        return self.db.execute(
            "SELECT COUNT(*) FROM agent_runs WHERE state IN ('queued','active')"
        ).fetchone()[0]

    def record_event(self, run_id, event, data):
        from .execution import CURRENT_SUBMISSION

        run_id = run_id or CURRENT_SUBMISSION.get()
        with self.db:
            self.db.execute(
                "INSERT INTO runtime_events(run_id,event,created,data) VALUES(?,?,?,?)",
                (run_id, event, database_clock(self.db), json.dumps(data, default=str)),
            )

    def begin_attempt(self, job_id, stage, workflow=None, run_id=None):
        import uuid

        from .execution import CURRENT_SUBMISSION

        run_id = run_id or CURRENT_SUBMISSION.get()
        identifier = uuid.uuid4().hex
        with self.db:
            self.db.execute(
                "INSERT INTO attempts(id,job_id,stage,workflow,run_id,state,started) VALUES(?,?,?,?,?,?,?)",
                (identifier, job_id, stage, workflow, run_id, "active", database_clock(self.db)),
            )
        return identifier

    def finish_attempt(self, identifier, state, *, metrics=None, error=None, run_id=None):
        stamp = database_clock(self.db)
        with self.db:
            self.db.execute(
                "UPDATE attempts SET state=?,finished=?,elapsed=?-started,metrics=?,error=?,run_id=COALESCE(run_id,?) WHERE id=?",
                (state, stamp, stamp, json.dumps(metrics or {}), error, run_id, identifier),
            )

    def reconcile_attempts(self):
        with self.db:
            result = self.db.execute(
                "UPDATE attempts SET state='interrupted',finished=?,error='Owner expired' WHERE state='active' AND job_id IS NOT NULL AND NOT EXISTS(SELECT 1 FROM claims WHERE resource='job:' || attempts.job_id AND expires>?)",
                (database_clock(self.db), database_clock(self.db)),
            )
        return result.rowcount

    def transcript_records(self, *, ids=None, profile=None, job_id=None):
        conditions = ["j.state='complete'"]
        values = []
        if job_id is not None:
            conditions.append("j.id=?")
            values.append(job_id)
        if ids:
            conditions.append("j.video_id IN (" + ",".join("?" for _ in ids) + ")")
            values.extend(ids)
        if profile:
            conditions.append("j.profile=?")
            values.append(profile)
        return self.db.execute(
            "SELECT j.id,t.*,j.video_id FROM jobs j JOIN transcripts t ON j.id=t.job_id WHERE "
            + " AND ".join(conditions)
            + " ORDER BY j.id",
            values,
        ).fetchall()

    def commit_transcript(self, record):
        from .execution import assert_owned

        with self.db:
            assert_owned(self.db, self.root)
            self.db.execute(
                "INSERT INTO transcripts VALUES("
                + ",".join("?" for _ in record)
                + ") ON CONFLICT(job_id) DO UPDATE SET "
                + ",".join(
                    name + "=excluded." + name
                    for name in columns(self.db, "transcripts")
                    if name != "job_id"
                ),
                record,
            )
            self.db.execute(
                "UPDATE jobs SET state='complete',asr_state='complete',completed_at=?,updated_at=?,error=NULL,error_kind=NULL,failed_stage=NULL WHERE id=?",
                (now(), now(), record[0]),
            )


class SQLiteJobRepository(SQLJobRepository):
    """Default embedded database adapter."""


class PostgreSQLJobRepository(SQLJobRepository):
    """Network database adapter with atomic claims and queue reservation."""

    def claim_submission(self, worker_id):
        with self.db:
            row = self.db.execute(
                "SELECT id FROM submissions WHERE state='queued' AND worker_id IS NULL ORDER BY created FOR UPDATE SKIP LOCKED LIMIT 1"
            ).fetchone()
            if not row:
                return None
            self.db.execute(
                "UPDATE submissions SET state='active',worker_id=?,updated=? WHERE id=?",
                (worker_id, database_clock(self.db), row[0]),
            )
            return self.submission(row[0])


def JobRepository(root, **kwargs):
    return (PostgreSQLJobRepository if network_database() else SQLiteJobRepository)(root, **kwargs)


class SQLClaims:
    """Database-backed atomic ownership; independent of worker scratch paths."""

    def __init__(self, root):
        from .migrations import open_runtime

        self.db = open_runtime(root)

    def acquire(self, resource, owner, *, ttl=90, clock=None):
        stamp = time.time() if clock is None else clock
        with self.db:
            if network_database():
                serialize(self.db, "claim-coordination")
                if clock is None:
                    stamp = self.db.execute(
                        "SELECT EXTRACT(EPOCH FROM clock_timestamp())"
                    ).fetchone()[0]
                    stamp = float(stamp)
                if (
                    resource == "archive"
                    and self.db.execute(
                        "SELECT 1 FROM claims WHERE resource LIKE 'worker:%' AND expires>? LIMIT 1",
                        (stamp,),
                    ).fetchone()
                ):
                    return False
                if (
                    resource.startswith("worker:")
                    and self.db.execute(
                        "SELECT 1 FROM claims WHERE resource='archive' AND expires>?", (stamp,)
                    ).fetchone()
                ):
                    return False
            r = self.db.execute(
                "INSERT INTO claims VALUES(?,?,?) ON CONFLICT(resource) DO UPDATE SET owner=excluded.owner,expires=excluded.expires WHERE claims.expires<=? OR claims.owner=?",
                (resource, owner, stamp + ttl, stamp, owner),
            )
        return r.rowcount == 1

    def renew(self, resource, owner, *, ttl=90, clock=None):
        stamp = time.time() if clock is None else clock
        with self.db:
            if network_database() and clock is None:
                stamp = float(
                    self.db.execute("SELECT EXTRACT(EPOCH FROM clock_timestamp())").fetchone()[0]
                )
            return (
                self.db.execute(
                    "UPDATE claims SET expires=? WHERE resource=? AND owner=? AND expires>?",
                    (stamp + ttl, resource, owner, stamp),
                ).rowcount
                == 1
            )

    def release(self, resource, owner):
        with self.db:
            self.db.execute("DELETE FROM claims WHERE resource=? AND owner=?", (resource, owner))

    @staticmethod
    def fence(db, resource, owner):
        if getattr(db, "dialect", "sqlite") == "postgres":
            return (
                db.execute(
                    "UPDATE claims SET owner=owner WHERE resource=? AND owner=? AND expires>EXTRACT(EPOCH FROM clock_timestamp())",
                    (resource, owner),
                ).rowcount
                == 1
            )
        return (
            db.execute(
                "UPDATE claims SET owner=owner WHERE resource=? AND owner=? AND expires>?",
                (resource, owner, database_clock(db)),
            ).rowcount
            == 1
        )

    def close(self):
        self.db.close()


class ArchiveViews:
    """Read models for HTTP delivery, keeping SQL and connections out of handlers."""

    def __init__(self, root):
        from .config import database_path

        self.database = database_path(root)
        self.root = root

    def _one(self, query, values):
        from contextlib import closing

        with closing(connect(self.root, readonly=True)) as db:
            return db.execute(query, values).fetchone()

    def owned(self, resource):
        try:
            from contextlib import closing

            with closing(connect(self.root, readonly=True)) as db:
                return (
                    db.execute(
                        "SELECT 1 FROM claims WHERE resource=? AND expires>?",
                        (resource, database_clock(db)),
                    ).fetchone()
                    is not None
                )
        except database_errors():
            return False

    def workers_running(self):
        from contextlib import closing

        with closing(connect(self.root, readonly=True)) as db:
            return (
                db.execute(
                    "SELECT 1 FROM claims WHERE resource LIKE 'worker:%' AND expires>? LIMIT 1",
                    (database_clock(db),),
                ).fetchone()
                is not None
            )

    def events(self):
        from contextlib import closing
        from datetime import UTC, datetime

        try:
            with closing(connect(self.root, readonly=True)) as db:
                return [
                    {
                        **json.loads(data),
                        "event": event,
                        "run_id": run_id,
                        "time": datetime.fromtimestamp(stamp, UTC).isoformat(),
                    }
                    for run_id, event, stamp, data in db.execute(
                        "SELECT run_id,event,created,data FROM runtime_events ORDER BY id DESC LIMIT 20"
                    )
                ]
        except database_errors():
            return []

    def resource_transcript(self, *, video_id=None, profile=None, job_id=None):
        from contextlib import closing

        with closing(connect(self.root, readonly=True)) as db:
            db.row_factory = sqlite3.Row
            condition, values = (
                ("j.id=?", (job_id,))
                if job_id is not None
                else ("j.video_id=? AND j.profile=?", (video_id, profile))
            )
            row = db.execute(
                "SELECT j.id AS job_id,j.video_id,j.profile,j.asr_state,v.title,v.url,t.json_path,d.state AS diarization_state,p.state AS postprocessing_state FROM jobs j JOIN videos v ON v.id=j.video_id LEFT JOIN transcripts t ON t.job_id=j.id LEFT JOIN diarizations d ON d.job_id=j.id LEFT JOIN postprocessings p ON p.job_id=j.id WHERE "
                + condition,
                values,
            ).fetchone()
            return dict(row) if row else None

    def resource_video(self, video_id):
        row = self._one(
            "SELECT id,channel_id,title,url,kind,duration,published_at FROM videos WHERE id=?",
            (video_id,),
        )
        return (
            dict(zip(("id", "channel_id", "title", "url", "kind", "duration", "published_at"), row))
            if row
            else None
        )

    def resource_profiles(self, video_id):
        from contextlib import closing

        with closing(connect(self.root, readonly=True)) as db:
            return [
                {"profile": r[0]}
                for r in db.execute(
                    "SELECT profile FROM jobs WHERE video_id=? ORDER BY profile", (video_id,)
                )
            ]

    def resource_artifacts(self, job_id):
        from contextlib import closing

        with closing(connect(self.root, readonly=True)) as db:
            return [
                {"workflow": r[0], "state": r[1]}
                for r in db.execute(
                    "SELECT workflow,state FROM artifacts WHERE job_id=? ORDER BY workflow",
                    (job_id,),
                )
            ]

    def artifact(self, job_id, workflow):
        return self._one(
            "SELECT v.title,p.state,a.state,a.output_dir,a.error,a.run_pid FROM jobs j JOIN videos v ON v.id=j.video_id JOIN postprocessings p ON p.job_id=j.id LEFT JOIN artifacts a ON a.job_id=j.id AND a.workflow=? WHERE j.id=?",
            (workflow, job_id),
        )

    def srt(self, job_id):
        return self._one(
            "SELECT t.json_path,v.title,v.id,p.state FROM transcripts t JOIN jobs j ON j.id=t.job_id JOIN videos v ON v.id=j.video_id LEFT JOIN postprocessings p ON p.job_id=j.id WHERE j.id=?",
            (job_id,),
        )

    def processed(self, job_id):
        return self._one(
            "SELECT p.output_dir,p.source_path,v.title FROM postprocessings p JOIN jobs j ON j.id=p.job_id JOIN videos v ON v.id=j.video_id WHERE p.job_id=? AND p.state='complete'",
            (job_id,),
        )

    def snapshot(self):
        import contextlib

        with contextlib.closing(connect(self.root, readonly=True)) as db:
            db.row_factory = sqlite3.Row
            db.execute("BEGIN")
            channels = [dict(r) for r in db.execute("SELECT * FROM channels ORDER BY title")]
            discovery = [dict(r) for r in db.execute("SELECT * FROM discovery")]
            profiles = [dict(r) for r in db.execute("SELECT * FROM profiles")]
            jobs = [
                dict(r)
                for r in db.execute("""
                    SELECT v.id AS video_id,v.channel_id,v.title,v.url,v.kind,v.duration,
                           v.published_at,v.discovered_at,j.id AS job_id,j.profile,j.state,
                           j.download_state,j.asr_state,j.download_attempts,j.asr_attempts,
                           j.next_retry,j.failed_stage,j.error_kind,j.error,j.audio_path,
                           j.updated_at,j.completed_at,t.json_path,t.text_path,t.model,t.revision,
                           t.language,t.processing_seconds,t.saved_at,
                           d.state AS diarization_state,d.stage AS diarization_stage,
                           d.error AS diarization_error,d.next_retry AS diarization_next_retry,d.updated_at AS diarization_updated_at,
                           d.attempts AS diarization_attempts,d.speaker_count,
                           d.processing_seconds AS diarization_seconds,d.asr_path,
                           d.prediction_path,d.audio_path AS diarization_audio_path
                           ,p.state AS postprocessing_state,p.stage AS postprocessing_stage,
                           p.error AS postprocessing_error,p.next_retry AS postprocessing_next_retry,
                           p.updated_at AS postprocessing_updated_at,p.attempts AS postprocessing_attempts,
                           p.figures,p.agent_seconds,p.output_dir AS postprocessing_dir,
                           a.state AS summary_state,a.stage AS summary_stage,a.error AS summary_error,
                           a.next_retry AS summary_next_retry,a.updated_at AS summary_updated_at,
                           a.attempts AS summary_attempts,a.output_dir AS summary_dir
                    FROM videos v LEFT JOIN jobs j ON j.video_id=v.id
                    LEFT JOIN transcripts t ON t.job_id=j.id
                    LEFT JOIN diarizations d ON d.job_id=j.id
                    LEFT JOIN postprocessings p ON p.job_id=j.id
                    LEFT JOIN artifacts a ON a.job_id=j.id AND a.workflow='summary'
                    ORDER BY COALESCE(v.published_at,'') DESC,v.discovered_at DESC,v.id,j.id
                """)
            ]
            db.rollback()

        return channels, discovery, profiles, jobs
