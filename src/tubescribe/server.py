"""Single-model ASR service, loopback only. Install with the `asr` extra."""

from __future__ import annotations

import asyncio
import importlib.metadata
import os
import socket
import subprocess
import tempfile
import time
from contextlib import asynccontextmanager
from pathlib import Path

from .inference import HardwarePolicy
from .storage import now


def create_app(*, hardware=HardwarePolicy()):
    from fastapi import FastAPI, HTTPException, UploadFile
    from faster_whisper import WhisperModel
    from faster_whisper.utils import download_model

    gate = asyncio.Semaphore(1)
    identity = {}
    model = None

    @asynccontextmanager
    async def lifespan(app):
        nonlocal model
        name = os.environ.get("TUBESCRIBE_MODEL", "large-v3-turbo")
        cache = os.environ.get(
            "TUBESCRIBE_MODEL_CACHE", str(Path.home() / ".cache/tubescribe/models")
        )
        requested_revision = os.environ.get("TUBESCRIBE_MODEL_REVISION")
        if requested_revision is None:
            if name != "large-v3-turbo":
                raise ValueError("Set TUBESCRIBE_MODEL_REVISION explicitly when changing models")
            requested_revision = "0a363e9161cbc7ed1431c9597a8ceaf0c4f78fcf"
        model_path = download_model(name, cache_dir=cache, revision=requested_revision)
        revision = Path(model_path).name
        device = os.environ.get("TUBESCRIBE_DEVICE", "cuda")
        compute = os.environ.get("TUBESCRIBE_COMPUTE", "float16")
        if device != "cuda":
            raise RuntimeError(
                "TubeScribe requires RTX 4090 CUDA inference; CPU fallback is disabled"
            )
        gpu = (
            subprocess.check_output(
                [
                    "nvidia-smi",
                    "--id=0",
                    "--query-gpu=name,memory.total",
                    "--format=csv,noheader,nounits",
                ],
                text=True,
            )
            .strip()
            .split(",")
        )
        gpu_name = gpu[0].strip()
        hardware.check(device, gpu_name)
        model = WhisperModel(
            model_path,
            device=device,
            device_index=0,
            compute_type=compute,
            num_workers=1,
            cpu_threads=4,
        )
        identity.update(
            service="tubescribe-asr",
            model=name,
            model_source=(
                "mobiuslabsgmbh/faster-whisper-large-v3-turbo" if name == "large-v3-turbo" else name
            ),
            revision=revision,
            backend="faster-whisper",
            backend_version=importlib.metadata.version("faster-whisper"),
            ctranslate2_version=importlib.metadata.version("ctranslate2"),
            device=device,
            device_index=0,
            gpu_name=gpu_name,
            gpu_memory_total_mib=int(gpu[1].strip()),
            host=socket.gethostname(),
            compute_type=compute,
            concurrency=1,
            decoding={
                "beam_size": 5,
                "vad_filter": True,
                "condition_on_previous_text": False,
                "word_timestamps": True,
            },
        )
        yield

    app = FastAPI(lifespan=lifespan)

    @app.get("/health")
    async def health():
        return {**identity, "ready": model is not None, "busy": gate.locked()}

    # Annotation is evaluated by FastAPI; imported locally to keep base install light.
    async def transcribe(audio):
        if gate.locked():
            raise HTTPException(503, "Inference busy; retry later")
        async with gate:
            path = None
            try:
                with tempfile.NamedTemporaryFile(suffix=".audio", delete=False) as f:
                    path = Path(f.name)
                    size = 0
                    while chunk := await audio.read(1024 * 1024):
                        size += len(chunk)
                        if size > 1024 * 1024 * 1024:
                            raise HTTPException(413, "Audio exceeds 1 GiB")
                        f.write(chunk)
                if size == 0:
                    raise HTTPException(400, "Empty audio")

                def infer():
                    started_at = now()
                    start = time.perf_counter()
                    segments, info = model.transcribe(
                        str(path),
                        beam_size=5,
                        vad_filter=True,
                        condition_on_previous_text=False,
                        word_timestamps=True,
                        language=os.environ.get("TUBESCRIBE_LANGUAGE") or None,
                    )
                    rows = [
                        {
                            "start": s.start,
                            "end": s.end,
                            "text": s.text.strip(),
                            "avg_logprob": s.avg_logprob,
                            "no_speech_prob": s.no_speech_prob,
                            "words": [
                                {
                                    "start": w.start,
                                    "end": w.end,
                                    "word": w.word,
                                    "probability": w.probability,
                                }
                                for w in (s.words or [])
                            ],
                        }
                        for s in segments
                        if s.text.strip() and s.end > s.start
                    ]
                    return {
                        **identity,
                        "segments": rows,
                        "language": info.language,
                        "language_probability": info.language_probability,
                        "duration": info.duration,
                        "speech_duration": info.duration_after_vad,
                        "processing_seconds": time.perf_counter() - start,
                        "started_at": started_at,
                        "finished_at": now(),
                    }

                # Shield inference from cancellation; retain the semaphore and temporary source
                # until GPU work ends even when a disconnected client abandons its request.
                task = asyncio.create_task(asyncio.to_thread(infer))
                try:
                    return await asyncio.shield(task)
                except asyncio.CancelledError:
                    await task
                    raise
            except HTTPException:
                raise
            except Exception as e:
                print(f"ASR failure: {e}", flush=True)
                raise HTTPException(500, "ASR failed; inspect the service journal") from e
            finally:
                await audio.close()
                if path:
                    path.unlink(missing_ok=True)

    transcribe.__annotations__["audio"] = UploadFile
    app.post("/transcribe")(transcribe)
    return app


def main():
    import argparse

    import uvicorn

    p = argparse.ArgumentParser()
    p.add_argument("--port", type=int, default=18765)
    args = p.parse_args()
    uvicorn.run(create_app(), host="127.0.0.1", port=args.port, workers=1, limit_concurrency=4)


if __name__ == "__main__":
    main()
