"""Run a bounded diarization trial on beast-3's RTX 4090, in its isolated environment."""

import argparse
import importlib.metadata
import json
import os
import socket
import time
import wave
from pathlib import Path

# Opt out before importing pyannote, without touching machine-wide settings.
os.environ["PYANNOTE_METRICS_ENABLED"] = "0"
os.environ["HF_HUB_DISABLE_TELEMETRY"] = "1"

MODEL = "pyannote/speaker-diarization-community-1"
REVISION = "3533c8cf8e369892e6b79ff1bf80f7b0286a54ee"


def main():
    import numpy as np
    import torch
    from pyannote.audio import Pipeline

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("audio", type=Path)
    parser.add_argument("output", type=Path)
    args = parser.parse_args()
    if not torch.cuda.is_available() or "RTX 4090" not in torch.cuda.get_device_name(0):
        raise RuntimeError("This trial requires beast-3's RTX 4090; no CPU fallback")
    if torch.cuda.mem_get_info(0)[0] < 2 * 1024**3:
        raise RuntimeError("Less than 2 GiB free VRAM; wait for GPU headroom")
    with wave.open(str(args.audio)) as audio:
        if (audio.getnchannels(), audio.getframerate(), audio.getsampwidth()) != (1, 16000, 2):
            raise ValueError("Provide mono 16 kHz PCM16 WAV")
        waveform = torch.from_numpy(
            np.frombuffer(audio.readframes(audio.getnframes()), dtype="<i2").astype(np.float32)
            / 32768.0
        ).unsqueeze(0)
    torch.set_num_threads(4)
    load_start = time.perf_counter()
    pipeline = Pipeline.from_pretrained(
        MODEL,
        revision=REVISION,
        cache_dir=str(Path.home() / ".cache/tubescribe/models"),
    )
    if pipeline is None:
        raise RuntimeError("Model access failed; accept model conditions and run hf auth login")
    pipeline.to(torch.device("cuda:0"))
    loaded = time.perf_counter() - load_start
    results = []
    for label, options in [("automatic", {}), ("known_two_speakers", {"num_speakers": 2})]:
        torch.cuda.reset_peak_memory_stats()
        torch.cuda.synchronize()
        start = time.perf_counter()
        output = pipeline({"waveform": waveform, "sample_rate": 16000}, **options)
        torch.cuda.synchronize()

        def turns(annotation):
            return [
                {"start": segment.start, "end": segment.end, "speaker": speaker}
                for segment, _, speaker in annotation.itertracks(yield_label=True)
            ]

        results.append(
            {
                "label": label,
                "options": options,
                "processing_seconds": time.perf_counter() - start,
                "peak_allocated_mib": torch.cuda.max_memory_allocated() / 1024**2,
                "turns": turns(output.speaker_diarization),
                "exclusive_turns": turns(output.exclusive_speaker_diarization),
            }
        )
        print(
            json.dumps({"completed": label, "seconds": results[-1]["processing_seconds"]}),
            flush=True,
        )
    args.output.write_text(
        json.dumps(
            {
                "model": MODEL,
                "revision": REVISION,
                "backend_version": importlib.metadata.version("pyannote-audio"),
                "torch_version": torch.__version__,
                "host": socket.gethostname(),
                "device": "cuda:0",
                "gpu": torch.cuda.get_device_name(0),
                "duration": waveform.shape[1] / 16000,
                "model_load_seconds": loaded,
                "runs": results,
            },
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
