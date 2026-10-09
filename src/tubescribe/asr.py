"""Loopback HTTP client and strict transcript validation."""

from __future__ import annotations

import math
from pathlib import Path
from urllib.parse import urlparse

import httpx


def validate(result: dict):
    for k in (
        "language",
        "model",
        "revision",
        "backend",
        "backend_version",
        "started_at",
        "finished_at",
    ):
        if not isinstance(result.get(k), str) or not result[k]:
            raise ValueError(f"ASR response lacks {k}")
    for k in ("duration", "processing_seconds"):
        if (
            not isinstance(result.get(k), (int, float))
            or not math.isfinite(result[k])
            or result[k] <= 0
        ):
            raise ValueError(f"Invalid ASR {k}")
    if not result.get("segments"):
        raise ValueError("ASR returned no speech; audio retained for inspection")
    previous = 0.0
    for s in result["segments"]:
        start, end = s.get("start"), s.get("end")
        if (
            not isinstance(start, (int, float))
            or not isinstance(end, (int, float))
            or not math.isfinite(start)
            or not math.isfinite(end)
        ):
            raise ValueError("Non-finite ASR timestamps")
        if not 0 <= start < end <= result["duration"] + 1.0 or start < previous - 0.05:
            raise ValueError("ASR timestamps are out of order or audio bounds")
        if not isinstance(s.get("text"), str) or not s["text"].strip():
            raise ValueError("ASR segment lacks text")
        previous = end
    return result


class Client:
    def __init__(self, endpoint: str):
        url = urlparse(endpoint)
        if url.scheme != "http" or url.hostname != "127.0.0.1":
            raise ValueError("ASR endpoint must use http://127.0.0.1 through SSH")
        self.endpoint = endpoint.rstrip("/")

    def health(self):
        r = httpx.get(self.endpoint + "/health", timeout=10, trust_env=False)
        r.raise_for_status()
        if r.json().get("service") != "tubescribe-asr":
            raise RuntimeError("Forwarded endpoint is not TubeScribe ASR")
        health = r.json()
        if health.get("device") != "cuda" or "RTX 4090" not in health.get("gpu_name", ""):
            raise RuntimeError(
                "ASR must run on beast-3 RTX 4090 with CUDA; CPU fallback is disabled"
            )
        return health

    def transcribe(self, path: Path):
        with (
            path.open("rb") as f,
            httpx.Client(timeout=httpx.Timeout(7200, connect=10), trust_env=False) as client,
        ):
            r = client.post(
                self.endpoint + "/transcribe",
                files={"audio": (path.name, f, "application/octet-stream")},
            )
            r.raise_for_status()
            return validate(r.json())
