"""Test ungated Nemotron 3 on the RTX 4090; retain overlap and exclusive speaker turns."""

import argparse
import importlib.metadata
import json
import os
import socket
import time
import wave
from pathlib import Path

os.environ["HF_HUB_DISABLE_TELEMETRY"] = "1"
MODEL = "nvidia/Nemotron-3-Diarization"
REVISION = "f667ed73aee57d40cc39428eb768b4fd87a0a29e"


def main():
    import numpy as np
    import torch
    from transformers import AutoModelForAudioFrameClassification, AutoProcessor

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("audio", type=Path)
    parser.add_argument("output", type=Path)
    parser.add_argument("--control", type=Path, help="Optional known-one-speaker WAV")
    args = parser.parse_args()
    if not torch.cuda.is_available() or "RTX 4090" not in torch.cuda.get_device_name(0):
        raise RuntimeError("Requires beast-3 RTX 4090 CUDA; no CPU fallback")
    if torch.cuda.mem_get_info(0)[0] < 2 * 1024**3:
        raise RuntimeError("Less than 2 GiB free VRAM; wait for GPU headroom")
    torch.set_num_threads(4)
    start = time.perf_counter()
    options = {
        "revision": REVISION,
        "token": False,
        "cache_dir": str(Path.home() / ".cache/tubescribe/models"),
    }
    processor = AutoProcessor.from_pretrained(MODEL, **options)
    model = (
        AutoModelForAudioFrameClassification.from_pretrained(MODEL, dtype=torch.float32, **options)
        .to("cuda:0")
        .eval()
    )
    if any(parameter.device.type != "cuda" for parameter in model.parameters()):
        raise RuntimeError("Model was not fully placed on CUDA")
    load_seconds = time.perf_counter() - start
    frame_seconds = (
        processor.feature_extractor.hop_length / processor.feature_extractor.sampling_rate
    )

    def infer(path, label):
        with wave.open(str(path)) as audio:
            if (audio.getnchannels(), audio.getframerate(), audio.getsampwidth()) != (1, 16000, 2):
                raise ValueError("Provide mono 16 kHz PCM16 WAV")
            waveform = (
                np.frombuffer(audio.readframes(audio.getnframes()), dtype="<i2").astype(np.float32)
                / 32768.0
            )
        torch.cuda.reset_peak_memory_stats()
        torch.cuda.synchronize()
        started = time.perf_counter()
        inputs = processor(waveform, sampling_rate=16000).to(model.device, dtype=model.dtype)
        with torch.inference_mode():
            logits = model(**inputs).logits
        torch.cuda.synchronize()
        duration = len(waveform) / 16000
        raw = processor.extract_speaker_dict(logits, inputs.attention_mask, threshold=0.5)[0]
        turns = [
            {
                "start": row["Start"],
                "end": min(duration, row["End"]),
                "speaker": f"speaker_{row['Speaker']}",
            }
            for row in raw
            if row["Start"] < duration
        ]
        probabilities = logits[0].sigmoid()
        active = probabilities > 0.5
        valid = inputs.attention_mask[0].bool()
        winner = probabilities.argmax(dim=-1)
        winner[~active.any(dim=-1) | ~valid] = -1
        winners = winner.cpu().numpy()
        # Resolve simultaneous voices for word assignment using the highest model probability.
        # The original overlapping turns are preserved separately.
        boundaries = np.flatnonzero(np.diff(np.r_[-2, winners, -2]))
        exclusive = [
            {
                "start": int(a) * frame_seconds,
                "end": min(duration, int(b) * frame_seconds),
                "speaker": f"speaker_{int(winners[a])}",
            }
            for a, b in zip(boundaries[:-1], boundaries[1:])
            if winners[a] >= 0 and a * frame_seconds < duration
        ]
        result = {
            "label": label,
            "duration": duration,
            "processing_seconds": time.perf_counter() - started,
            "peak_allocated_mib": torch.cuda.max_memory_allocated() / 1024**2,
            "options": {
                "threshold": 0.5,
                "frame_seconds": frame_seconds,
                "chunk_length": model.config.chunk_length,
                "chunk_right_context": model.config.chunk_right_context,
            },
            "overlapping_speech_seconds": int(((active.sum(dim=-1) > 1) & valid).sum())
            * frame_seconds,
            "turns": turns,
            "exclusive_turns": exclusive,
        }
        print(
            json.dumps(
                {
                    "completed": label,
                    "seconds": result["processing_seconds"],
                    "speakers": sorted({turn["speaker"] for turn in turns}),
                }
            ),
            flush=True,
        )
        return result

    result = infer(args.audio, "nemotron_automatic")
    controls = [infer(args.control, "jordi_monologue_control")] if args.control else []
    args.output.write_text(
        json.dumps(
            {
                "model": MODEL,
                "revision": REVISION,
                "backend": "transformers",
                "backend_version": importlib.metadata.version("transformers"),
                "torch_version": torch.__version__,
                "host": socket.gethostname(),
                "device": "cuda:0",
                "gpu": torch.cuda.get_device_name(0),
                "compute_type": "float32",
                "duration": result["duration"],
                "model_load_seconds": load_seconds,
                "exclusive_policy": "Highest activity probability during overlap; raw overlapping turns retained",
                "runs": [result],
                "controls": controls,
            },
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
