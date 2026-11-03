"""Loopback HTTP client and strict transcript validation."""

from __future__ import annotations

import math
from pathlib import Path

import httpx

from .inference import EndpointPolicy, HardwarePolicy, health_request


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
        # Concurrent provider speaker turns can overlap; their starts remain ordered.
        previous = start if result.get("backend") == "openrouter" else end
    return result


class Client:
    def __init__(
        self,
        endpoint: str,
        *,
        policy=EndpointPolicy(),
        hardware=HardwarePolicy(),
        headers=None,
        transport=None,
    ):
        self.endpoint = policy.check(endpoint)
        self.hardware = hardware
        self.headers = headers or {}
        self.transport = transport

    def health(self):
        r = health_request(self.endpoint, headers=self.headers, transport=self.transport)
        r.raise_for_status()
        if r.json().get("service") != "tubescribe-asr":
            raise RuntimeError("Forwarded endpoint is not TubeScribe ASR")
        health = r.json()
        try:
            self.hardware.check(health.get("device"), health.get("gpu_name"))
        except ValueError as error:
            raise RuntimeError(str(error)) from error
        return health

    def transcribe(self, path: Path):
        with (
            path.open("rb") as f,
            httpx.Client(
                timeout=httpx.Timeout(7200, connect=10),
                trust_env=False,
                headers=self.headers,
                transport=self.transport,
            ) as client,
        ):
            r = client.post(
                self.endpoint + "/transcribe",
                files={"audio": (path.name, f, "application/octet-stream")},
            )
            r.raise_for_status()
            return validate(r.json())
