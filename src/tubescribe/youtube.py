"""Public YouTube indexing and conservative audio/video downloads."""

from __future__ import annotations

import json
import os
import re
import signal
import subprocess
import sys
from functools import wraps
from pathlib import Path

TABS = {"upload": "videos", "short": "shorts", "stream": "streams"}
ID = re.compile(r"^[A-Za-z0-9_-]{11}$")
CHANNEL = re.compile(r"^UC[A-Za-z0-9_-]{22}$")


class YouTubeError(RuntimeError):
    def __init__(self, message):
        super().__init__(message)
        lower = message.lower()
        self.kind = (
            "rate_limit"
            if any(
                x in lower
                for x in (
                    "429",
                    "too many requests",
                    "confirm you",
                    "not a bot",
                    "sign in",
                    "captcha",
                )
            )
            else "inaccessible"
            if any(
                x in lower
                for x in ("private", "unavailable", "removed", "members-only", "not available")
            )
            else "download"
        )


def run(args: list[str], timeout=900):
    cmd = [
        sys.executable,
        "-m",
        "yt_dlp",
        "--ignore-config",
        "--js-runtimes",
        "node",
        "--socket-timeout",
        "30",
        "--retries",
        "2",
        "--fragment-retries",
        "2",
        "--extractor-retries",
        "2",
        "--retry-sleep",
        "exp=5:30",
        "--sleep-requests",
        "1",
        *args,
    ]
    process = subprocess.Popen(
        cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, start_new_session=True
    )
    try:
        stdout, stderr = process.communicate(timeout=timeout)
    except BaseException:
        # Stop the downloader and its ffmpeg children before releasing the archive lock.
        try:
            os.killpg(process.pid, signal.SIGTERM)
            process.communicate(timeout=5)
        except subprocess.TimeoutExpired:
            os.killpg(process.pid, signal.SIGKILL)
            process.communicate()
        except ProcessLookupError:
            process.wait()
        raise
    if process.returncode:
        raise YouTubeError(stderr[-4000:])
    if stderr:
        print(stderr.strip(), file=sys.stderr, flush=True)
    return json.loads(stdout)


def resolve(url: str) -> dict:
    if not re.match(
        r"^https://(www\.)?youtube\.com/(?:@[^/?#]+|channel/UC[A-Za-z0-9_-]{22})/?$", url
    ):
        raise ValueError("Use a YouTube channel handle or /channel/UC… URL")
    info = run(
        [
            "--flat-playlist",
            "--dump-single-json",
            "--skip-download",
            "--playlist-end",
            "1",
            url.rstrip("/") + "/videos",
        ]
    )
    cid = info.get("channel_id") or info.get("id")
    if not cid or not CHANNEL.fullmatch(cid):
        raise ValueError("YouTube did not resolve a stable channel ID")
    return {
        "id": cid,
        "title": info.get("channel") or info.get("uploader") or info["title"],
        "url": f"https://www.youtube.com/channel/{cid}",
    }


def discover(channel_id: str, kind: str, limit=None) -> list[dict]:
    args = [
        "--flat-playlist",
        "--dump-single-json",
        "--skip-download",
        "--extractor-args",
        "youtubetab:approximate_date",
    ]
    if limit:
        args += ["--playlist-end", str(limit)]
    try:
        data = run(
            [*args, f"https://www.youtube.com/channel/{channel_id}/{TABS[kind]}"], timeout=1800
        )
    except YouTubeError as e:
        if f"This channel does not have a {TABS[kind]} tab" in str(e):
            return []
        raise
    videos = []
    for e in data.get("entries", []):
        if not ID.fullmatch(e.get("id", "")):
            continue
        if e.get("live_status") in ("is_live", "is_upcoming", "post_live"):
            continue
        videos.append(
            {
                "id": e["id"],
                "title": e.get("title") or e["id"],
                "url": f"https://www.youtube.com/watch?v={e['id']}",
                "duration": e.get("duration"),
                "published_at": str(e["timestamp"]) if e.get("timestamp") else e.get("upload_date"),
            }
        )
    return videos


def shared_backoff(function):
    @wraps(function)
    def invoke(root, *args, **kwargs):
        try:
            return function(root, *args, **kwargs)
        except YouTubeError as error:
            if error.kind == "rate_limit":
                from .execution import defer_downloads

                defer_downloads(root)
            raise

    return invoke


@shared_backoff
def download(root: Path, video_id: str, *, concurrent_fragments=1) -> tuple[Path, dict]:
    if not ID.fullmatch(video_id):
        raise ValueError("Invalid video ID")
    if concurrent_fragments not in (1, 2):
        raise ValueError("Audio downloads are limited to one or two concurrent fragments")
    metadata = details(video_id)
    if metadata.get("live_status") in ("is_live", "is_upcoming", "post_live"):
        raise YouTubeError("Video is live or upcoming; wait for the completed recording")
    folder = root / "audio"
    folder.mkdir(parents=True, exist_ok=True)
    data = run(
        [
            "--no-playlist",
            "--dump-json",
            "--no-simulate",
            "--format",
            "bestaudio[protocol^=m3u8]/bestaudio",
            "--concurrent-fragments",
            str(concurrent_fragments),
            "--abort-on-unavailable-fragments",
            "--sleep-interval",
            "3",
            "--max-sleep-interval",
            "7",
            "-o",
            str(folder / f"{video_id}.%(ext)s"),
            f"https://www.youtube.com/watch?v={video_id}",
        ]
    )
    if data.get("live_status") in ("is_live", "is_upcoming", "post_live"):
        raise YouTubeError(
            "Video is live or upcoming; wait until the completed recording is available"
        )
    path = Path(data.get("requested_downloads", [{}])[0].get("filepath") or data["_filename"])
    if not path.is_file() or path.stat().st_size == 0:
        raise YouTubeError("Downloader returned no complete audio file")
    with path.open("rb") as f:
        os.fsync(f.fileno())
    return path, data


def details(video_id: str) -> dict:
    if not ID.fullmatch(video_id):
        raise ValueError("Invalid video ID")
    return run(
        [
            "--no-playlist",
            "--dump-single-json",
            "--skip-download",
            f"https://www.youtube.com/watch?v={video_id}",
        ]
    )


@shared_backoff
def download_video(root: Path, video_id: str) -> tuple[Path, dict]:
    """Cache a modest video stream for visual inspection, independently of ASR audio."""
    if not ID.fullmatch(video_id):
        raise ValueError("Invalid video ID")
    metadata = details(video_id)
    if metadata.get("live_status") in ("is_live", "is_upcoming", "post_live"):
        raise YouTubeError("Video is live or upcoming; wait for the completed recording")
    folder = root / "videos"
    folder.mkdir(parents=True, exist_ok=True)
    result = run(
        [
            "--no-playlist",
            "--dump-json",
            "--no-simulate",
            "--format",
            "bestvideo[height<=720][protocol^=m3u8]/bestvideo[height<=720]/best[height<=720]",
            "--concurrent-fragments",
            "1",
            "--abort-on-unavailable-fragments",
            "--sleep-interval",
            "3",
            "--max-sleep-interval",
            "7",
            "-o",
            str(folder / f"{video_id}.%(ext)s"),
            f"https://www.youtube.com/watch?v={video_id}",
        ]
    )
    if result.get("live_status") in ("is_live", "is_upcoming", "post_live"):
        raise YouTubeError("Recording has not finished")
    path = Path(result.get("requested_downloads", [{}])[0].get("filepath") or result["_filename"])
    if not path.is_file() or not path.stat().st_size:
        raise YouTubeError("Downloader returned no complete video")
    return path, result
