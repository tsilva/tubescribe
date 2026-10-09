"""Public YouTube indexing and conservative audio-only downloads."""

from __future__ import annotations

import json
import re
import subprocess
import sys
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
    p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    if p.returncode:
        raise YouTubeError(p.stderr[-4000:])
    if p.stderr:
        print(p.stderr.strip(), file=sys.stderr, flush=True)
    return json.loads(p.stdout)


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
    args = ["--flat-playlist", "--dump-single-json", "--skip-download"]
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


def download(root: Path, video_id: str) -> tuple[Path, dict]:
    if not ID.fullmatch(video_id):
        raise ValueError("Invalid video ID")
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
    if data.get("live_status") in ("is_live", "is_upcoming", "post_live"):
        raise YouTubeError(
            "Video is live or upcoming; wait until the completed recording is available"
        )
    path = Path(data.get("requested_downloads", [{}])[0].get("filepath") or data["_filename"])
    if not path.is_file() or path.stat().st_size == 0:
        raise YouTubeError("Downloader returned no complete audio file")
    with path.open("rb") as f:
        import os

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
