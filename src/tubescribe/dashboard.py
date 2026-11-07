"""Read-only, loopback-only archive monitoring; inference stays in the worker."""

from __future__ import annotations

import fcntl
import gzip
import hashlib
import html
import json
import os
import re
import signal
import subprocess
import sys
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import parse_qs, quote, urlsplit

import httpx

from .config import RuntimeConfig, database_path
from .database import database_errors
from .pipeline import status as stage
from .repository import ArchiveViews
from .resources import artifact_url, transcript_url
from .storage import atomic_write, now
from .transfer import operation

ASSETS = Path(__file__).with_name("dashboard_assets")
CLERK_BROWSER_SCRIPTS = (
    "/npm/@clerk/ui@1.40.0/dist/ui.browser.js",
    "/npm/@clerk/clerk-js@6.39.0/dist/clerk.browser.js",
)
STATIC_ASSETS = {
    "/": "index.html",
    "/app.js": "app.js",
    "/realtime.js": "realtime.js",
    "/style.css": "style.css",
    "/favicon.ico": "brand/favicon.ico",
    **{
        f"/brand/{name}": f"brand/{name}"
        for name in (
            "logo.png",
            "favicon.ico",
            "favicon-16.png",
            "favicon-32.png",
            "favicon-48.png",
            "apple-touch-icon.png",
            "android-chrome-192.png",
            "android-chrome-512.png",
            "site.webmanifest",
        )
    },
    **{
        f"/icons/{name}.svg": f"icons/{name}.svg"
        for name in (
            "magnifying-glass",
            "document-text",
            "clipboard-document",
            "arrow-top-right-on-square",
            "arrow-down-tray",
            "chevron-up",
            "x-mark",
            "arrow-path",
        )
    },
}
TEXT_FORMATS = {".json", ".txt", ".srt", ".md"}
AUDIO_FORMATS = {".mp4", ".m4a", ".mp3", ".webm", ".wav", ".ogg", ".opus", ".flac"}
CONTENT_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".js": "text/javascript; charset=utf-8",
    ".json": "application/json; charset=utf-8",
    ".mp4": "audio/mp4",
    ".m4a": "audio/mp4",
    ".mp3": "audio/mpeg",
    ".webm": "audio/webm",
    ".wav": "audio/wav",
    ".png": "image/png",
    ".ico": "image/vnd.microsoft.icon",
    ".webmanifest": "application/manifest+json",
    ".zip": "application/zip",
    ".md": "text/markdown; charset=utf-8",
    ".svg": "image/svg+xml",
}


def thumbnail_url(video_id: str) -> str:
    """Derive previews for both existing and newly discovered videos without processing."""
    return "https://i.ytimg.com/vi/" + quote(video_id, safe="") + "/hqdefault.jpg"


def lock_status(root: Path, name: str) -> dict:
    if name == "worker.lock" and (
        RuntimeConfig.load(root).network or database_path(root).is_file()
    ):
        views = ArchiveViews(root)
        if RuntimeConfig.load(root).network:
            return {"running": views.workers_running(), "pid": None}
        owned = views.owned("archive")
        try:
            pid = int((root / name).read_text()) if owned else None
        except (OSError, ValueError):
            pid = None
        return {"running": owned, "pid": pid}
    try:
        with (root / name).open() as handle:
            try:
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
                return {"running": False, "pid": None}
            except BlockingIOError:
                try:
                    pid = int(handle.read())
                except ValueError:
                    pid = None
                return {"running": True, "pid": pid}
    except FileNotFoundError:
        return {"running": False, "pid": None}


def allowed_file(root: Path, relative: str, *, archive=None) -> Path:
    """Only archive outputs/audio; never expose configuration, sockets or arbitrary files."""
    root = root.resolve()
    rel = Path(relative)
    if rel.is_absolute() or ".." in rel.parts or not rel.parts:
        raise FileNotFoundError("File is outside the archive outputs")
    category = rel.parts[0]
    formats = AUDIO_FORMATS if category == "audio" else TEXT_FORMATS
    if category == "transcripts" and "postprocessing" in rel.parts:
        formats = formats | {".png", ".zip"}
    if category not in {"transcripts", "subtitles", "diarization", "quality", "audio"}:
        raise FileNotFoundError("Not an archive output")
    path = root / rel
    resolved = path.resolve()
    if not resolved.is_relative_to(root / category) or path.suffix.lower() not in formats:
        raise FileNotFoundError("Not an archive output")
    from .storage import Store

    owned = archive is None
    archive = archive or Store(root)
    try:
        archive.materialize(relative)
    finally:
        if owned:
            archive.db.close()
    if not path.is_file():
        raise FileNotFoundError("File is no longer available")
    return path


def output_files(root: Path) -> list[dict]:
    from .storage import Store

    archive = Store(root)
    try:
        if not archive.local_blobs:
            files = []
            inventory = (
                archive.blobs.inventory()
                if hasattr(archive.blobs, "inventory")
                else ((key, archive.blobs.stat(key)) for key in archive.blobs.keys())
            )
            for key, metadata in inventory:
                category = Path(key).parts[0]
                if category not in {
                    "transcripts",
                    "subtitles",
                    "diarization",
                    "quality",
                    "audio",
                } or Path(key).suffix not in TEXT_FORMATS | {".png", ".zip"}:
                    continue
                files.append(
                    {
                        "path": key,
                        "name": Path(key).name,
                        "format": Path(key).suffix[1:],
                        "category": category,
                        **metadata,
                        "previewable": Path(key).suffix in TEXT_FORMATS,
                        "url": "/api/file?path=" + quote(key, safe=""),
                    }
                )
            return files
    finally:
        archive.db.close()
    archive = Store(root)
    try:
        files = []
        for category in ("transcripts", "subtitles", "diarization", "quality", "audio"):
            for path in sorted((root / category).rglob("*")):
                relative = path.relative_to(root).as_posix()
                try:
                    allowed_file(root, relative, archive=archive)
                    stat = path.stat()
                except (OSError, ValueError):
                    continue
                files.append(
                    {
                        "path": relative,
                        "name": path.name,
                        "format": path.suffix[1:],
                        "category": category,
                        "bytes": stat.st_size,
                        "modified_at": stat.st_mtime,
                        "previewable": path.suffix in TEXT_FORMATS,
                        "url": "/api/file?path=" + quote(relative, safe=""),
                    }
                )
        return files
    finally:
        archive.db.close()


def _events_in_file(path: Path) -> list[dict]:
    try:
        with path.open("rb") as handle:
            handle.seek(0, os.SEEK_END)
            handle.seek(max(0, handle.tell() - 131072))
            lines = handle.read().decode("utf-8", errors="replace").splitlines()
    except FileNotFoundError:
        return []
    events = []
    for line in lines:
        try:
            event = json.loads(line)
        except ValueError:
            continue
        if isinstance(event, dict) and event.get("event") != "asr_ready":
            events.append(event)
    return events[-20:][::-1]


def recent_events(root: Path) -> list[dict]:
    return sorted(
        ArchiveViews(root).events()
        + _events_in_file(root / "backfill.log")
        + _events_in_file(root / "diarization-backfill.log")
        + _events_in_file(root / "postprocessing-backfill.log"),
        key=lambda e: e.get("time", ""),
        reverse=True,
    )[:20]


def snapshot(root: Path) -> dict:
    root = root.resolve()
    # Short read transaction gives consistent counters without taking the worker lock.
    channels, discovery, profiles, jobs = ArchiveViews(root).snapshot()
    files = output_files(root)
    by_path = {f["path"]: f for f in files}
    counts = dict.fromkeys(
        (
            "queued",
            "downloading",
            "transcribing",
            "diarizing",
            "awaiting_diarization",
            "awaiting_postprocessing",
            "postprocessing",
            "awaiting_summary",
            "summarizing",
            "complete",
            "failed",
            "indexed",
        ),
        0,
    )
    names = {c["id"]: c["title"] for c in channels}
    for job in jobs:
        job["stage"] = stage(job)
        job["cover_url"] = thumbnail_url(job["video_id"])
        if job.get("diarization_state") in ("active", "failed"):
            job["updated_at"] = job["diarization_updated_at"]
            job["error"] = job["diarization_error"] or job["error"]
            job["next_retry"] = job["diarization_next_retry"]
            job["failed_stage"] = job["diarization_stage"]
        if job.get("postprocessing_state") in ("active", "failed") or job.get(
            "postprocessing_error"
        ):
            job["updated_at"] = job["postprocessing_updated_at"]
            job["error"] = job["postprocessing_error"] or job["error"]
            job["next_retry"] = job["postprocessing_next_retry"]
            job["failed_stage"] = "postprocessing " + (job["postprocessing_stage"] or "")
        if job.get("summary_state") in ("active", "failed") or job.get("summary_error"):
            job["updated_at"] = job["summary_updated_at"]
            job["error"] = job["summary_error"] or job["error"]
            job["next_retry"] = job["summary_next_retry"]
            job["failed_stage"] = "summary " + (job["summary_stage"] or "")
        counts[job["stage"]] += 1
        job["channel_title"] = names.get(job["channel_id"], job["channel_id"])
        paths = []
        if job["json_path"]:
            job["transcript_url"] = transcript_url(job["video_id"], job["profile"])
            base = Path(job["json_path"])
            paths.extend(
                base.with_suffix(ext).as_posix() for ext in (".md", ".srt", ".txt", ".json")
            )
            if base.with_suffix(".srt").as_posix() in by_path:
                job["srt_url"] = transcript_url(job["video_id"], job["profile"]) + "subtitles/"
        if job["audio_path"]:
            paths.append(job["audio_path"])
        if job.get("postprocessing_state") == "complete" and job.get("postprocessing_dir"):
            directory = Path(job["postprocessing_dir"])
            processed = (directory / "processed.md").as_posix()
            if processed in by_path:
                job["processed_path"] = processed
                job["processed_url"] = transcript_url(job["video_id"], job["profile"])
                paths.extend([processed, (directory / "processed-with-figures.zip").as_posix()])
        if job.get("summary_state") == "complete" and job.get("summary_dir"):
            # Published state owns availability; a fresh web instance has no local
            # bundle. Resource delivery still validates the manifest and contents.
            job["summary_url"] = artifact_url(job["video_id"], job["profile"], "summary")
        for key in ("asr_path", "prediction_path", "diarization_audio_path"):
            if job.get(key):
                paths.append(job[key])
        job["files"] = [by_path[p] for p in dict.fromkeys(paths) if p in by_path]
    for channel in channels:
        rows = [j for j in jobs if j["channel_id"] == channel["id"]]
        channel["videos"] = len({j["video_id"] for j in rows})
        channel["jobs"] = len([j for j in rows if j["job_id"] is not None])
        channel["complete"] = sum(j["stage"] == "complete" for j in rows)
        channel["failed"] = sum(j["stage"] == "failed" for j in rows)
        channel["discovery"] = [d for d in discovery if d["channel_id"] == channel["id"]]
    for profile in profiles:
        profile["identity"] = json.loads(profile["identity"])
    return {
        "generated_at": now(),
        "archive_root": str(root),
        "summary": {
            **counts,
            "channels": len(channels),
            "videos": len({j["video_id"] for j in jobs}),
            "jobs": sum(j["job_id"] is not None for j in jobs),
            "files": len(files),
            "asr_complete": sum(j["state"] == "complete" for j in jobs),
            "diarization_complete": sum(j["diarization_state"] == "complete" for j in jobs),
        },
        "worker": lock_status(root, "worker.lock"),
        "channels": channels,
        "jobs": jobs,
        "profiles": profiles,
        "files": files,
        "events": recent_events(root),
    }


def reader_snapshot(root: Path) -> dict:
    """Build reader metadata from one database snapshot, without enumerating objects."""
    channels, _, _, jobs = ArchiveViews(root).snapshot()
    names = {c["id"]: c["title"] for c in channels}
    for job in jobs:
        job["stage"] = stage(job)
        job["cover_url"] = thumbnail_url(job["video_id"])
        job["channel_title"] = names.get(job["channel_id"], job["channel_id"])
        base = transcript_url(job["video_id"], job["profile"]) if job["profile"] else None
        if job["json_path"]:
            job["transcript_url"] = base
            job["srt_url"] = base + "subtitles/"
        if job.get("postprocessing_state") == "complete" and job.get("postprocessing_dir"):
            job["processed_url"] = base
        if job.get("summary_state") == "complete" and job.get("summary_dir"):
            job["summary_url"] = artifact_url(job["video_id"], job["profile"], "summary")
    for channel in channels:
        channel["videos"] = len({j["video_id"] for j in jobs if j["channel_id"] == channel["id"]})
    return {
        "generated_at": now(),
        "summary": {"channels": len(channels), "videos": len({j["video_id"] for j in jobs})},
        "channels": channels,
        "jobs": jobs,
    }


class FeedChanges:
    """Small UI records and bounded revision history; snapshots remain authoritative."""

    fields = frozenset(
        "video_id channel_id title url duration published_at job_id profile stage scheduled "
        "channel_title asr_state updated_at completed_at language "
        "diarization_state speaker_count postprocessing_state figures summary_state "
        "summary_updated_at transcript_url processed_url cover_url summary_url srt_url".split()
    )

    def __init__(self, root):
        self.root = root
        self.history = {}
        self.lock = threading.Lock()
        self.cached = None
        self.revision = None

    @operation("feed.read")
    def get(self, revision=None):
        with self.lock:
            views = ArchiveViews(self.root)
            current = "feed-v3-" + views.revision()
            if revision == current:
                return {
                    **(self.cached["metadata"] if self.cached and self.revision == current
                       else views.feed_metadata()),
                    "generated_at": now(), "revision": current, "full": False,
                    "jobs": [], "removed": [],
                }
            if self.cached and self.revision == current:
                return {**self.cached["full"], "generated_at": now()}
            data = reader_snapshot(self.root)
            jobs = {}
            for source in data["jobs"]:
                key = (
                    str(source["job_id"])
                    if source["job_id"] is not None
                    else "video:" + source["video_id"]
                )
                jobs[key] = {k: v for k, v in source.items() if k in self.fields and v is not None}
                jobs[key]["key"] = key
            fingerprints = {
                key: hashlib.sha256(json.dumps(job, sort_keys=True).encode()).hexdigest()
                for key, job in jobs.items()
            }
            metadata = {
                "channels": [
                    {k: row[k] for k in ("id", "title", "url", "videos") if k in row}
                    for row in data["channels"]
                ],
                "summary": {k: data["summary"][k] for k in ("channels", "videos")},
            }
            previous = fingerprints if revision == current else self.history.get(revision)
            result = {
                **metadata,
                "generated_at": data["generated_at"],
                "revision": current,
                "full": previous is None,
                "jobs": list(jobs.values())
                if previous is None
                else [job for key, job in jobs.items() if previous.get(key) != fingerprints[key]],
                "removed": [] if previous is None else [key for key in previous if key not in jobs],
            }
            self.history[current] = fingerprints
            self.revision = current
            self.cached = {
                "metadata": metadata,
                "full": {**result, "full": True, "jobs": list(jobs.values()), "removed": []},
            }
            while len(self.history) > 8:
                del self.history[next(iter(self.history))]
            return result


def make_handler(root: Path, instance: str | None = None, *, public_origins=()):
    """Share HTTP routes between the local server and hosted WSGI entrypoint."""
    root = root.resolve()
    config = RuntimeConfig.load(root)
    instance = instance or uuid.uuid4().hex
    feed_changes = FeedChanges(root)
    from .auth import AuthenticationUnavailable, ClerkAccess

    access = ClerkAccess(root, (config.public_origin, *public_origins))

    def reader_configuration():
        from .realtime import configuration

        settings = {**access.configuration(), "realtime": configuration()}
        if access.enabled:
            settings["browser_scripts"] = CLERK_BROWSER_SCRIPTS
        if access.enabled and config.manual_queue:
            from .manual_queue import PROFILE
            from .openrouter import CHUNK_SECONDS, PRICE_PER_SECOND
            from .speaker_continuity import REFERENCE_BUDGET

            settings["processing"] = {
                "profile": PROFILE, "price_per_second": PRICE_PER_SECOND,
                "chunk_seconds": CHUNK_SECONDS, "reference_seconds": REFERENCE_BUDGET,
            }
        return settings

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, format, *args):
            # Polling is quiet; errors are still returned visibly to the UI.
            pass

        def send_headers(
            self,
            status,
            content_type,
            length,
            *,
            disposition=None,
            generated_html=False,
            content_encoding=None,
            vary_encoding=False,
        ):
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(length))
            cache = "no-store"
            path = urlsplit(self.path).path
            public = (
                not access.enabled
                and not config.access_token
                and not self.headers.get("Authorization")
            )
            if status == 200 and public:
                if path in STATIC_ASSETS:
                    cache = "public, max-age=0, s-maxage=3600, stale-while-revalidate=60"
                elif path == "/api/feed" and not urlsplit(self.path).query:
                    cache = "public, max-age=0, s-maxage=5, stale-while-revalidate=10"
                elif path.endswith("/cover.png"):
                    cache = "public, max-age=300, s-maxage=3600"
            self.send_header("Cache-Control", cache)
            if access.enabled:
                self.send_header("CDN-Cache-Control", "no-store")
                self.send_header("Vercel-CDN-Cache-Control", "no-store")
            self.send_header("Vary", "Accept, Accept-Encoding" if vary_encoding else "Accept")
            if content_encoding:
                self.send_header("Content-Encoding", content_encoding)
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Referrer-Policy", "no-referrer")
            from .realtime import origin as realtime_origin

            live_origin = realtime_origin() or ""
            clerk_csp = (
                f"default-src 'self'; script-src 'self' {access.issuer} https://challenges.cloudflare.com https://*.protect.clerk.com; "
                f"connect-src 'self' {access.issuer} {live_origin} https://*.protect.clerk.com:* https://clerk-telemetry.com https://*.clerk-telemetry.com; style-src 'self' 'unsafe-inline'; "
                f"img-src 'self' data: https://i.ytimg.com https://img.clerk.com {access.issuer}; "
                "frame-src https://challenges.cloudflare.com https://*.protect.clerk.com; worker-src 'self' blob:; "
                "object-src 'none'; base-uri 'none'; frame-ancestors 'none'"
                if access.enabled
                else None
            )
            self.send_header(
                "Content-Security-Policy",
                "default-src 'self'; script-src 'none'; style-src 'self' 'unsafe-inline'; img-src 'self'; object-src 'none'; base-uri 'none'; frame-ancestors 'none'"
                if generated_html
                else clerk_csp
                or "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data: https://i.ytimg.com; object-src 'none'; base-uri 'none'; frame-ancestors 'none'",
            )
            if getattr(self, "resource", None):
                self.send_header("Link", f'<{self.origin() + self.resource.url}>; rel="canonical"')
            if disposition:
                self.send_header("Content-Disposition", disposition)
            self.end_headers()

        def origin(self):
            return (
                config.public_origin.rstrip("/")
                if config.public_origin
                else "http://" + self.headers["Host"]
            )

        def document_data(self, data):
            resource = getattr(self, "resource", None)
            if not resource or resource.leaf.endswith(
                (".png", ".zip", ".json", ".srt", ".txt", "transcript.md")
            ):
                return data
            from .resources import rewrite_links

            text = rewrite_links(data.decode("utf-8"), resource)
            if "<!doctype html>" in text.lower():
                canonical = html.escape(self.origin() + resource.url, quote=True)
                tag = f'<link rel="canonical" href="{canonical}">'
                if "</head>" in text:
                    text = text.replace("</head>", tag + "</head>", 1)
                else:
                    text = text.replace('<meta charset="utf-8">', '<meta charset="utf-8">' + tag, 1)
            return text.encode()

        def write_data(self, data):
            if self.command != "HEAD":
                self.wfile.write(data)

        def send_json(self, value, status=200, *, compress=False):
            data = json.dumps(value, ensure_ascii=False).encode()
            encoded = compress and re.search(
                r"(?:^|,)\s*gzip\s*(?:,|$)", self.headers.get("Accept-Encoding", "")
            )
            if encoded:
                data = gzip.compress(data, compresslevel=4, mtime=0)
            self.send_headers(
                status,
                CONTENT_TYPES[".json"],
                len(data),
                content_encoding="gzip" if encoded else None,
                vary_encoding=compress,
            )
            self.write_data(data)

        def trusted(self):
            port = self.server.server_port
            hosts = {f"127.0.0.1:{port}", f"localhost:{port}"}
            if config.public_origin:
                hosts.add(urlsplit(config.public_origin).netloc)
            hosts.update(urlsplit(origin).netloc for origin in public_origins)
            if config.access_token:
                import hmac
                from http.cookies import SimpleCookie

                cookies = SimpleCookie(self.headers.get("Cookie", ""))
                supplied = self.headers.get("Authorization", "").removeprefix("Bearer ")
                if not supplied and "tubescribe_access" in cookies:
                    supplied = cookies["tubescribe_access"].value
                if not hmac.compare_digest(supplied, config.access_token):
                    self.send_json({"error": "Authentication required"}, 401)
                    return False
            if self.headers.get("Host") not in hosts:
                self.send_json({"error": "Use the local dashboard address"}, 403)
                return False
            origin = self.headers.get("Origin")
            if origin and origin not in {f"http://{h}" for h in hosts} | (
                set(public_origins) | ({config.public_origin} if config.public_origin else set())
            ):
                self.send_json({"error": "Cross-origin access is disabled"}, 403)
                return False
            navigation = (
                self.command in {"GET", "HEAD"}
                and self.headers.get("Sec-Fetch-Mode") == "navigate"
                and self.headers.get("Sec-Fetch-Dest") == "document"
            )
            if self.headers.get("Sec-Fetch-Site") == "cross-site" and not navigation:
                self.send_json({"error": "Cross-site access is disabled"}, 403)
                return False
            return True

        def do_GET(self):
            if not self.trusted():
                return
            url = urlsplit(self.path)
            query = parse_qs(url.query)
            try:
                self.resource = None
                if url.path == "/auth/config":
                    self.send_json(reader_configuration())
                    return
                if url.path == "/api/realtime-authorization":
                    if not access.enabled:
                        self.send_json({"error": "Hosted authentication is required"}, 503)
                        return
                    user_id = access.session_user(self.headers)
                    if not user_id:
                        self.send_json({"error": "Sign in required"}, 401)
                        return
                    self.send_json({"user_id": user_id, "issuer": access.issuer})
                    return
                # Check ownership before resolving ANY data URL, including legacy
                # paths, figures, metadata, downloads, and HEAD requests.
                static = url.path in STATIC_ASSETS
                if access.enabled and not static and url.path != "/llms.txt":
                    identity = access.identify(self.headers)
                    if identity is None:
                        if self.headers.get("Sec-Fetch-Dest") == "document" or (
                            "text/html" in self.headers.get("Accept", "")
                            and not url.path.startswith("/api/")
                        ):
                            self.send_response(302)
                            self.send_header("Location", "/?return_to=" + quote(self.path, safe=""))
                            self.send_header("Cache-Control", "no-store")
                            self.send_header("Content-Length", "0")
                            self.end_headers()
                        else:
                            self.send_json(
                                {"error": "Sign in to read your library", "sign_in_url": "/"}, 401
                            )
                        return
                    if url.path == "/api/session":
                        self.send_json(
                            {"user_id": identity.user_id, "has_library": identity.owns_library}
                        )
                        return
                    if not identity.owns_library:
                        self.empty_library(url.path, query)
                        return
                if url.path == "/api/processing/worker" and access.enabled and config.manual_queue:
                    from .workflow_dispatch import call

                    self.send_json(call("status", request_token=self.headers.get("x-vercel-oidc-token")))
                    return
                if url.path == "/api/processing" and access.enabled and config.manual_queue:
                    from .manual_queue import status

                    self.send_json({"videos": status(root)})
                    return
                if match := re.fullmatch(r"/api/videos/([^/]+)/pipeline/?", url.path):
                    from .pipeline_details import details
                    from .resources import segment

                    self.send_json(details(root, segment(match[1]), query.get("profile", [None])[0]))
                    return
                from .resources import render_video, resolve, segment, video_metadata
                from .rollups import index as digest_index
                from .rollups import records as digest_records
                from .rollups import url as digest_url
                from .storage import Store

                if url.path in {"/library", "/library/", "/api/library", "/llms.txt"}:
                    from .catalogue import catalogue, guide, render_catalogue

                    if url.path == "/llms.txt":
                        data = guide(self.origin()).encode()
                        self.send_headers(200, "text/plain; charset=utf-8", len(data))
                        self.write_data(data)
                    else:
                        data = catalogue(root, query, self.origin())
                        if url.path == "/api/library" or "application/json" in self.headers.get(
                            "Accept", ""
                        ):
                            self.send_json(data, compress=True)
                        else:
                            text = render_catalogue(data).encode()
                            self.send_headers(
                                200, CONTENT_TYPES[".html"], len(text), generated_html=True
                            )
                            self.write_data(text)
                    return

                digest_match = re.fullmatch(
                    r"/(?:api/)?(summaries|channels/([^/]+)/summary)(?:/(summary\.md))?/?", url.path
                )
                if url.path == "/api/summaries" or digest_match:
                    store = Store(root)
                    try:
                        metadata = digest_index(store)
                        metadata.pop("waiting_for_videos", None)
                        for entry in metadata["items"]:
                            entry.pop("error", None)
                            entry.pop("input_hash", None)
                            entry["markdown_url"] = entry["url"] + "summary.md"
                        if url.path == "/api/summaries":
                            self.send_json(metadata)
                            return
                        scope = "channel:" + segment(digest_match[2]) if digest_match[2] else "all"
                        entry = next((e for e in metadata["items"] if e["scope"] == scope), None)
                        if entry is None:
                            raise FileNotFoundError("Unknown channel")
                        if url.path.startswith("/api/") or "application/json" in self.headers.get(
                            "Accept", ""
                        ):
                            self.send_json(
                                {
                                    **entry,
                                    "days": metadata["days"],
                                    "since": metadata["since"],
                                    "until": metadata["until"],
                                }
                            )
                            return
                        if not digest_match[3] and not url.path.endswith("/"):
                            self.send_response(308)
                            self.send_header("Location", digest_url(scope))
                            self.send_header("Content-Length", "0")
                            self.end_headers()
                            return
                        if entry["state"] != "ready":
                            self.send_json(
                                {
                                    **entry,
                                    "message": "The rolling summary is being refreshed; expired content is not served.",
                                },
                                503,
                            )
                            return
                        row = digest_records(store)[scope]
                        if not store.bundle_valid(row["output_dir"], row["manifest_sha256"]):
                            raise FileNotFoundError("Digest bundle is unavailable")
                        manifest = json.loads(
                            store.read_key(
                                row["output_dir"] + "/manifest.json", row["manifest_sha256"]
                            )
                        )
                        leaf = "summary.md" if digest_match[3] else "preview.html"
                        data = store.read_key(
                            row["output_dir"] + "/" + leaf, manifest["files"][leaf]
                        )
                        if leaf == "preview.html":
                            tag = (
                                '<link rel="canonical" href="'
                                + html.escape(self.origin() + digest_url(scope), quote=True)
                                + '">'
                            )
                            data = data.decode().replace("</head>", tag + "</head>", 1).encode()
                        self.send_headers(
                            200,
                            CONTENT_TYPES[".md" if digest_match[3] else ".html"],
                            len(data),
                            generated_html=not bool(digest_match[3]),
                        )
                        self.write_data(data)
                    finally:
                        store.db.close()
                    return

                self.resource = None
                views = ArchiveViews(root)
                force_json = url.path.startswith("/api/videos/")
                resource_path = url.path.removeprefix("/api") if force_json else url.path
                wants_json = force_json or "application/json" in self.headers.get("Accept", "")
                if video := re.fullmatch(r"/videos/([^/]+)(?:/(transcripts))?/?", resource_path):
                    metadata = video_metadata(views, segment(video[1]), self.origin())
                    if wants_json or video[2]:
                        self.send_json(metadata["transcripts"] if video[2] else metadata)
                    else:
                        data = render_video(metadata).encode()
                        data = self.document_data(data)
                        self.send_headers(
                            200, CONTENT_TYPES[".html"], len(data), generated_html=True
                        )
                        self.write_data(data)
                    return
                self.resource = resource = resolve(views, resource_path)
                if resource:
                    if (
                        resource.canonical_request
                        and resource_path != resource.url
                        and not force_json
                    ):
                        self.send_response(308)
                        self.send_header("Location", resource.url)
                        self.send_header("Content-Length", "0")
                        self.end_headers()
                        return
                    if wants_json and not resource.leaf:
                        if resource.workflow:
                            from .artifacts import workflow

                            workflow(root, resource.workflow)
                        self.send_json(resource.metadata(views, self.origin()))
                        return
                    if resource.leaf == "cover.png":
                        if resource.row["postprocessing_state"] != "complete":
                            raise FileNotFoundError()
                        directory = resource.row["postprocessing_dir"]
                        checksum = resource.row["postprocessing_manifest_sha256"]
                        if not directory or not checksum:
                            raise FileNotFoundError()
                        store = Store(root)
                        try:
                            manifest = json.loads(
                                store.read_key(directory + "/manifest.json", checksum)
                            )
                            names = sorted(
                                n
                                for n in manifest["files"]
                                if re.fullmatch(r"figures/frame_\d+\.png", n)
                            )
                            if not names:
                                raise FileNotFoundError()
                            data = store.read_key(
                                directory + "/" + names[0], manifest["files"][names[0]]
                            )
                        finally:
                            store.db.close()
                        self.send_headers(200, CONTENT_TYPES[".png"], len(data))
                        self.write_data(data)
                        return
                    if resource.raw_key:
                        data = allowed_file(root, resource.raw_key).read_bytes()
                        if not resource.leaf:
                            from .postprocessing import render_preview

                            data = (
                                render_preview(data.decode(), resource.row["title"])
                                .replace('href="processed.md"', 'href="transcript.md"')
                                .replace(
                                    "</header>",
                                    ' · <a href="transcript.json">JSON</a> · <a href="transcript.txt">Text</a>'
                                    ' · <a href="source.srt">Download SRT</a> · <a href="subtitles/">View SRT</a></header>',
                                )
                                .encode()
                            )
                        data = self.document_data(data)
                        suffix = Path(resource.raw_key).suffix if resource.leaf else ".html"
                        self.send_headers(
                            200,
                            CONTENT_TYPES.get(suffix, "text/plain; charset=utf-8"),
                            len(data),
                            generated_html=suffix == ".html",
                        )
                        self.write_data(data)
                        return
                    if not resource.internal:
                        raise FileNotFoundError()
                    if resource.canonical_request:
                        url = url._replace(path=resource.internal)
                if url.path == "/api/health" and not public_origins and not config.public_origin:
                    self.send_json(
                        {
                            "service": "tubescribe-dashboard",
                            "instance": instance,
                            "pid": os.getpid(),
                            "archive_root": str(root),
                        }
                    )
                elif url.path == "/api/feed":
                    # Bootstrap the sleeping discovery clock from an authenticated
                    # owner visit. Once started it continues with the browser closed.
                    # Admission still comes exclusively from the explicit Process action.
                    if access.enabled and config.manual_queue and not query.get("revision"):
                        from .workflow_dispatch import bootstrap

                        bootstrap(request_token=self.headers.get("x-vercel-oidc-token"))
                    self.send_json(
                        feed_changes.get(query.get("revision", [None])[0]), compress=True
                    )
                elif match := re.fullmatch(
                    r"/artifacts/(\d+)/([a-z][a-z0-9_-]{0,63})(?:/(artifact\.md|figures/frame_\d+\.png))?/?",
                    url.path,
                ):
                    from .artifacts import artifact_path, workflow

                    if not match[3] and not url.path.endswith("/"):
                        self.send_response(302)
                        self.send_header("Location", url.path + "/")
                        self.send_header("Content-Length", "0")
                        self.end_headers()
                        return
                    definition = workflow(root, match[2])
                    row = ArchiveViews(root).artifact(int(match[1]), match[2])
                    if not row or row[1] != "complete":
                        raise FileNotFoundError()
                    if row[2] == "complete" and row[3]:
                        # Published pages are read-only; the worker validates manifests on reruns.
                        directory = artifact_path(SimpleNamespace(root=root), row[3])
                        path = directory / (match[3] or "preview.html")
                        if not path.resolve().is_relative_to(directory):
                            raise FileNotFoundError()
                        if (directory / "references.json").is_file() and (
                            not match[3] or match[3] == "artifact.md"
                        ):
                            from .publication import render_bundle

                            data = render_bundle(directory, markdown=bool(match[3])).encode()
                        else:
                            data = path.read_bytes()
                        data = self.document_data(data)
                        self.send_headers(
                            200,
                            CONTENT_TYPES.get(path.suffix, "text/plain; charset=utf-8"),
                            len(data),
                            generated_html=path.suffix == ".html",
                        )
                        self.write_data(data)
                    elif match[3]:
                        raise FileNotFoundError()
                    else:
                        data = (
                            '<!doctype html><html lang="en"><meta charset="utf-8">'
                            '<meta name="viewport" content="width=device-width,initial-scale=1">'
                            f"<title>{html.escape(definition['title'])}</title>"
                            '<link rel="stylesheet" href="/style.css"><main>'
                            f"<h1>{html.escape(definition['title'])}</h1><p>{html.escape(row[0])}</p>"
                            "<p>This summary is not available yet. Please check back later.</p>"
                            f'<p><a href="/processed/{match[1]}/preview.html">Full transcript</a></p></main></html>'
                        ).encode()
                        data = self.document_data(data)
                        self.send_headers(200, CONTENT_TYPES[".html"], len(data))
                        self.write_data(data)
                elif match := re.fullmatch(r"/transcripts/(\d+)/srt", url.path):
                    from .srt_viewer import render_srt

                    row = ArchiveViews(root).srt(int(match[1]))
                    if not row:
                        raise FileNotFoundError()
                    relative = Path(row[0]).with_suffix(".srt").as_posix()
                    path = allowed_file(root, relative)
                    data = render_srt(
                        path.read_text(encoding="utf-8"),
                        title=row[1],
                        video_id=row[2],
                        download_url=self.resource.base + "source.srt"
                        if self.resource
                        else transcript_url(row[2], row[4]) + "source.srt",
                        processed_url=f"/processed/{match[1]}/preview.html"
                        if row[3] == "complete"
                        else None,
                    ).encode()
                    data = self.document_data(data)
                    self.send_headers(200, CONTENT_TYPES[".html"], len(data), generated_html=True)
                    self.write_data(data)
                elif match := re.fullmatch(
                    r"/processed/(\d+)/(preview\.html|processed\.md|source\.srt|processed-with-figures\.zip|figures/frame_\d+\.png)",
                    url.path,
                ):
                    row = ArchiveViews(root).processed(int(match[1]))
                    if not row:
                        raise FileNotFoundError()
                    filename = match[2]
                    if filename == "preview.html":
                        from .artifacts import workflows
                        from .postprocessing import render_preview

                        path = allowed_file(root, (Path(row[0]) / "processed.md").as_posix())
                        data = (
                            render_preview(path.read_text(), row[2])
                            .replace(
                                '<a href="processed.md" download>Download Markdown</a>',
                                '<a href="processed.md" download>Download Markdown</a> · <a href="processed-with-figures.zip" download>Download with figures</a> · <a href="source.srt" download>Source SRT</a>'
                                f' · <a href="/transcripts/{match[1]}/srt">View SRT</a>',
                            )
                            .replace(
                                "</header>",
                                " · "
                                + " · ".join(
                                    f'<a href="/artifacts/{match[1]}/{w["name"]}/">{html.escape(w["title"])}</a>'
                                    for w in workflows(root)
                                )
                                + "</header>",
                            )
                            .encode()
                        )
                        data = self.document_data(data)
                        self.send_headers(
                            200, CONTENT_TYPES[".html"], len(data), generated_html=True
                        )
                        self.write_data(data)
                    else:
                        relative = (
                            Path(row[1]).with_suffix(".srt")
                            if filename == "source.srt"
                            else Path(row[0]) / filename
                        )
                        path = allowed_file(root, relative.as_posix())
                        data = path.read_bytes()
                        data = self.document_data(data)
                        self.send_headers(
                            200,
                            CONTENT_TYPES.get(path.suffix, "text/plain; charset=utf-8"),
                            len(data),
                            disposition=f"attachment; filename*=UTF-8''{quote(path.name)}"
                            if path.suffix == ".zip"
                            else None,
                        )
                        self.write_data(data)
                elif url.path in STATIC_ASSETS:
                    path = ASSETS / STATIC_ASSETS[url.path]
                    data = path.read_bytes()
                    if url.path == "/":
                        public_config = html.escape(json.dumps(reader_configuration()), quote=True)
                        head = [f'<meta name="tubescribe-config" content="{public_config}">']
                        if access.enabled:
                            head.append(f'<link rel="preconnect" href="{access.issuer}" crossorigin="anonymous">')
                            head.extend(
                                f'<link rel="preload" as="script" href="{access.issuer}{script}" crossorigin="anonymous">'
                                for script in CLERK_BROWSER_SCRIPTS
                            )
                        data = data.replace(b"<!-- reader-bootstrap -->", "\n  ".join(head).encode(), 1)
                    self.send_headers(200, CONTENT_TYPES[path.suffix], len(data))
                    self.write_data(data)
                else:
                    self.send_json({"error": "Not found"}, 404)
            except AuthenticationUnavailable:
                self.send_json({"error": "Sign-in verification is temporarily unavailable"}, 503)
            except (FileNotFoundError, IsADirectoryError):
                self.send_json({"error": "File is not available"}, 404)
            except ValueError as error:
                self.send_json({"error": str(error)}, 400)
            except database_errors():
                self.send_json({"error": "Archive database is unavailable; refresh to retry"}, 503)
            except (BrokenPipeError, ConnectionResetError):
                pass
            except OSError:
                self.send_json({"error": "Output file is temporarily unavailable"}, 503)

        do_HEAD = do_GET

        def empty_library(self, path, query):
            """An authenticated new account sees no archive metadata or object reads."""
            if path == "/api/feed":
                self.send_json(
                    {
                        "generated_at": now(),
                        "summary": {"channels": 0, "videos": 0},
                        "channels": [],
                        "jobs": [],
                        "revision": "empty",
                        "full": True,
                        "removed": [],
                    }
                )
            elif path == "/api/summaries":
                self.send_json({"days": 7, "items": []})
            elif path in {"/api/library", "/library", "/library/"}:
                from .catalogue import render_catalogue

                data = {
                    "description": "No videos yet",
                    "total": 0,
                    "limit": 50,
                    "offset": 0,
                    "channels": [],
                    "items": [],
                    "links": {
                        "self": self.origin() + "/api/library",
                        "next": None,
                        "previous": None,
                        "summaries": self.origin() + "/api/summaries",
                        "guide": self.origin() + "/llms.txt",
                    },
                }
                if path == "/api/library" or "application/json" in self.headers.get("Accept", ""):
                    self.send_json(data)
                else:
                    content = render_catalogue(data).encode()
                    self.send_headers(
                        200, CONTENT_TYPES[".html"], len(content), generated_html=True
                    )
                    self.write_data(content)
            else:
                self.send_json({"error": "File is not available"}, 404)

        def do_POST(self):
            if access.enabled and config.manual_queue and self.path == "/api/processing":
                if not self.trusted():
                    return
                self.resource = None
                self.close_connection = True
                if not self.headers.get("Origin"):
                    self.send_json({"error": "A same-site origin is required"}, 403)
                    return
                try:
                    identity = access.identify(self.headers)
                    if identity is None or not identity.owns_library:
                        self.send_json({"error": "Library owner access is required"}, 401 if identity is None else 403)
                        return
                    if self.headers.get_content_type() != "application/json" or self.headers.get("Transfer-Encoding"):
                        raise ValueError("A JSON selection is required")
                    length = int(self.headers.get("Content-Length", "0"))
                    if not 0 < length <= 4096:
                        raise ValueError("Invalid selection size")
                    payload = json.loads(self.rfile.read(length))
                    if not isinstance(payload, dict) or set(payload) != {"action", "video_ids"}:
                        raise ValueError("Invalid processing selection")
                    from .manual_queue import change

                    result = change(root, payload["action"], payload["video_ids"])
                    if result["changed"] and payload["action"] != "cancel":
                        from .workflow_dispatch import call

                        worker = call("wake", request_token=self.headers.get("x-vercel-oidc-token"))
                        if worker.get("configured"):
                            result["worker"] = worker
                    self.send_json(result)
                except (ValueError, TypeError):
                    self.send_json({"error": "Invalid queue action or video selection"}, 400)
                except Exception:
                    self.send_json({"error": "Processing queue is unavailable"}, 503)
                return
            self.read_only()

        def read_only(self):
            if not self.trusted():
                return
            self.resource = None
            self.close_connection = True
            self.send_response(405)
            data = b'{"error":"This library is read-only. Use GET or HEAD."}'
            self.send_header("Allow", "GET, HEAD")
            self.send_header("Content-Type", CONTENT_TYPES[".json"])
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.write_data(data)

        do_PUT = read_only
        do_PATCH = read_only
        do_DELETE = read_only

    return Handler


def make_server(root: Path, port: int = 0, instance: str | None = None) -> ThreadingHTTPServer:
    config = RuntimeConfig.load(root)
    return ThreadingHTTPServer((config.host, port), make_handler(root, instance))


def dashboard_status(root: Path) -> dict:
    locked = lock_status(root, "dashboard.lock")
    try:
        state = json.loads((root / "dashboard.json").read_text())
    except (FileNotFoundError, ValueError):
        return {**locked, "ready": False}
    ready = False
    if locked["running"] and locked["pid"] == state.get("pid"):
        try:
            address = urlsplit(state["url"])
            if address.hostname != "127.0.0.1" or address.scheme != "http":
                raise ValueError("Invalid dashboard address")
            config = RuntimeConfig.load(root)
            headers = (
                {"Authorization": "Bearer " + config.access_token} if config.access_token else None
            )
            response = httpx.get(
                state["url"] + "/api/health", timeout=1, trust_env=False, headers=headers
            )
            response.raise_for_status()
            health = response.json()
            ready = (
                health.get("instance") == state.get("instance")
                and health.get("pid") == locked["pid"]
            )
        except (httpx.HTTPError, ValueError, KeyError):
            pass
    return {**state, **locked, "ready": ready}


def dashboard(root: Path, action="start", port=0):
    root = root.resolve()
    if action in ("status", "stop", "start"):
        state = dashboard_status(root)
        if action == "status":
            return state
        if action == "stop":
            if not state["running"]:
                return {"running": False, "message": "No active dashboard"}
            if not state["ready"]:
                raise RuntimeError("Dashboard ownership could not be verified; inspect its log")
            os.kill(state["pid"], signal.SIGTERM)
            return {"message": "Dashboard stop requested", "pid": state["pid"]}
        if state["running"]:
            if not state["ready"]:
                raise RuntimeError("Dashboard is starting or unavailable; inspect dashboard.log")
            return {**state, "reused": True}
        with (root / "dashboard.log").open("ab") as log:
            process = subprocess.Popen(
                [
                    sys.executable,
                    "-m",
                    "tubescribe.cli",
                    "--data",
                    str(root),
                    "dashboard",
                    "serve",
                    "--port",
                    str(port),
                ],
                cwd=root.parent,
                stdin=subprocess.DEVNULL,
                stdout=log,
                stderr=log,
                start_new_session=True,
            )
        for _ in range(50):
            if process.poll() is not None:
                raise RuntimeError(f"Dashboard failed to start; inspect {root / 'dashboard.log'}")
            state = dashboard_status(root)
            if state["ready"]:
                return state
            time.sleep(0.1)
        raise RuntimeError(f"Dashboard startup timed out; inspect {root / 'dashboard.log'}")
    with (root / "dashboard.lock").open("a") as handle:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise RuntimeError("A dashboard already owns this archive; use dashboard status")
        handle.seek(0)
        handle.truncate()
        handle.write(str(os.getpid()))
        handle.flush()
        instance = uuid.uuid4().hex
        with make_server(root, port, instance) as server:
            state = {
                "pid": os.getpid(),
                "url": f"http://127.0.0.1:{server.server_port}",
                "instance": instance,
                "started_at": now(),
                "log": str(root / "dashboard.log"),
            }
            atomic_write(root / "dashboard.json", json.dumps(state, indent=2).encode())
            print(json.dumps(state), flush=True)
            server.serve_forever()
