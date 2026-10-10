"""Read-only, loopback-only archive monitoring; inference stays in the worker."""

from __future__ import annotations

import fcntl
import html
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import threading
import time
import uuid
from http import HTTPStatus
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

ASSETS = Path(__file__).with_name("dashboard_assets")
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
    ".zip": "application/zip",
    ".md": "text/markdown; charset=utf-8",
    ".svg": "image/svg+xml",
}


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


def allowed_file(root: Path, relative: str) -> Path:
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

    archive = Store(root)
    try:
        archive.materialize(relative)
    finally:
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
            for key in archive.blobs.keys():
                category = Path(key).parts[0]
                if category not in {
                    "transcripts",
                    "subtitles",
                    "diarization",
                    "quality",
                    "audio",
                } or Path(key).suffix not in TEXT_FORMATS | {".png", ".zip"}:
                    continue
                metadata = archive.blobs.stat(key)
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
    files = []
    for category in ("transcripts", "subtitles", "diarization", "quality", "audio"):
        for path in sorted((root / category).rglob("*")):
            relative = path.relative_to(root).as_posix()
            try:
                allowed_file(root, relative)
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
    covers = {}
    for file in files:
        path = Path(file["path"])
        if re.fullmatch(r"frame_\d+\.png", path.name) and path.parent.name == "figures":
            covers.setdefault(path.parent.parent.as_posix(), path.name)
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
                if cover := covers.get(directory.as_posix()):
                    job["cover_url"] = job["processed_url"] + "figures/" + cover
        if job.get("summary_state") == "complete" and job.get("summary_dir"):
            from .artifacts import artifact_path

            directory = artifact_path(SimpleNamespace(root=root), job["summary_dir"])
            if (directory / "artifact.md").is_file():
                job["summary_url"] = artifact_url(job["video_id"], job["profile"], "summary")
                for figure in sorted((directory / "figures").glob("frame_*.png")):
                    if re.fullmatch(r"frame_\d+\.png", figure.name) and figure.is_file():
                        job["cover_url"] = job["summary_url"] + "figures/" + figure.name
                        break
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


def make_server(root: Path, port: int = 0, instance: str | None = None) -> ThreadingHTTPServer:
    root = root.resolve()
    config = RuntimeConfig.load(root)
    instance = instance or uuid.uuid4().hex
    children_lock = threading.Lock()

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, format, *args):
            # Polling is quiet; errors are still returned visibly to the UI.
            pass

        def send_headers(
            self, status, content_type, length, *, disposition=None, generated_html=False
        ):
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(length))
            self.send_header("Cache-Control", "no-store")
            self.send_header("Vary", "Accept")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header(
                "Content-Security-Policy",
                "default-src 'self'; script-src 'none'; style-src 'self' 'unsafe-inline'; img-src 'self'; object-src 'none'; base-uri 'none'; frame-ancestors 'none'"
                if generated_html
                else "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; object-src 'none'; base-uri 'none'; frame-ancestors 'none'",
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

        def send_json(self, value, status=200):
            data = json.dumps(value, ensure_ascii=False).encode()
            self.send_headers(status, CONTENT_TYPES[".json"], len(data))
            self.write_data(data)

        def trusted(self):
            port = self.server.server_port
            hosts = {f"127.0.0.1:{port}", f"localhost:{port}"}
            if config.public_origin:
                hosts.add(urlsplit(config.public_origin).netloc)
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
                {config.public_origin} if config.public_origin else set()
            ):
                self.send_json({"error": "Cross-origin access is disabled"}, 403)
                return False
            if self.headers.get("Sec-Fetch-Site") == "cross-site":
                self.send_json({"error": "Cross-site access is disabled"}, 403)
                return False
            return True

        def do_GET(self):
            if not self.trusted():
                return
            url = urlsplit(self.path)
            query = parse_qs(url.query)
            try:
                from .resources import render_video, resolve, segment, video_metadata

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
                if url.path == "/api/health":
                    self.send_json(
                        {
                            "service": "tubescribe-dashboard",
                            "instance": instance,
                            "pid": os.getpid(),
                            "archive_root": str(root),
                        }
                    )
                elif url.path == "/api/ready":
                    from .operations import readiness

                    result = readiness(root)
                    self.send_json(result, 200 if result["ready"] else 503)
                elif url.path in {"/api/metrics", "/metrics"}:
                    from .operations import metrics, prometheus

                    result = metrics(root)
                    if url.path == "/api/metrics":
                        self.send_json(result)
                    else:
                        data = prometheus(result).encode()
                        self.send_headers(
                            200, "text/plain; version=0.0.4; charset=utf-8", len(data)
                        )
                        self.write_data(data)
                elif url.path == "/api/snapshot":
                    self.send_json(snapshot(root))
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
                        state = row[2] or "ready"
                        pending = state in {"pending", "active"}
                        interrupted = False
                        if pending and row[5]:
                            try:
                                os.kill(row[5], 0)
                            except ProcessLookupError:
                                state, pending, interrupted = "interrupted", False, True
                            except PermissionError:
                                pass
                        refresh = '<meta http-equiv="refresh" content="5">' if pending else ""
                        form = (
                            ""
                            if pending
                            else (
                                f'<form data-artifact method="post" action="/artifacts/{match[1]}/{match[2]}/run">'
                                f'<input type="hidden" name="token" value="{instance}">'
                                '<button type="submit">Build artifact</button></form>'
                            )
                        )
                        data = (
                            '<!doctype html><html lang="en"><meta charset="utf-8">'
                            '<meta name="viewport" content="width=device-width,initial-scale=1">'
                            f"{refresh}<title>{html.escape(definition['title'])}</title>"
                            '<link rel="stylesheet" href="/style.css"><script src="/artifact.js" defer></script><main>'
                            f"<h1>{html.escape(definition['title'])}</h1><p>{html.escape(row[0])}</p>"
                            f'<p class="artifact-status">{html.escape(state.capitalize())}</p>'
                            + (
                                "<p>Generating artifact. This page refreshes automatically.</p>"
                                if state == "active"
                                else "<p>The workflow will start when the archive worker is available. This page refreshes automatically.</p>"
                                if pending
                                else ""
                            )
                            + (f'<p role="alert">{html.escape(row[4])}</p>' if row[4] else "")
                            + (
                                "<p>Worker interrupted. Build again to resume saved progress.</p>"
                                if interrupted
                                else ""
                            )
                            + '<p role="alert" data-artifact-error></p>'
                            + form
                            + f'<p><a href="/processed/{match[1]}/preview.html">Full transcript</a></p></main></html>'
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
                        else "/api/file?path=" + quote(relative, safe="") + "&download=1",
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
                elif url.path in ("/api/file", "/api/preview"):
                    path = allowed_file(root, query.get("path", [""])[0])
                    if url.path == "/api/preview":
                        if path.suffix not in TEXT_FORMATS:
                            raise ValueError("Preview is available for transcript text formats")
                        with path.open("rb") as handle:
                            data = handle.read(2 * 1024**2 + 1)
                        self.send_json(
                            {
                                "content": data[: 2 * 1024**2].decode("utf-8", errors="replace"),
                                "truncated": len(data) > 2 * 1024**2,
                            }
                        )
                    else:
                        with path.open("rb") as handle:
                            length = os.fstat(handle.fileno()).st_size
                            disposition = (
                                "attachment" if query.get("download") == ["1"] else "inline"
                            )
                            self.send_headers(
                                200,
                                CONTENT_TYPES.get(path.suffix, "text/plain; charset=utf-8"),
                                length,
                                disposition=f"{disposition}; filename*=UTF-8''{quote(path.name)}",
                            )
                            if self.command != "HEAD":
                                shutil.copyfileobj(handle, self.wfile)
                elif url.path in ("/", "/app.js", "/artifact.js", "/style.css") or re.fullmatch(
                    r"/icons/(magnifying-glass|document-text|clipboard-document|arrow-top-right-on-square|arrow-down-tray|chevron-up|x-mark|arrow-path)\.svg",
                    url.path,
                ):
                    path = ASSETS / ("index.html" if url.path == "/" else url.path[1:])
                    data = path.read_bytes()
                    self.send_headers(200, CONTENT_TYPES[path.suffix], len(data))
                    self.write_data(data)
                else:
                    self.send_json({"error": "Not found"}, 404)
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

        def do_POST(self):
            if not self.trusted():
                return
            self.resource = None
            requested_path = urlsplit(self.path).path
            from .resources import resolve

            try:
                resource = (
                    resolve(ArchiveViews(root), requested_path)
                    if requested_path.startswith("/videos/")
                    else None
                )
            except FileNotFoundError:
                self.send_json({"error": "Resource not found"}, 404)
                return
            internal = requested_path
            if resource and resource.canonical_request and resource.workflow and not resource.leaf:
                self.resource = resource
                internal = resource.internal + "run"
            match = re.fullmatch(r"/artifacts/(\d+)/([a-z][a-z0-9_-]{0,63})/run", internal)
            if not match:
                self.send_json(
                    {"error": "Only artifact workflows can be triggered here"},
                    HTTPStatus.METHOD_NOT_ALLOWED,
                )
                return
            try:
                from .artifacts import inputs
                from .storage import Store

                length = int(self.headers.get("Content-Length", "0"))
                content_type = self.headers.get("Content-Type", "").split(";")[0]
                json_request = content_type == "application/json" and self.resource is not None
                if (
                    not 0 < length <= 1024
                    or content_type not in {"application/x-www-form-urlencoded", "application/json"}
                    or (content_type == "application/json" and not json_request)
                ):
                    raise ValueError("Use a workflow form or JSON token object")
                raw = self.rfile.read(length).decode()
                if json_request:
                    value = json.loads(raw)
                    if (
                        not isinstance(value, dict)
                        or set(value) != {"token"}
                        or not isinstance(value["token"], str)
                    ):
                        raise ValueError("Workflow JSON requires a token string")
                    fields = {"token": [value["token"]]}
                else:
                    fields = parse_qs(raw)
                if fields.get("token") != [instance]:
                    self.send_json({"error": "Refresh the workflow page before triggering it"}, 403)
                    return
                with children_lock:
                    store = Store(root)
                    try:
                        from .submission import Dispatcher, JobRequest, LocalProcess

                        source = inputs(store, int(match[1]), match[2])[0]
                        key = (int(match[1]), match[2])
                        store.ensure_stage("artifact", key[0], workflow=key[1])
                        request = JobRequest(
                            "artifacts",
                            {
                                "video": [source["video_id"]],
                                "profile": source["profile"],
                                "workflow": match[2],
                                "wait": True,
                            },
                        )
                        identifier = Dispatcher(
                            store, process=LocalProcess(spawn=subprocess.Popen)
                        ).submit(request, background=True)
                        pid = store.submission(identifier)["pid"]
                        store.stage_update("artifact", key[0], workflow=key[1], run_pid=pid)
                    finally:
                        store.db.close()
                location = artifact_url(source["video_id"], source["profile"], match[2])
                if json_request:
                    data = json.dumps(
                        {
                            "submission_id": identifier,
                            "state": "queued",
                            "url": self.origin() + location,
                        }
                    ).encode()
                    self.send_response(202)
                    self.send_header("Content-Type", CONTENT_TYPES[".json"])
                    self.send_header("Content-Length", str(len(data)))
                    self.send_header("Cache-Control", "no-store")
                    self.send_header("Location", location)
                    self.end_headers()
                    self.write_data(data)
                else:
                    self.send_response(303)
                    self.send_header("Location", location)
                    self.send_header("Content-Length", "0")
                    self.end_headers()
            except (ValueError, FileNotFoundError, KeyError) as error:
                self.send_json({"error": str(error)}, 400)
            except (OSError, RuntimeError, *database_errors()):
                self.send_json({"error": "Artifact worker could not start"}, 503)

    return ThreadingHTTPServer((config.host, port), Handler)


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
