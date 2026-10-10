"""Read-only, loopback-only archive monitoring; inference stays in the worker."""

from __future__ import annotations

import contextlib
import fcntl
import html
import json
import os
import re
import shutil
import signal
import sqlite3
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

from .pipeline import status as stage
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
}


def lock_status(root: Path, name: str) -> dict:
    if name == "worker.lock" and (root / "archive.sqlite3").is_file():
        with contextlib.closing(sqlite3.connect(root / "archive.sqlite3")) as db:
            if db.execute("SELECT 1 FROM sqlite_master WHERE name='claims'").fetchone():
                owned = db.execute(
                    "SELECT 1 FROM claims WHERE resource='archive' AND expires>?", (time.time(),)
                ).fetchone()
                try:
                    pid = int((root / name).read_text()) if owned else None
                except (OSError, ValueError):
                    pid = None
                return {"running": bool(owned), "pid": pid}
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
    if not path.is_file():
        raise FileNotFoundError("File is no longer available")
    return path


def output_files(root: Path) -> list[dict]:
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
        _events_in_file(root / "backfill.log")
        + _events_in_file(root / "diarization-backfill.log")
        + _events_in_file(root / "postprocessing-backfill.log"),
        key=lambda e: e.get("time", ""),
        reverse=True,
    )[:20]


def snapshot(root: Path) -> dict:
    root = root.resolve()
    # Short read transaction gives consistent counters without taking the worker lock.
    with contextlib.closing(
        sqlite3.connect((root / "archive.sqlite3").as_uri() + "?mode=ro", uri=True, timeout=3)
    ) as db:
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
            base = Path(job["json_path"])
            paths.extend(
                base.with_suffix(ext).as_posix() for ext in (".md", ".srt", ".txt", ".json")
            )
            if base.with_suffix(".srt").as_posix() in by_path:
                job["srt_url"] = f"/transcripts/{job['job_id']}/srt"
        if job["audio_path"]:
            paths.append(job["audio_path"])
        if job.get("postprocessing_state") == "complete" and job.get("postprocessing_dir"):
            directory = Path(job["postprocessing_dir"])
            processed = (directory / "processed.md").as_posix()
            if processed in by_path:
                job["processed_path"] = processed
                job["processed_url"] = f"/processed/{job['job_id']}/preview.html"
                paths.extend([processed, (directory / "processed-with-figures.zip").as_posix()])
        if job.get("summary_state") == "complete" and job.get("summary_dir"):
            from .artifacts import artifact_path

            directory = artifact_path(SimpleNamespace(root=root), job["summary_dir"])
            if (directory / "artifact.md").is_file():
                job["summary_url"] = f"/artifacts/{job['job_id']}/summary/"
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
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header(
                "Content-Security-Policy",
                "default-src 'self'; script-src 'none'; style-src 'self' 'unsafe-inline'; img-src 'self'; object-src 'none'; base-uri 'none'; frame-ancestors 'none'"
                if generated_html
                else "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; object-src 'none'; base-uri 'none'; frame-ancestors 'none'",
            )
            if disposition:
                self.send_header("Content-Disposition", disposition)
            self.end_headers()

        def send_json(self, value, status=200):
            data = json.dumps(value, ensure_ascii=False).encode()
            self.send_headers(status, CONTENT_TYPES[".json"], len(data))
            self.wfile.write(data)

        def trusted(self):
            port = self.server.server_port
            hosts = {f"127.0.0.1:{port}", f"localhost:{port}"}
            if self.headers.get("Host") not in hosts:
                self.send_json({"error": "Use the local dashboard address"}, 403)
                return False
            origin = self.headers.get("Origin")
            if origin and origin not in {f"http://{h}" for h in hosts}:
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
                if url.path == "/api/health":
                    self.send_json(
                        {
                            "service": "tubescribe-dashboard",
                            "instance": instance,
                            "pid": os.getpid(),
                            "archive_root": str(root),
                        }
                    )
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
                    with contextlib.closing(sqlite3.connect(root / "archive.sqlite3")) as db:
                        row = db.execute(
                            "SELECT v.title,p.state,a.state,a.output_dir,a.error,a.run_pid FROM jobs j "
                            "JOIN videos v ON v.id=j.video_id JOIN postprocessings p ON p.job_id=j.id "
                            "LEFT JOIN artifacts a ON a.job_id=j.id AND a.workflow=? WHERE j.id=?",
                            (match[2], int(match[1])),
                        ).fetchone()
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
                        self.send_headers(
                            200,
                            CONTENT_TYPES.get(path.suffix, "text/plain; charset=utf-8"),
                            len(data),
                            generated_html=path.suffix == ".html",
                        )
                        self.wfile.write(data)
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
                        self.send_headers(200, CONTENT_TYPES[".html"], len(data))
                        self.wfile.write(data)
                elif match := re.fullmatch(r"/transcripts/(\d+)/srt", url.path):
                    from .srt_viewer import render_srt

                    with contextlib.closing(sqlite3.connect(root / "archive.sqlite3")) as db:
                        row = db.execute(
                            "SELECT t.json_path,v.title,v.id,p.state FROM transcripts t "
                            "JOIN jobs j ON j.id=t.job_id JOIN videos v ON v.id=j.video_id "
                            "LEFT JOIN postprocessings p ON p.job_id=j.id WHERE j.id=?",
                            (int(match[1]),),
                        ).fetchone()
                    if not row:
                        raise FileNotFoundError()
                    relative = Path(row[0]).with_suffix(".srt").as_posix()
                    path = allowed_file(root, relative)
                    data = render_srt(
                        path.read_text(encoding="utf-8"),
                        title=row[1],
                        video_id=row[2],
                        download_url="/api/file?path=" + quote(relative, safe="") + "&download=1",
                        processed_url=f"/processed/{match[1]}/preview.html"
                        if row[3] == "complete"
                        else None,
                    ).encode()
                    self.send_headers(200, CONTENT_TYPES[".html"], len(data), generated_html=True)
                    self.wfile.write(data)
                elif match := re.fullmatch(
                    r"/processed/(\d+)/(preview\.html|processed\.md|source\.srt|processed-with-figures\.zip|figures/frame_\d+\.png)",
                    url.path,
                ):
                    with contextlib.closing(sqlite3.connect(root / "archive.sqlite3")) as db:
                        row = db.execute(
                            "SELECT p.output_dir,p.source_path,v.title FROM postprocessings p JOIN jobs j ON j.id=p.job_id JOIN videos v ON v.id=j.video_id WHERE p.job_id=? AND p.state='complete'",
                            (int(match[1]),),
                        ).fetchone()
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
                        self.send_headers(
                            200, CONTENT_TYPES[".html"], len(data), generated_html=True
                        )
                        self.wfile.write(data)
                    else:
                        relative = (
                            Path(row[1]).with_suffix(".srt")
                            if filename == "source.srt"
                            else Path(row[0]) / filename
                        )
                        path = allowed_file(root, relative.as_posix())
                        data = path.read_bytes()
                        self.send_headers(
                            200,
                            CONTENT_TYPES.get(path.suffix, "text/plain; charset=utf-8"),
                            len(data),
                            disposition=f"attachment; filename*=UTF-8''{quote(path.name)}"
                            if path.suffix == ".zip"
                            else None,
                        )
                        self.wfile.write(data)
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
                            shutil.copyfileobj(handle, self.wfile)
                elif url.path in ("/", "/app.js", "/artifact.js", "/style.css"):
                    path = ASSETS / ("index.html" if url.path == "/" else url.path[1:])
                    data = path.read_bytes()
                    self.send_headers(200, CONTENT_TYPES[path.suffix], len(data))
                    self.wfile.write(data)
                else:
                    self.send_json({"error": "Not found"}, 404)
            except (FileNotFoundError, IsADirectoryError):
                self.send_json({"error": "File is not available"}, 404)
            except ValueError as error:
                self.send_json({"error": str(error)}, 400)
            except sqlite3.Error:
                self.send_json({"error": "Archive database is unavailable; refresh to retry"}, 503)
            except (BrokenPipeError, ConnectionResetError):
                pass
            except OSError:
                self.send_json({"error": "Output file is temporarily unavailable"}, 503)

        def do_POST(self):
            if not self.trusted():
                return
            match = re.fullmatch(
                r"/artifacts/(\d+)/([a-z][a-z0-9_-]{0,63})/run", urlsplit(self.path).path
            )
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
                if (
                    not 0 < length <= 1024
                    or self.headers.get("Content-Type", "").split(";")[0]
                    != "application/x-www-form-urlencoded"
                ):
                    raise ValueError("Use the artifact workflow form")
                fields = parse_qs(self.rfile.read(length).decode())
                if fields.get("token") != [instance]:
                    self.send_json({"error": "Refresh the workflow page before triggering it"}, 403)
                    return
                with children_lock:
                    store = Store(root)
                    try:
                        from .submission import Dispatcher, JobRequest, LocalProcess

                        source = inputs(store, int(match[1]), match[2])[0]
                        key = (int(match[1]), match[2])
                        with store.db:
                            store.db.execute(
                                "INSERT OR IGNORE INTO artifacts(job_id,workflow,updated_at) VALUES(?,?,?)",
                                (*key, now()),
                            )
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
                        pid = store.db.execute(
                            "SELECT pid FROM submissions WHERE id=?", (identifier,)
                        ).fetchone()[0]
                        with store.db:
                            store.db.execute(
                                "UPDATE artifacts SET run_pid=? WHERE job_id=? AND workflow=?",
                                (pid, *key),
                            )
                    finally:
                        store.db.close()
                self.send_response(303)
                self.send_header("Location", f"/artifacts/{match[1]}/{match[2]}/")
                self.send_header("Content-Length", "0")
                self.end_headers()
            except (ValueError, FileNotFoundError, KeyError) as error:
                self.send_json({"error": str(error)}, 400)
            except (OSError, sqlite3.Error, RuntimeError):
                self.send_json({"error": "Artifact worker could not start"}, 503)

    return ThreadingHTTPServer(("127.0.0.1", port), Handler)


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
            response = httpx.get(state["url"] + "/api/health", timeout=1, trust_env=False)
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
