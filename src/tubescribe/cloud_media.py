"""Media-only Sandbox entrypoint. Receives bounded object capabilities, never AI keys."""

import base64
import hashlib
import io
import json
import subprocess
import sys
import tarfile
import wave
from pathlib import Path
from tempfile import TemporaryDirectory

import httpx

from .openrouter import CHUNK_SECONDS
from .speaker_continuity import RATE

MAX_SECONDS = 4 * 60 * 60
MAX_TOOL_BYTES = 64 * 1024 * 1024
MAX_MEDIA_BYTES = 1024 * 1024 * 1024


def upload(url, content):
    with httpx.Client(timeout=180, trust_env=False) as client:
        client.put(url, content=content).raise_for_status()


def upload_file(url, path):
    with httpx.Client(timeout=180, trust_env=False) as client, path.open("rb") as source:
        client.put(url, content=source, headers={"Content-Length": str(path.stat().st_size)}).raise_for_status()


def fetch_file(url, path):
    path.parent.mkdir(parents=True, exist_ok=True)
    with httpx.stream("GET", url, timeout=180, trust_env=False) as response:
        response.raise_for_status()
        with path.open("wb") as output:
            size = 0
            for block in response.iter_bytes():
                size += len(block)
                if size > MAX_MEDIA_BYTES:
                    raise ValueError("Media exceeds the transfer bound")
                output.write(block)


def prepare(request, root):
    from .apify_client import stored_file

    video = root / "source.mp4"
    fetch_file(stored_file(request["source"]), video)
    # One stored muxed file gives audio and frames the same timeline. No
    # YouTube cookies, source-stream URLs or service token enter the Sandbox.
    decoded = root / "source.wav"
    subprocess.run([
        "ffmpeg", "-nostdin", "-xerror", "-v", "error", "-i", str(video), "-map", "0:a:0",
        "-ac", "1", "-ar", str(RATE), "-c:a", "pcm_s16le", str(decoded),
    ], check=True, capture_output=True, timeout=600)
    parts = []
    with wave.open(str(decoded)) as source:
        duration = source.getnframes() / RATE
        if not 0 < duration <= MAX_SECONDS:
            raise ValueError("Selected video exceeds the four-hour media limit")
        while pcm := source.readframes(CHUNK_SECONDS * RATE):
            index = len(parts)
            upload(request["parts"][index], pcm)
            parts.append({"sha256": hashlib.sha256(pcm).hexdigest(),
                          "seconds": len(pcm) / (RATE * 2)})
    subprocess.run([
        "ffmpeg", "-nostdin", "-xerror", "-v", "error", "-i", str(video), "-map", "0:v:0",
        "-f", "null", "-",
    ], check=True, capture_output=True, timeout=600)
    from .blobs import file_digest

    upload_file(request["video"], video)
    return {"duration": duration, "parts": parts, "video_sha256": file_digest(video), "title": request.get("title"),
        "video_id": request["video_id"], "channel_id": request["channel_id"]}


def visual(request, root):
    from .postprocessing import Screenshots

    video = root / "video.webm"
    fetch_file(request["video"], video)
    output = root / "output"
    output.mkdir()
    for name, url in request["files"].items():
        from .blobs import checked_key

        fetch_file(url, output / checked_key(name))
    screenshots = Screenshots(video, output, request["duration"], request["budgets"])
    for name, value in request.get("state", {}).items():
        if name in {"calls", "searches", "full_views", "search_samples"}:
            setattr(screenshots, name, value)
    screenshots.sent_views = set(request.get("sent_views", []))
    screenshots.preview_fingerprints = {
        tuple(key): base64.b64decode(value) for key, value in request.get("fingerprints", [])
    }
    result = (screenshots.execute if request["tool"] == "video_screenshots"
              else screenshots.search)(request["arguments"])
    # State belongs to the caller's immutable tool checkpoint; no model runs here.
    (output / "cloud-result.json").write_text(json.dumps({
        "value": result, "state": {"assets": screenshots.assets, "calls": screenshots.calls,
                                    "searches": screenshots.searches,
                                    "full_views": screenshots.full_views,
                                    "search_samples": screenshots.search_samples,
                                    "sent_views": sorted(screenshots.sent_views),
                                    "preview_fingerprints": [[list(key), base64.b64encode(value).decode()]
                                                             for key, value in screenshots.preview_fingerprints.items()]},
    }))
    archive = io.BytesIO()
    with tarfile.open(fileobj=archive, mode="w:gz") as bundle:
        for path in sorted(output.rglob("*")):
            if path.is_file():
                relative = path.relative_to(output).as_posix()
                # Previously supplied frames are immutable. Return only new
                # pixels and current ledgers instead of a growing full copy.
                if relative.startswith("figures/") and relative in request["files"]:
                    continue
                bundle.add(path, arcname=relative)
    if archive.tell() > MAX_TOOL_BYTES:
        raise ValueError("Media tool output exceeds the bounded transfer size")
    upload(request["archive"], archive.getvalue())
    return {"sha256": hashlib.sha256(archive.getvalue()).hexdigest()}


def main():
    # No secrets or signed URLs enter argv, files or logs.
    raw = sys.stdin.buffer.read(2 * 1024 * 1024 + 1)
    if len(raw) > 2 * 1024 * 1024:
        raise ValueError("Media request exceeds the bounded input size")
    request = json.loads(raw)
    try:
        with TemporaryDirectory(prefix="tubescribe-media-") as directory:
            result = (prepare if request["operation"] == "prepare" else visual)(
                request, Path(directory))
        upload(request["result"], json.dumps({"ok": True, **result}).encode())
    except Exception:
        upload(request["result"], b'{"ok":false,"error":"Media preparation failed"}')
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
