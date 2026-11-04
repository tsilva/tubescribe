"""Transactional admission to the owner-selected OpenRouter queue."""

import json
import uuid
from contextlib import closing

from .config import OPENROUTER_PROFILE as PROFILE
from .config import OPENROUTER_PROFILES
from .database import clock, connect, serialize
from .repository import now
from .submission import JobRequest
from .youtube import ID

PREFIX = "manual-openrouter:"


def lane_filter(manual):
    selected = f"dedupe LIKE '{PREFIX}%'"
    if manual:
        return f'({selected} OR request LIKE \'{{"command": "rollups",%\')'
    return "NOT (" + selected + ")"


def request_for(video_id, profile=PROFILE):
    return JobRequest("work", {"video": [video_id], "profile": profile, "max_attempts": 1})


def authorize_execution(store, request, identifier):
    if request.command == "rollups":
        return
    if not store.config.manual_queue and request.options.get("profile") not in OPENROUTER_PROFILES:
        return
    ids = request.options.get("video") or []
    row = store.submission(identifier) if identifier else None
    if (
        not store.config.manual_queue
        or request.command != "work"
        or len(ids) != 1
        or not row
        or row["dedupe"] != PREFIX + ids[0]
        or request.encode() != request_for(ids[0], request.options.get("profile")).encode()
    ):
        raise ValueError("Select videos in the processing queue before starting OpenRouter work")


def change(root, action, ids):
    if action not in {"queue", "retry", "cancel"}:
        raise ValueError("Unknown queue action")
    if (
        not isinstance(ids, list)
        or not 1 <= len(ids) <= 50
        or any(not isinstance(value, str) or not ID.fullmatch(value) for value in ids)
    ):
        raise ValueError("Select between 1 and 50 known videos")
    ids = list(dict.fromkeys(ids))
    with closing(connect(root, must_exist=True)) as db, db:
        serialize(db, "manual-openrouter-queue")
        stamp = clock(db)
        changed = 0
        for video_id in ids:
            if not db.execute("SELECT 1 FROM videos WHERE id=?", (video_id,)).fetchone():
                raise ValueError("One of the selected videos is no longer in the catalogue")
            dedupe = PREFIX + video_id
            if action == "cancel":
                changed += db.execute(
                    "UPDATE submissions SET state='cancelled',updated=? "
                    "WHERE dedupe=? AND state='queued' AND pid IS NULL AND worker_id IS NULL",
                    (stamp, dedupe),
                ).rowcount
                db.execute(
                    "DELETE FROM claims WHERE resource IN (SELECT 'submission-retry:' || id "
                    "FROM submissions WHERE dedupe=? AND state='cancelled')", (dedupe,),
                )
                continue
            latest = db.execute(
                "SELECT state,request FROM submissions WHERE dedupe=? ORDER BY created DESC,id DESC LIMIT 1",
                (dedupe,),
            ).fetchone()
            state = latest[0] if latest else None
            previous_profile = json.loads(latest[1])["options"].get("profile") if latest else None
            profile = previous_profile if action == "retry" else PROFILE
            if action == "retry":
                if state not in {"failed", "interrupted"}:
                    continue
            elif state not in {None, "cancelled"} and not (
                state == "complete" and previous_profile != PROFILE
            ):
                continue
            if profile not in OPENROUTER_PROFILES:
                raise ValueError("Unsupported saved processing profile")
            if (
                action == "queue"
                and db.execute(
                    "SELECT 1 FROM jobs JOIN transcripts ON transcripts.job_id=jobs.id "
                    "WHERE video_id=? AND profile=?",
                    (video_id, profile),
                ).fetchone()
            ):
                continue
            db.execute(
                "INSERT INTO jobs(video_id,profile,updated_at) VALUES(?,?,?) ON CONFLICT DO NOTHING",
                (video_id, profile, now()),
            )
            if action == "retry":
                job_id = db.execute(
                    "SELECT id FROM jobs WHERE video_id=? AND profile=?", (video_id, profile)
                ).fetchone()[0]
                db.execute(
                    "UPDATE jobs SET state=CASE WHEN state='failed' THEN 'pending' ELSE state END,"
                    "next_retry=0,download_attempts=0,asr_attempts=0,error=NULL,error_kind=NULL "
                    "WHERE id=? AND state IN ('failed','active')",
                    (job_id,),
                )
                for table, counters in (
                    ("postprocessings", "download_attempts=0,attempts=0,save_attempts=0"),
                    ("artifacts", "attempts=0,save_attempts=0"),
                ):
                    db.execute(
                        f"UPDATE {table} SET state=CASE WHEN state='failed' THEN 'pending' ELSE state END,"
                        f"next_retry=0,error=NULL,{counters} WHERE job_id=? AND state IN ('failed','active')",
                        (job_id,),
                    )
            db.execute(
                "INSERT INTO submissions(id,dedupe,request,state,created,updated) VALUES(?,?,?,?,?,?)",
                (uuid.uuid4().hex, dedupe, request_for(video_id, profile).encode(), "queued", stamp, stamp),
            )
            changed += 1
    return {"changed": changed}


def status(root):
    with closing(connect(root, readonly=True)) as db:
        return [
            {
                "video_id": row[0][len(PREFIX) :],
                "state": row[1],
                "cost": json.loads(row[2] or "{}").get("cost"),
                **({"retry_at": row[3]} if row[3] and row[1] == "queued" else {}),
            }
            for row in db.execute(
                "SELECT s.dedupe,s.state,(SELECT a.metrics FROM attempts a WHERE a.job_id=j.id "
                "AND a.stage='asr' AND a.state='complete' ORDER BY a.started DESC LIMIT 1),c.expires "
                "FROM (SELECT id,dedupe,state,request,ROW_NUMBER() OVER (PARTITION BY dedupe ORDER BY created DESC,id DESC) AS ordinal "
                "FROM submissions WHERE dedupe LIKE ?) s "
                "LEFT JOIN jobs j ON j.video_id=SUBSTR(s.dedupe,?) "
                "AND s.request LIKE '%\"profile\": \"' || j.profile || '\"%' "
                "LEFT JOIN claims c ON c.resource='submission-retry:' || s.id WHERE s.ordinal=1",
                (PREFIX + "%", len(PREFIX) + 1),
            )
        ]
