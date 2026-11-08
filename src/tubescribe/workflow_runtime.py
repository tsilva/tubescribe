"""Isolated per-step scratch, authoritative selection, and inexpensive Atom discovery."""

import json
import os
import xml.etree.ElementTree as ET
from datetime import UTC, datetime, timedelta
from pathlib import Path
from tempfile import TemporaryDirectory

import httpx

from .config import RuntimeConfig
from .execution import lease
from .storage import Store
from .youtube import CHANNEL, ID

ATOM = "{http://www.w3.org/2005/Atom}"
YT = "{http://www.youtube.com/xml/schemas/2015}"
_FEED_RETRY_DELAY = timedelta(minutes=90)
_FEED_ROWS = (
    " FROM channels c LEFT JOIN discovery d ON d.channel_id=c.id AND d.kind='cloud-feed'"
)
_FEED_DUE = "COALESCE(d.state<>'failed' OR d.updated_at<=?, TRUE)"


def feed_channel(value):
    # Current YouTube Atom feeds omit the UC prefix in yt:channelId, while
    # catalogue/channel URLs use the canonical UC-prefixed identifier.
    value = value or ""
    canonical = value if CHANNEL.fullmatch(value) else "UC" + value
    return canonical if CHANNEL.fullmatch(canonical) else None


def enabled(env=None):
    env = os.environ if env is None else env
    return env.get("TUBESCRIBE_PROCESSING_BACKEND") == "workflow"


def validate_runtime(root):
    config = RuntimeConfig.load(root)
    if not (enabled() and config.network and config.s3_bucket and config.s3_endpoint
            and config.manual_queue and config.queued):
        raise ValueError("Workflows require PostgreSQL, S3, queued dispatch and manual admission")
    if os.environ.get("VERCEL_ENV") != "production":
        raise ValueError("Production processing is disabled outside the Production deployment")
    if not os.environ.get("VERCEL_DEPLOYMENT_ID"):
        raise ValueError("Vercel System Environment Variables are required; local fallback is disabled")


def _feed_cutoff():
    return (datetime.now(UTC) - _FEED_RETRY_DELAY).isoformat()


def due_feed_channels(store):
    # Apply the cooldown before the batch limit so failed feeds leave room for others.
    return [row[0] for row in store.db.execute(
        "SELECT c.id" + _FEED_ROWS + " WHERE " + _FEED_DUE
        + " ORDER BY COALESCE(d.updated_at,''),c.id LIMIT 32",
        (_feed_cutoff(),),
    )]


def discover(store, channel_id, *, request=None):
    if not CHANNEL.fullmatch(channel_id):
        raise ValueError("Unknown followed channel")
    with lease(store.root, "cloud-feed:" + channel_id):
        # Another poll may have failed this channel since the batch was selected.
        row = store.db.execute(
            "SELECT " + _FEED_DUE + _FEED_ROWS + " WHERE c.id=?",
            (_feed_cutoff(), channel_id),
        ).fetchone()
        if row is None:
            raise ValueError("Unknown followed channel")
        if not row[0]:
            return {"skipped": 1}
        store.discovery(channel_id, "cloud-feed", "active")
        try:
            response = (request or httpx.get)(
                "https://www.youtube.com/feeds/videos.xml", params={"channel_id": channel_id},
                timeout=20, follow_redirects=False,
            )
            response.raise_for_status()
            if len(response.content) > 1024 * 1024:
                raise ValueError("Channel feed exceeds the bounded size")
            feed = ET.fromstring(response.content)
            if feed_channel(feed.findtext(YT + "channelId")) != channel_id:
                raise ValueError("Channel feed identity mismatch")
            videos = []
            for entry in feed.findall(ATOM + "entry"):
                video_id = entry.findtext(YT + "videoId") or ""
                if not ID.fullmatch(video_id) or feed_channel(entry.findtext(YT + "channelId")) != channel_id:
                    raise ValueError("Feed entry identity mismatch")
                videos.append({"id": video_id, "title": entry.findtext(ATOM + "title") or video_id,
                               "published_at": entry.findtext(ATOM + "published"),
                               "url": "https://www.youtube.com/watch?v=" + video_id})
        except httpx.HTTPStatusError as error:
            store.discovery(channel_id, "cloud-feed", "failed",
                            error=f"Public feed returned HTTP {error.response.status_code}")
            raise
        except (httpx.RequestError, ValueError, ET.ParseError):
            store.discovery(channel_id, "cloud-feed", "failed", error="Public feed unavailable")
            raise
        new_videos = [video for video in videos if not store.db.execute(
            "SELECT 1 FROM videos WHERE id=?", (video["id"],)
        ).fetchone()]
        # profile=None is the catalogue-only path: it never creates a job.
        store.index(channel_id, "upload", new_videos, None)
        store.discovery(channel_id, "cloud-feed", "complete", count=len(new_videos))
        return {"discovered": len(new_videos)}


def canonical(store, identifier, run_id):
    key = "cloud/control/selections/" + identifier + ".json"
    if not store.exists(key):
        return None
    return json.loads(store.read_key(key))["run_id"] == run_id


def with_store(operation, identifier=None, run_id=None):
    with TemporaryDirectory(prefix="tubescribe-step-") as directory:
        root = Path(directory)
        validate_runtime(root)
        store = Store(root)
        try:
            if operation in {"advance", "fail"}:
                owner = canonical(store, identifier, run_id)
                if owner is None:
                    return {"done": False, "delay": 10}
                if not owner:
                    return {"done": True, "state": "superseded"}
            if operation == "advance":
                from .cloud_pipeline import advance

                return advance(store, identifier)
            if operation == "channels":
                return due_feed_channels(store)
            if operation == "discovery_owner":
                key = "cloud/control/discovery.json"
                if not store.exists(key):
                    return "pending"
                return "current" if json.loads(store.read_key(key))["run_id"] == identifier else "superseded"
            if operation == "rollups_schedule":
                from .rollups import schedule_cloud

                identifier, outcome = schedule_cloud(store)
                return {"scheduled": identifier is not None, "outcome": outcome}
            if operation == "discover":
                return discover(store, identifier)
            if operation == "fail":
                with lease(store.root, "submission:" + identifier):
                    row = store.submission(identifier)
                    if canonical(store, identifier, run_id) and row and row["state"] in {"queued", "active"}:
                        store.update_submission(identifier, state="failed", worker_id=None,
                                                error="Workflow stopped; saved results retained")
                return {"done": True}
            raise ValueError("Unknown Workflow operation")
        finally:
            store.db.close()
