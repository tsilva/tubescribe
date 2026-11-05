"""Durable media jobs: start once, release the function, collect and stop later."""

import asyncio
import hashlib
import io
import json
import os
import re
import subprocess
import tarfile
import time

from .agent_gateway import DurableRunPending
from .blobs import atomic_write, checked_key
from .cloud_media import MAX_SECONDS, MAX_TOOL_BYTES
from .openrouter import CHUNK_SECONDS


def signed(store, operation, key):
    from .s3 import S3Artifacts

    if not isinstance(store.blobs, S3Artifacts):
        raise ValueError("Cloud media requires the configured S3 archive")
    return store.blobs.client.generate_presigned_url(
        operation, Params={"Bucket": store.blobs.bucket, "Key": checked_key(key)}, ExpiresIn=2100)


async def start_box(name, request):
    from vercel import sandbox

    image = os.environ.get("TUBESCRIBE_MEDIA_IMAGE", "")
    snapshot = os.environ.get("TUBESCRIBE_MEDIA_SNAPSHOT", "")
    if bool(image) == bool(snapshot):
        raise ValueError("Configure exactly one reviewed media image or clean snapshot")
    if image and not re.fullmatch(r"[a-zA-Z0-9./_-]+@sha256:[a-f0-9]{64}", image):
        raise ValueError("Configure an immutable reviewed TubeScribe media image")
    if snapshot and not re.fullmatch(r"snap_[a-zA-Z0-9]+", snapshot):
        raise ValueError("Configure an immutable clean media snapshot")
    # Named creation permits recovery after losing the create acknowledgement.
    try:
        box = await sandbox.get_sandbox(name=name)
    except Exception as error:
        if getattr(error, "status_code", getattr(error, "status", None)) != 404:
            raise
        box = await sandbox.create_sandbox(
            name=name, **({"source": sandbox.SnapshotSource(snapshot_id=snapshot)} if snapshot else {"image": image}),
            persistent=False, execution_time_limit=1800,
            region="fra1", resources=sandbox.SandboxResources(vcpus=1),
        )
    try:
        process = await box.create_process(
            "/app/.venv/bin/python" if snapshot else "python", ["-m", "tubescribe.cloud_media"], cwd="/app",
            stdin=subprocess.PIPE,
            kill_after=1740, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        # Object capabilities travel only over stdin, avoiding Linux's per-env
        # size limit and keeping signed URLs out of process arguments and files.
        await process.stdin.send(json.dumps(request).encode())
        await process.stdin.aclose()
    except BaseException:
        await box.stop()
        raise


async def stop_box(name):
    from vercel import sandbox

    try:
        box = await sandbox.get_sandbox(name=name)
        if box.status not in {"stopped", "failed", "destroyed"}:
            await box.stop()
    except sandbox.SandboxApiError as error:
        if error.status_code != 404:
            raise


def media_job(store, key, request):
    """Only names and timestamps are persisted; signed capabilities remain transient."""
    control_key = key + "/control.json"
    control = json.loads(store.read_key(control_key)) if store.exists(control_key) else None
    if control and store.exists(control["result_key"]):
        result = json.loads(store.read_key(control["result_key"]))
        if control and not control.get("stopped"):
            asyncio.run(stop_box(control["name"]))
            control["stopped"] = True
            store.blobs.put(control_key, json.dumps(control).encode())
        if result.get("ok"):
            return result
        control["deadline"] = 0
    if control:
        if time.time() > control["deadline"]:
            if not control.get("stopped"):
                asyncio.run(stop_box(control["name"]))
            if control["attempt"] >= 3:
                raise ValueError("Media preparation exhausted three safe attempts")
        else:
            raise DurableRunPending("Media preparation is running")
    attempt = control["attempt"] + 1 if control else 1
    name = "ts-media-" + hashlib.sha256(f"{key}/{attempt}".encode()).hexdigest()[:24]
    result_key = key + f"/attempt-{attempt}/result.json"
    control = {"name": name, "deadline": time.time() + 1800, "attempt": attempt,
               "result_key": result_key}
    store.blobs.put(control_key, json.dumps(control).encode())
    try:
        payload = request() if callable(request) else request
        asyncio.run(start_box(name, {**payload, "result": signed(store, "put_object", result_key)}))
    except Exception:
        # Keep the bounded intent: never create duplicate media machines after
        # an ambiguous start. The hard session deadline is the final cleanup.
        raise DurableRunPending("Media start acknowledgement unavailable; intent retained") from None
    raise DurableRunPending("Media preparation started")


def prepare(store, job, submission):
    prefix = f"transcripts/{job['channel_id']}/{job['video_id']}/{job['profile']}/cloud-media"
    manifest_key = prefix + "/manifest.json"
    if store.exists(manifest_key):
        return json.loads(store.read_key(manifest_key))
    key = f"cloud/media/{submission}/prepare"
    parts = [prefix + f"/{i:06d}.pcm" for i in range(MAX_SECONDS // CHUNK_SECONDS)]
    result = media_job(store, key, lambda: {
        "operation": "prepare", "video_id": job["video_id"], "channel_id": job["channel_id"],
        "parts": [signed(store, "put_object", part) for part in parts],
        "video": signed(store, "put_object", prefix + "/video.webm"),
    })
    if result.get("video_id") != job["video_id"] or result.get("channel_id") != job["channel_id"]:
        raise ValueError("Prepared media differs from the selected video")
    expected, duration = job.get("duration"), result["duration"]
    if expected and abs(duration - expected) > max(5, expected * .01):
        raise ValueError("Decoded audio duration differs from video metadata")
    result["parts"] = [{**part, "key": parts[i]} for i, part in enumerate(result["parts"])]
    result["video_key"] = prefix + "/video.webm"
    store.blobs.put(manifest_key, json.dumps(result).encode())
    return result


class RemoteScreenshots:
    """The existing visual tools run in short media-only Sandboxes, with cached outputs."""

    def __init__(self, store, media, output, duration, budgets):
        self.store, self.media, self.output = store, media, output
        self.duration, self.budgets = duration, budgets
        self.assets, self.calls, self.searches = {}, [], []
        self.full_views = self.search_samples = 0
        self.sent_views, self.preview_fingerprints = [], []

    @property
    def issued_assets(self):
        issued = {image for call in self.calls for image in call["image_ids"]}
        return {key: value for key, value in self.assets.items() if key in issued}

    def execute(self, arguments):
        return self.tool("video_screenshots", arguments)

    def search(self, arguments):
        return self.tool("video_search", arguments)

    def tool(self, name, arguments):
        signature = hashlib.sha256(json.dumps({
            "tool": name, "arguments": arguments, "prior": self.calls,
            "searches": self.searches,
        }, sort_keys=True).encode()).hexdigest()
        prefix = self.output.relative_to(self.store.root).as_posix()
        # Transport checkpoints stay outside the publication and its materialized
        # prefix, so model-step retries do not download or publish every tool tar.
        key = "cloud/media-tools/" + hashlib.sha256(prefix.encode()).hexdigest() + "/" + signature

        def request():
            files = {}
            # Only media state; capabilities are generated only for a new job.
            for path in self.output.rglob("*"):
                relative = path.relative_to(self.output).as_posix()
                if path.is_file() and (relative.startswith("figures/") or relative in {
                    "assets.json", "tool-calls.json", "searches.json",
                }):
                    ref = self.store.blobs.put_file(prefix + "/" + relative, path)
                    files[relative] = signed(self.store, "get_object", ref.key)
            return {
                "operation": "visual", "tool": name, "arguments": arguments,
                "duration": self.duration, "budgets": self.budgets, "files": files,
                "state": {"calls": self.calls, "searches": self.searches,
                          "full_views": self.full_views, "search_samples": self.search_samples},
                "sent_views": self.sent_views, "fingerprints": self.preview_fingerprints,
                "video": signed(self.store, "get_object", self.media["video_key"]),
                "archive": signed(self.store, "put_object", key + "/output.tar.gz"),
            }

        result = media_job(self.store, key, request)
        raw = self.store.read_key(key + "/output.tar.gz", result["sha256"])
        if len(raw) > MAX_TOOL_BYTES:
            raise ValueError("Media tool checkpoint exceeds the transfer bound")
        with tarfile.open(fileobj=io.BytesIO(raw), mode="r:gz") as bundle:
            total = 0
            for member in bundle.getmembers():
                if not member.isfile() or member.size < 0:
                    raise ValueError("Invalid media checkpoint member")
                total += member.size
                if total > MAX_TOOL_BYTES:
                    raise ValueError("Expanded media checkpoint exceeds the bound")
                target = self.output / checked_key(member.name)
                if not target.resolve().is_relative_to(self.output.resolve()):
                    raise ValueError("Media checkpoint escapes the output")
                atomic_write(target, bundle.extractfile(member).read())
        value = json.loads((self.output / "cloud-result.json").read_bytes())
        for field, content in value["state"].items():
            setattr(self, field, content)
        return value["value"]
