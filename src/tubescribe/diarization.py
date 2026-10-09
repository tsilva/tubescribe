"""Pinned RTX 4090 diarization and speaker assignment, separate from speech recognition."""

from __future__ import annotations

import importlib.metadata
import math
import subprocess
import tempfile
import time
import wave
from pathlib import Path
from urllib.parse import urlparse

import httpx

from .storage import now

MODEL = "nvidia/Nemotron-3-Diarization"
REVISION = "f667ed73aee57d40cc39428eb768b4fd87a0a29e"


def validate(result):
    if result.get("model") != MODEL or result.get("revision") != REVISION:
        raise ValueError("Unexpected diarization model/revision; upgrade explicitly")
    if result.get("device") != "cuda:0" or "RTX 4090" not in result.get("gpu", ""):
        raise ValueError("Diarization requires beast-3 RTX 4090")
    for field in ("duration", "processing_seconds"):
        if (
            not isinstance(result.get(field), (int, float))
            or not math.isfinite(result[field])
            or result[field] <= 0
        ):
            raise ValueError(f"Invalid diarization {field}")
    turns = result.get("exclusive_turns")
    if not isinstance(turns, list) or not turns:
        raise ValueError("Missing diarization turns")
    previous = 0
    for turn in turns:
        start, end = turn.get("start"), turn.get("end")
        if not all(isinstance(t, (int, float)) and math.isfinite(t) for t in (start, end)):
            raise ValueError("Invalid speaker timestamps")
        if not 0 <= start < end <= result["duration"] + 0.05 or start < previous - 0.001:
            raise ValueError("Overlapping or out-of-bounds exclusive speaker turns")
        if not isinstance(turn.get("speaker"), str) or not turn["speaker"]:
            raise ValueError("Missing speaker ID")
        previous = end
    return result


def untranscribed_speaker_turns(cues, turns):
    """Keep substantial activity for a voice absent from ASR, outside existing cues."""
    represented = {cue["speaker_id"] for cue in cues}
    intervals = []
    for turn in turns:
        if turn["speaker"] in represented:
            continue
        start, end = turn["start"], turn["end"]
        for cue in cues:
            if cue["end"] <= start:
                continue
            if cue["start"] >= end:
                break
            if cue["start"] > start:
                intervals.append({"start": start, "end": cue["start"], "speaker": turn["speaker"]})
            start = max(start, cue["end"])
            if start >= end:
                break
        if start < end:
            intervals.append({"start": start, "end": end, "speaker": turn["speaker"]})

    merged = []
    for interval in intervals:
        if (
            merged
            and merged[-1]["speaker"] == interval["speaker"]
            and interval["start"] - merged[-1]["end"] <= 0.1
            and not any(
                cue["start"] < interval["start"] and cue["end"] > merged[-1]["end"] for cue in cues
            )
        ):
            merged[-1]["end"] = interval["end"]
        else:
            merged.append(dict(interval))
    # Sub-frame boundaries and very short detections do not warrant a reading turn.
    return [turn for turn in merged if turn["end"] - turn["start"] >= 0.5]


def assign_speakers(asr, prediction):
    """Retain ASR words and explicitly annotate missing voices instead of inventing text."""
    original = asr.get("asr_segments", asr["segments"])
    turns = prediction["exclusive_turns"]
    cues = []
    for segment in original:
        words = segment.get("words") or [
            {"start": segment["start"], "end": segment["end"], "word": segment["text"]}
        ]
        for word in words:
            start, end = word["start"], word["end"]
            scores = {}
            for turn in turns:
                if turn["start"] > end:
                    break
                overlap = max(0, min(end, turn["end"]) - max(start, turn["start"]))
                if start == end and turn["start"] <= start < turn["end"]:
                    overlap = 1
                if overlap:
                    scores[turn["speaker"]] = scores.get(turn["speaker"], 0) + overlap
            speaker = max(scores, key=scores.get) if scores else "UNKNOWN"
            text = word["word"].strip()
            if not text:
                continue
            if (
                not cues
                or cues[-1]["speaker_id"] != speaker
                or start - cues[-1]["end"] > 1.2
                or end - cues[-1]["start"] > 7
                or len(cues[-1]["text"]) + len(text) > 90
            ):
                cues.append(
                    {
                        "start": start,
                        "end": max(end, start + 0.001),
                        "text": text,
                        "speaker_id": speaker,
                        "words": [dict(word)],
                    }
                )
            else:
                cues[-1]["text"] += " " + text
                cues[-1]["end"] = max(end, cues[-1]["end"])
                cues[-1]["words"].append(dict(word))
    if not cues:
        raise ValueError("No ASR words available for speaker assignment")
    represented = sorted({cue["speaker_id"] for cue in cues} - {"UNKNOWN"})
    detected = {turn["speaker"] for turn in prediction.get("turns", turns)}
    missing = untranscribed_speaker_turns(cues, turns)

    def timestamp(seconds):
        milliseconds = round(seconds * 1000)
        seconds, milliseconds = divmod(milliseconds, 1000)
        minutes, seconds = divmod(seconds, 60)
        hours, minutes = divmod(minutes, 60)
        return f"{hours:02}:{minutes:02}:{seconds:02}.{milliseconds:03}"

    for turn in missing:
        cues.append(
            {
                "start": turn["start"],
                "end": turn["end"],
                "text": f"[Untranscribed speech detected at {timestamp(turn['start'])}–{timestamp(turn['end'])}; ASR supplied no words for this voice.]",
                "speaker_id": turn["speaker"],
                "words": [],
                "annotation": "untranscribed_speech",
            }
        )
    cues.sort(key=lambda cue: cue["start"])
    return {
        **asr,
        "asr_segments": original,
        "segments": cues,
        "diarization": prediction,
        "diarization_coverage": {
            "speakers_with_asr_words": represented,
            "speakers_without_asr_words": sorted(detected - set(represented)),
            "untranscribed_speaker_turns": missing,
        },
        "speaker_name_provenance": "Anonymous IDs within this video; names not inferred",
    }


class Client:
    def __init__(self, endpoint):
        url = urlparse(endpoint)
        if url.scheme != "http" or url.hostname != "127.0.0.1":
            raise ValueError("Diarization endpoint must be loopback HTTP through SSH")
        self.endpoint = endpoint.rstrip("/")

    def health(self):
        response = httpx.get(self.endpoint + "/health", timeout=10, trust_env=False)
        response.raise_for_status()
        health = response.json()
        if (
            health.get("service") != "tubescribe-diarization"
            or health.get("model") != MODEL
            or health.get("revision") != REVISION
        ):
            raise ValueError("Unexpected diarization service/model")
        if health.get("device") != "cuda:0" or "RTX 4090" not in health.get("gpu", ""):
            raise ValueError("Diarization service must use RTX 4090 CUDA")
        if not health.get("ready"):
            raise RuntimeError("Diarization model is not ready")
        return health

    def diarize(self, path):
        with (
            path.open("rb") as audio,
            httpx.Client(timeout=httpx.Timeout(7200, connect=10), trust_env=False) as client,
        ):
            response = client.post(
                self.endpoint + "/diarize",
                content=audio,
                headers={
                    "Content-Length": str(path.stat().st_size),
                    "Content-Type": "application/octet-stream",
                },
            )
            response.raise_for_status()
            return validate(response.json())


class Nemotron:
    def __init__(self):
        import torch
        from transformers import AutoModelForAudioFrameClassification, AutoProcessor

        if not torch.cuda.is_available() or "RTX 4090" not in torch.cuda.get_device_name(0):
            raise RuntimeError("Requires RTX 4090 CUDA; no CPU fallback")
        if torch.cuda.mem_get_info(0)[0] < 2 * 1024**3:
            raise RuntimeError("Wait for at least 2 GiB free GPU memory")
        torch.set_num_threads(4)
        options = {
            "revision": REVISION,
            "token": False,
            "cache_dir": str(Path.home() / ".cache/tubescribe/models"),
        }
        self.processor = AutoProcessor.from_pretrained(MODEL, **options)
        self.model = (
            AutoModelForAudioFrameClassification.from_pretrained(
                MODEL, dtype=torch.float32, **options
            )
            .to("cuda:0")
            .eval()
        )
        if any(p.device.type != "cuda" for p in self.model.parameters()):
            raise RuntimeError("Model is not fully on CUDA")
        self.identity = {
            "model": MODEL,
            "revision": REVISION,
            "backend": "transformers",
            "backend_version": importlib.metadata.version("transformers"),
            "torch_version": torch.__version__,
            "device": "cuda:0",
            "gpu": torch.cuda.get_device_name(0),
            "compute_type": "float32",
            "options": {
                "threshold": 0.5,
                "chunk_length": self.model.config.chunk_length,
                "chunk_right_context": self.model.config.chunk_right_context,
            },
            "exclusive_policy": "Highest activity probability during overlap; raw turns retained",
        }

    def release_scratch(self):
        import torch

        # Preserve resident model weights, but return unused request buffers to the
        # shared GPU so ASR and existing workloads have room for their next request.
        torch.cuda.empty_cache()

    def infer(self, path):
        import numpy as np
        import torch

        started_at, started = now(), time.perf_counter()
        with tempfile.TemporaryDirectory() as folder:
            wav = Path(folder) / "decoded.wav"
            subprocess.run(
                [
                    "ffmpeg",
                    "-nostdin",
                    "-v",
                    "error",
                    "-i",
                    str(path),
                    "-ac",
                    "1",
                    "-ar",
                    "16000",
                    "-c:a",
                    "pcm_s16le",
                    str(wav),
                ],
                check=True,
                timeout=600,
            )
            with wave.open(str(wav)) as audio:
                waveform = (
                    np.frombuffer(audio.readframes(audio.getnframes()), dtype="<i2").astype(
                        np.float32
                    )
                    / 32768
                )
        duration = len(waveform) / 16000
        if not duration or duration > 14400:
            raise ValueError("Diarization supports nonempty audio up to four hours per request")
        torch.cuda.reset_peak_memory_stats()
        torch.cuda.synchronize()
        inputs = self.processor(waveform, sampling_rate=16000).to(
            self.model.device, dtype=self.model.dtype
        )
        with torch.inference_mode():
            logits = self.model(**inputs).logits
        torch.cuda.synchronize()
        raw = self.processor.extract_speaker_dict(logits, inputs.attention_mask, threshold=0.5)[0]
        turns = [
            {
                "start": r["Start"],
                "end": min(duration, r["End"]),
                "speaker": f"Speaker {r['Speaker'] + 1}",
            }
            for r in raw
            if r["Start"] < duration
        ]
        probabilities = logits[0].sigmoid()
        active = probabilities > 0.5
        valid = inputs.attention_mask[0].bool()
        winner = probabilities.argmax(dim=-1)
        winner[~active.any(dim=-1) | ~valid] = -1
        winners = winner.cpu().numpy()
        frame = self.processor.feature_extractor.hop_length / 16000
        boundaries = np.flatnonzero(np.diff(np.r_[-2, winners, -2]))
        exclusive = [
            {
                "start": int(a) * frame,
                "end": min(duration, int(b) * frame),
                "speaker": f"Speaker {int(winners[a]) + 1}",
            }
            for a, b in zip(boundaries[:-1], boundaries[1:])
            if winners[a] >= 0 and a * frame < duration
        ]
        return validate(
            {
                **self.identity,
                "duration": duration,
                "processing_seconds": time.perf_counter() - started,
                "started_at": started_at,
                "finished_at": now(),
                "turns": turns,
                "exclusive_turns": exclusive,
                "speaker_count": len({t["speaker"] for t in turns}),
                "peak_allocated_mib": torch.cuda.max_memory_allocated() / 1024**2,
                "overlapping_speech_seconds": int(((active.sum(dim=-1) > 1) & valid).sum()) * frame,
            }
        )
