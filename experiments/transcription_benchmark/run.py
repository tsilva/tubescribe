"""Run matched audio through unchanged resident services or an owned temporary native server."""

import argparse
import base64
import contextlib
import hashlib
import json
import os
import signal
import socket
import subprocess
import threading
import time
import urllib.request
import uuid
from pathlib import Path
from urllib.parse import urlsplit


def write(path, data):
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(data, indent=2) + "\n")
    temporary.replace(path)


def get(url):
    with urllib.request.urlopen(url, timeout=5) as response:
        return json.load(response)


def post(url, payload, content_type):
    request = urllib.request.Request(url, payload, {"Content-Type": content_type})
    with urllib.request.urlopen(request, timeout=1800) as response:
        return json.load(response)


def multipart(path, name="file", fields=None):
    boundary = "benchmark-" + uuid.uuid4().hex
    parts = []
    for key, value in (fields or {}).items():
        parts.append(
            f'--{boundary}\r\nContent-Disposition: form-data; name="{key}"\r\n\r\n{value}\r\n'.encode()
        )
    parts.append(
        f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"; filename="audio.wav"\r\nContent-Type: audio/wav\r\n\r\n'.encode()
    )
    parts += [path.read_bytes(), f"\r\n--{boundary}--\r\n".encode()]
    return b"".join(parts), f"multipart/form-data; boundary={boundary}"


def service_config(path):
    """Keep installation-specific endpoints and paths outside published source."""
    config = json.loads(path.read_text())
    if not isinstance(config, dict):
        raise ValueError("Service configuration must be an object")
    for key in ("asr_url", "diarization_url"):
        value = config.get(key)
        if not isinstance(value, str):
            raise ValueError("Both local inference service URLs are required")
        parsed = urlsplit(value)
        if (
            parsed.scheme not in ("http", "https")
            or parsed.hostname not in ("localhost", "127.0.0.1", "::1")
            or parsed.username
            or parsed.password
            or parsed.query
            or parsed.fragment
            or parsed.path not in ("", "/")
        ):
            raise ValueError("Service URLs must be loopback origins without credentials")
        config[key] = value.rstrip("/")
    units = config.get("service_units")
    if (
        not isinstance(units, list)
        or not units
        or any(not isinstance(unit, str) or not unit or unit.startswith("-") for unit in units)
    ):
        raise ValueError("Explicit service units are required for preservation checks")
    files = config.get("source_files")
    if (
        not isinstance(files, list)
        or not files
        or any(not isinstance(file, str) or not Path(file).is_absolute() for file in files)
    ):
        raise ValueError("Explicit absolute unit and source file paths are required")
    return config


def snapshot(services):
    state = subprocess.check_output(
        [
            "systemctl",
            "--user",
            "show",
            *services["service_units"],
            "-p",
            "Id",
            "-p",
            "MainPID",
            "-p",
            "ActiveState",
            "-p",
            "ExecMainStartTimestamp",
        ],
        text=True,
    )
    files = {
        file: hashlib.sha256(Path(file).read_bytes()).hexdigest()
        for file in services["source_files"]
    }
    return {
        "services": state,
        "files": files,
        "asr": get(services["asr_url"] + "/health"),
        "diarization": get(services["diarization_url"] + "/health"),
    }


def idle(services):
    for key in ("asr_url", "diarization_url"):
        health = get(services[key] + "/health")
        if not health.get("ready") or health.get("busy"):
            raise RuntimeError("Production inference is busy or unavailable; benchmark stopped")


class Monitor:
    def __init__(self, services, candidate=None):
        self.services = services
        self.samples = []
        self.stop = threading.Event()
        self.candidate = candidate
        self.failure = None

    def __enter__(self):
        self.thread = threading.Thread(target=self.sample, daemon=True)
        self.thread.start()
        return self

    def sample(self):
        while not self.stop.is_set():
            try:
                raw = (
                    subprocess.check_output(
                        [
                            "nvidia-smi",
                            "--query-gpu=memory.used,memory.free,utilization.gpu,power.draw",
                            "--format=csv,noheader,nounits",
                        ],
                        text=True,
                        timeout=5,
                    )
                    .strip()
                    .split(",")
                )
                used, free, utilization, watts = map(float, raw)
                self.samples.append(
                    {
                        "time": time.time(),
                        "used_mib": used,
                        "free_mib": free,
                        "utilization": utilization,
                        "watts": watts,
                    }
                )
                if self.candidate:
                    idle(self.services)
                    if free < 1024:
                        raise RuntimeError("Candidate breached the 1 GiB GPU headroom floor")
            except Exception as error:
                self.failure = str(error)
                if self.candidate and self.candidate.poll() is None:
                    os.killpg(self.candidate.pid, signal.SIGTERM)
                return
            self.stop.wait(0.25)

    def __exit__(self, *unused):
        self.stop.set()
        self.thread.join(timeout=10)
        if self.failure:
            raise RuntimeError(self.failure)


def assign_words(asr, diarization):
    # The exact production assignment implementation, imported from its resident environment.
    from tubescribe.diarization import assign_speakers

    segments = assign_speakers(asr, diarization)["segments"]
    words = []
    for segment in segments:
        for word in segment.get("words", []):
            words.append(
                {
                    "text": word["word"],
                    "start": word["start"],
                    "end": word["end"],
                    "speaker": segment.get("speaker_id", "UNKNOWN"),
                }
            )
    return words


def baseline(path, services):
    payload, content_type = multipart(path, "audio")
    started = time.perf_counter()
    asr = post(services["asr_url"] + "/transcribe", payload, content_type)
    asr_wall = time.perf_counter() - started
    started = time.perf_counter()
    diarization = post(
        services["diarization_url"] + "/diarize", path.read_bytes(), "application/octet-stream"
    )
    diarization_wall = time.perf_counter() - started
    started = time.perf_counter()
    words = assign_words(asr, diarization)
    assignment_seconds = time.perf_counter() - started
    return {
        "text": " ".join(s["text"] for s in asr["segments"]),
        "words": words,
        "turns": diarization["turns"],
        "asr": asr,
        "diarization": diarization,
        "asr_wall_seconds": asr_wall,
        "diarization_wall_seconds": diarization_wall,
        "assignment_seconds": assignment_seconds,
        "server_compute_seconds": asr["processing_seconds"] + diarization["processing_seconds"],
    }


def nemotron(path, port):
    model_id = get(f"http://127.0.0.1:{port}/v1/models")["data"][0]["id"]
    prompt = (
        "Transcribe this audio. Include all spoken words and verbal fillers. "
        "Write numbers as spoken. Use no punctuation and no capitalization. "
        "Start every speaker turn with a speaker tag such as <spk:0>, <spk:1>, <spk:2>, etc.; "
        "assign one stable tag per speaker and keep speaker identities consistent. "
        "Output only the speaker-tagged transcript."
    )
    payload = json.dumps(
        {
            "model": model_id,
            "temperature": 0,
            "seed": 0,
            "timestamp_granularities": ["word"],
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": prompt},
                        {
                            "type": "input_audio",
                            "input_audio": {
                                "data": base64.b64encode(path.read_bytes()).decode(),
                                "format": "wav",
                            },
                        },
                    ],
                }
            ],
        }
    ).encode()
    raw = post(f"http://127.0.0.1:{port}/v1/chat/completions", payload, "application/json")
    transcription = raw.get("transcription", raw)
    words = [
        {
            "text": w.get("word", w.get("text", "")),
            "start": w["start"],
            "end": w["end"],
            "speaker": str(w.get("speaker", "UNKNOWN")),
        }
        for w in transcription.get("words", [])
    ]
    if not words:
        raise RuntimeError(
            "Nemotron returned no aligned words; this is not a valid diarization run"
        )
    turns = transcription.get("diarization_timestamps", [])
    if not turns:
        raise RuntimeError("Native activity intervals are required for matched DER scoring")
    return {
        "raw": raw,
        "text": transcription.get("text", ""),
        "words": words,
        "turns": turns,
        "turn_source": "diarization_timestamps",
        "request_settings": {"model": model_id, "temperature": 0, "seed": 0, "prompt": prompt},
    }


@contextlib.contextmanager
def server(args):
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    command = [
        str(args.runtime),
        "serve",
        "--asr-model",
        str(args.model),
        "--host",
        "127.0.0.1",
        "--port",
        str(port),
        "--threads",
        "4",
        "--no-ui",
        "--device",
        "cuda:0",
        "--asr.salm.max_sequences",
        "1",
        "--asr.salm.n_ctx_per_sequence",
        "4096",
        "--asr.salm.max_context",
        "8192",
        "--asr.salm.mtp_draft",
        "0",
        "--asr.salm.n_batch",
        "512",
        "--asr.salm.encoder_max_batch",
        "1",
        "--asr.backend.threads",
        str(args.cpu_threads),
    ]
    if args.cpu_moe:
        command += ["--asr.salm.n_cpu_moe", str(args.cpu_moe)]
    env = {
        k: v
        for k, v in os.environ.items()
        if not any(s in k.upper() for s in ["TOKEN", "SECRET", "PASSWORD", "API_KEY"])
    }
    started = time.perf_counter()
    with (args.output / "server.log").open("w") as log:
        process = subprocess.Popen(
            command, stdout=log, stderr=subprocess.STDOUT, env=env, start_new_session=True
        )
        write(args.output / "owned-process.json", {"pid": process.pid, "command": command})
        try:
            with Monitor(args.services, process):
                deadline = time.monotonic() + 240
                while True:
                    if process.poll() is not None:
                        raise RuntimeError(
                            f"Isolated Nemotron server exited {process.returncode}; see server.log"
                        )
                    try:
                        health = get(f"http://127.0.0.1:{port}/health")
                        break
                    except (OSError, ValueError):
                        if time.monotonic() >= deadline:
                            raise TimeoutError("Nemotron startup timeout")
                        time.sleep(0.5)
                write(
                    args.output / "startup.json",
                    {
                        "seconds": time.perf_counter() - started,
                        "health": health,
                        "command": command,
                    },
                )
                yield port, process
        finally:
            if process.poll() is None:
                os.killpg(process.pid, signal.SIGTERM)
                try:
                    process.wait(timeout=30)
                except subprocess.TimeoutExpired:
                    os.killpg(process.pid, signal.SIGKILL)
                    process.wait(timeout=10)
            write(
                args.output / "cleanup.json",
                {
                    "owned_pid": process.pid,
                    "exit_code": process.returncode,
                    "process_stopped": process.poll() is not None,
                },
            )


def execute(args, corpus, port=None, candidate=None):
    warmup = args.corpus.parent / "warmup.wav"
    if not warmup.exists():
        subprocess.run(
            [
                "ffmpeg",
                "-nostdin",
                "-v",
                "error",
                "-i",
                str(args.corpus.parent / corpus["samples"][0]["audio"]),
                "-t",
                "30",
                "-c:a",
                "pcm_s16le",
                str(warmup),
            ],
            check=True,
        )
    idle(args.services)
    start = time.perf_counter()
    warmup_result = (
        baseline(warmup, args.services) if args.backend == "baseline" else nemotron(warmup, port)
    )
    write(
        args.output / "warmup.json",
        {"seconds": time.perf_counter() - start, "prediction": warmup_result},
    )
    for repeat in range(args.repeats):
        # Alternate fixed order to expose drift without choosing inputs from results.
        samples = corpus["samples"] if repeat % 2 == 0 else list(reversed(corpus["samples"]))
        for sample in samples:
            idle(args.services)
            audio = args.corpus.parent / sample["audio"]
            if hashlib.sha256(audio.read_bytes()).hexdigest() != sample["sha256"]:
                raise ValueError("Corpus checksum mismatch")
            started = time.perf_counter()
            with Monitor(args.services, candidate) as monitor:
                result = (
                    baseline(audio, args.services)
                    if args.backend == "baseline"
                    else nemotron(audio, port)
                )
            elapsed = time.perf_counter() - started
            result.update(
                sample_id=sample["id"],
                repeat=repeat,
                wall_seconds=elapsed,
                duration=sample["duration"],
                backend=args.backend,
                audio_sha256=sample["sha256"],
                gpu_samples=monitor.samples,
            )
            write(args.output / f"{sample['id']}-r{repeat}.json", result)
            print(
                json.dumps(
                    {
                        "completed": sample["id"],
                        "backend": args.backend,
                        "repeat": repeat,
                        "seconds": round(elapsed, 3),
                        "words": len(result["words"]),
                    }
                ),
                flush=True,
            )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("backend", choices=["baseline", "nemotron"])
    parser.add_argument("corpus", type=Path)
    parser.add_argument("output", type=Path)
    parser.add_argument("--service-config", type=Path, required=True)
    parser.add_argument("--runtime", type=Path)
    parser.add_argument("--model", type=Path)
    parser.add_argument("--cpu-moe", type=int, default=0)
    parser.add_argument("--cpu-threads", type=int, default=6)
    parser.add_argument("--repeats", type=int, default=2)
    args = parser.parse_args()
    args.services = service_config(args.service_config)
    if args.backend == "nemotron" and (not args.runtime or not args.model):
        parser.error("The native candidate requires --runtime and --model")
    args.output = args.output.resolve()
    args.output.mkdir(parents=True, exist_ok=True)
    before = snapshot(args.services)
    write(args.output / "before.json", before)
    corpus = json.loads(args.corpus.read_text())
    try:
        if args.backend == "baseline":
            execute(args, corpus)
        else:
            with server(args) as (port, candidate):
                execute(args, corpus, port, candidate)
    finally:
        after = snapshot(args.services)
        preserved = before["services"] == after["services"] and before["files"] == after["files"]
        write(args.output / "after.json", {**after, "production_preserved": preserved})
        if not preserved:
            raise RuntimeError("Production service identity or source changed during benchmark")


if __name__ == "__main__":
    main()
