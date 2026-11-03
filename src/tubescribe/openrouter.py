"""OpenRouter speech adapter and isolated proxy-launched HTTP client."""

import base64
import json
import math
import os
import ssl
import subprocess
import sys
import time
import wave
from hashlib import sha256
from pathlib import Path
from tempfile import TemporaryDirectory
from urllib.parse import urlsplit

import httpx

from .asr import validate
from .blobs import file_digest
from .execution import assert_owned
from .repository import now

MODEL = "nvidia/nemotron-3.5-transcribe"
PLACEHOLDER = "tubescribe_openrouter_proxy_placeholder"
PRICE_PER_SECOND = 0.0000333
CHUNK_SECONDS = 480
MAX_REQUEST_BYTES = 23 * 1024 * 1024
REJECTIONS = {400, 401, 402, 403, 404, 413, 415, 422, 429}


def launcher():
    path = Path(os.environ.get("TUBESCRIBE_OPENROUTER_LAUNCHER", ""))
    if not path.is_absolute() or not path.is_file() or not os.access(path, os.X_OK):
        raise ValueError("Configure an executable TubeScribe OpenRouter proxy launcher on the Mac")
    return path


def proxy_request(payload):
    if os.environ.get("OPENROUTER_API_KEY") != PLACEHOLDER:
        raise ValueError("The TubeScribe proxy placeholder is required")
    proxy = os.environ.get("HTTPS_PROXY") or os.environ.get("https_proxy")
    endpoint = urlsplit(proxy or "")
    ca = os.environ.get("SSL_CERT_FILE")
    if (
        endpoint.scheme not in {"http", "https"}
        or endpoint.hostname not in {"127.0.0.1", "localhost", "::1"}
        or not ca
    ):
        raise ValueError("The reviewed loopback proxy and process-local CA are required")
    request = json.loads(payload)
    audio = base64.b64decode(request["input_audio"]["data"], validate=True)
    with httpx.Client(
        proxy=proxy,
        verify=ssl.create_default_context(cafile=ca),
        trust_env=False,
        timeout=httpx.Timeout(75, connect=10),
    ) as client:
        response = client.post(
            "https://openrouter.ai/api/v1/audio/transcriptions",
            files={"file": ("speech.wav", audio, "audio/wav")},
            data={
                "model": request["model"],
                "response_format": request["response_format"],
                "timestamp_granularities[]": request["timestamp_granularities"],
            },
            headers={"Authorization": "Bearer " + PLACEHOLDER},
        )
        if response.status_code != 200:
            return {"status": response.status_code}
        return {
            "status": 200,
            "generation_id": response.headers.get("X-Generation-Id"),
            "response": response.json(),
        }


def call(path, audio):
    payload = json.dumps(
        {
            "model": MODEL,
            "input_audio": {"data": base64.b64encode(audio).decode("ascii"), "format": "wav"},
            "response_format": "verbose_json",
            "timestamp_granularities": ["word"],
        }
    ).encode()
    if len(payload) > MAX_REQUEST_BYTES:
        raise ValueError("Audio request exceeds the configured upload bound")
    environment = {
        name: os.environ[name]
        for name in ("PATH", "HOME", "TMPDIR", "USER", "LOGNAME")
        if name in os.environ
    }
    result = subprocess.run(
        [str(path)], input=payload, capture_output=True, timeout=180, env=environment
    )
    if result.returncode:
        raise ValueError("OpenRouter proxy request did not return a verified outcome")
    return json.loads(result.stdout)


def cached_response(store, key, path, audio):
    response_key = key + ".json"
    pending_key = key + ".pending"
    digest = sha256(audio).hexdigest()
    unknown = "OpenRouter request outcome unknown. Check billing before clearing its checkpoint"
    with store.db:
        assert_owned(store.db, store.root)
        if store.exists(response_key):
            result = json.loads(store.read_key(response_key))
            if result["audio_sha256"] != digest:
                raise ValueError("Decoded audio changed since the saved OpenRouter request")
            return result
        if store.exists(pending_key):
            raise ValueError(unknown)
        # Record intent before sending. A lost response must not cause a second charge.
        store.blobs.put(pending_key, b"request outcome pending\n")
    try:
        result = call(path, audio)
    except (ValueError, OSError, subprocess.SubprocessError):
        raise ValueError(unknown) from None
    if result.get("status") in REJECTIONS:
        store.blobs.delete(pending_key)
        raise ValueError(f"OpenRouter rejected the transcription request ({result['status']})")
    if result.get("status") != 200:
        raise ValueError(unknown)
    result["audio_sha256"] = digest
    store.blobs.put(response_key, json.dumps(result).encode())
    store.blobs.delete(pending_key)
    return result


def cues(response, offset, duration, first_speaker=1):
    words = response.get("words")
    if words == [] and response.get("text") == "":
        return []
    if not isinstance(words, list) or not words:
        raise ValueError("OpenRouter returned no timestamped words")
    labels = {}
    segments = []
    previous = 0.0
    for word in words:
        start, end = word.get("start"), word.get("end")
        text = word.get("word")
        speaker = word.get("speaker_label")
        if speaker is None:
            speaker = word.get("speaker")
        if (
            not isinstance(text, str)
            or not text.strip()
            or not isinstance(start, (int, float))
            or not isinstance(end, (int, float))
            or not previous <= start <= end <= duration + 0.1
            or start >= duration
        ):
            raise ValueError("OpenRouter returned invalid words or timestamps")
        previous = start
        start, end = start + offset, min(duration, max(end, start + 0.001)) + offset
        label = None
        if isinstance(speaker, (int, str)) and str(speaker).strip():
            label = labels.setdefault(str(speaker), f"Speaker {first_speaker + len(labels)}")
        text = text.strip()
        timed_word = {"word": text, "start": start, "end": end}
        confidence = word.get("confidence")
        if isinstance(confidence, (float, int)) and 0 <= confidence <= 1:
            timed_word["probability"] = confidence
        last = segments[-1] if segments else None
        if (
            last
            and last["speaker_id"] == label
            and start - last["end"] <= 1.2
            and end - last["start"] <= 7
            and len(last["text"]) + len(text) < 90
        ):
            last["end"] = max(end, last["end"])
            last["text"] += " " + text
            last["words"].append(timed_word)
        else:
            segments.append(
                {
                    "start": start,
                    "end": end,
                    "text": text,
                    "speaker_id": label,
                    "words": [timed_word],
                }
            )
    return segments


class OpenRouter:
    def health(self):
        return {
            "model": MODEL,
            "revision": "provider-managed",
            "backend": "openrouter",
            "backend_version": "audio-transcriptions-v1",
            "decoding": {"chunk_seconds": CHUNK_SECONDS, "timestamp_granularities": ["word"]},
        }

    def transcribe(self, store, job, path):
        command = launcher()
        started_at, start = now(), time.monotonic()
        prefix = f"transcripts/{job['channel_id']}/{job['video_id']}/{job['profile']}/openrouter"
        source_key = prefix + "/source.sha256"
        source = file_digest(path).encode()
        if store.exists(source_key):
            if store.read_key(source_key) != source:
                raise ValueError("Audio differs from the saved OpenRouter request source")
        else:
            store.blobs.put(source_key, source)
        segments, generations, languages, costs = [], [], [], []
        offset = 0.0
        first_speaker = 1
        with TemporaryDirectory(prefix="openrouter-", dir=store.root) as scratch:
            subprocess.run(
                [
                    "ffmpeg",
                    "-nostdin",
                    "-v",
                    "error",
                    "-i",
                    str(path),
                    "-map",
                    "0:a:0",
                    "-ac",
                    "1",
                    "-ar",
                    "16000",
                    "-c:a",
                    "pcm_s16le",
                    "-f",
                    "segment",
                    "-segment_time",
                    str(CHUNK_SECONDS),
                    "-segment_format",
                    "wav",
                    str(Path(scratch) / "%06d.wav"),
                ],
                check=True,
                capture_output=True,
                timeout=600,
            )
            chunks = []
            for chunk in sorted(Path(scratch).glob("*.wav")):
                with wave.open(str(chunk)) as audio:
                    duration = audio.getnframes() / audio.getframerate()
                if not 0 < duration <= CHUNK_SECONDS + 1:
                    raise ValueError("Decoded audio part is outside the request limits")
                chunks.append((chunk, duration))
            total = sum(duration for _, duration in chunks)
            expected = job.get("duration")
            if not total or (expected and abs(total - expected) > max(5, expected * 0.01)):
                raise ValueError("Decoded audio duration differs from the selected video")
            for part, (chunk, duration) in enumerate(chunks):
                item = cached_response(store, f"{prefix}/{part:06d}", command, chunk.read_bytes())
                response = item["response"]
                usage = response.get("usage") or {}
                cost = usage.get("cost")
                costs.append(
                    cost
                    if isinstance(cost, (int, float)) and math.isfinite(cost) and cost >= 0
                    else None
                )
                part_cues = cues(response, offset, duration, first_speaker)
                first_speaker += len({cue["speaker_id"] for cue in part_cues if cue["speaker_id"]})
                segments.extend(part_cues)
                generations.append(item.get("generation_id"))
                if isinstance(response.get("language"), str) and response["language"]:
                    languages.append(response["language"])
                offset += duration
        return validate(
            {
                **self.health(),
                "segments": segments,
                "duration": offset,
                "language": languages[0] if languages else "und",
                "processing_seconds": time.monotonic() - start,
                "started_at": started_at,
                "finished_at": now(),
                "generation_ids": generations,
                "cost": sum(costs) if costs and all(cost is not None for cost in costs) else None,
                "speaker_name_provenance": "Provider speaker labels are anonymous and scoped to each audio part",
            }
        )


def main():
    try:
        payload = sys.stdin.buffer.read(MAX_REQUEST_BYTES + 1)
        if not payload or len(payload) > MAX_REQUEST_BYTES:
            raise ValueError("Invalid request size")
        # Only the worker's fixed speech request may pass through this client.
        request = json.loads(payload)
        if request.get("model") != MODEL or "input_audio" not in request:
            raise ValueError("Invalid speech request")
        print(json.dumps(proxy_request(payload)))
    except Exception:
        print("OpenRouter proxy transport failed", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
