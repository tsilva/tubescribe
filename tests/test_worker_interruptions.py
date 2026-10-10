"""Real CLI signal handling must release ownership and preserve retry budgets."""

import json
import os
import signal
import subprocess
import sys
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import test_postprocessing_pipeline as pipeline


class WorkerInterruptions(unittest.TestCase):
    def test_sigterm_during_model_preserves_stage_budget_and_releases_claims(self):
        for command in ("postprocess", "artifacts"):
            for checkpoint in (False, True):
                with self.subTest(command=command, checkpoint=checkpoint):
                    self.check_interruption(command, checkpoint)

    def check_interruption(self, command, checkpoint):
        fixture = pipeline.FinalStage()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        fixture.full()
        store = fixture.store
        entered, released = threading.Event(), threading.Event()
        if checkpoint:
            released.set()
        calls = []

        class Bridge(BaseHTTPRequestHandler):
            def log_message(self, *_):
                pass

            def do_GET(self):
                self.send_response(200)
                self.end_headers()
                self.wfile.write(b'{"paths":{"/api/v1/agent/runs":{}}}')

            def do_POST(self):
                request = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                calls.append(request)
                entered.set()
                released.wait(15)
                if command == "artifacts":
                    output = {
                        "markdown": "# Summary\n\nA real transcript.",
                        "used_image_ids": [],
                        "evidence": [
                            {
                                "claim": "Real transcript",
                                "source_quote": "**Speaker 1:** A real transcript.",
                            }
                        ],
                    }
                else:
                    output = {
                        "markdown": "# Test\n\n## Opening\n\n**Speaker 1:** A real transcript.",
                        "sections": [{"heading": "Opening", "first_cue": 1, "last_cue": 1}],
                        "used_image_ids": [],
                        "unresolved_references": [],
                        "speaker_names": [
                            {
                                "speaker_id": "Speaker 1",
                                "name": None,
                                "confidence": "unknown",
                                "evidence": [],
                            }
                        ],
                    }
                result = {
                    "output": output,
                    "elapsed_seconds": 0.01,
                    "tool_calls": [],
                    "usage": {
                        "total": {"totalTokens": 120, "inputTokens": 100, "outputTokens": 20}
                    },
                }
                self.send_response(200)
                self.end_headers()
                try:
                    self.wfile.write(json.dumps(result).encode())
                except (BrokenPipeError, ConnectionResetError):
                    pass

        server = ThreadingHTTPServer(("127.0.0.1", 0), Bridge)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        argv = [
            sys.executable,
            "-m",
            "tubescribe.cli",
            "--data",
            str(store.root),
            command,
            "--video",
            fixture.job["video_id"],
            "--bridge",
            f"http://127.0.0.1:{server.server_port}",
            "--force",
        ]
        if command == "postprocess":
            argv += ["--mode", "text", "--no-summary"]
        marker = store.root / "saving.marker"
        module = "postprocessing" if command == "postprocess" else "artifacts"
        target = "processed.md" if command == "postprocess" else "artifact.md"
        wrapper = (
            f"import runpy,time; from pathlib import Path; from tubescribe import {module} as m\n"
            "original=m.atomic_write\n"
            "def pause(path,content):\n"
            f" if path.name=={target!r}:\n"
            f"  Path({str(marker)!r}).write_text('saving'); time.sleep(30)\n"
            " return original(path,content)\n"
            "m.atomic_write=pause\n"
            "runpy.run_module('tubescribe.cli',run_name='__main__')\n"
        )
        launch = [sys.executable, "-c", wrapper, *argv[3:]] if checkpoint else argv
        child = subprocess.Popen(launch, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        try:
            self.assertTrue(entered.wait(10), "CLI did not reach the model request")
            if checkpoint:
                deadline = time.monotonic() + 10
                while not marker.exists() and time.monotonic() < deadline:
                    time.sleep(0.02)
                self.assertTrue(marker.exists(), "CLI did not reach response packaging")
            os.kill(child.pid, signal.SIGTERM)
            output = child.communicate(timeout=10)[0].decode()
        finally:
            released.set()
            if child.poll() is None:
                child.kill()
                child.communicate(timeout=5)
        self.assertEqual(child.returncode, 130, output)
        row = store.stage_row(
            "postprocessing" if command == "postprocess" else "artifact",
            fixture.job["id"],
            workflow="summary" if command == "artifacts" else None,
        )
        self.assertEqual(row["state"], "pending")
        self.assertEqual(row["attempts"], int(checkpoint))
        self.assertEqual(row["save_attempts"], 0)
        self.assertEqual(store.db.execute("SELECT count(*) FROM claims").fetchone()[0], 0)
        self.assertEqual(
            store.db.execute("SELECT state FROM submissions").fetchone()[0], "interrupted"
        )
        # Retry the same real CLI after the blocked request has been interrupted.
        argv.remove("--force")
        resumed = subprocess.run(argv, capture_output=True, timeout=15)
        self.assertEqual(resumed.returncode, 0, resumed.stdout.decode() + resumed.stderr.decode())
        row = store.stage_row(
            "postprocessing" if command == "postprocess" else "artifact",
            fixture.job["id"],
            workflow="summary" if command == "artifacts" else None,
        )
        self.assertEqual(row["state"], "complete")
        self.assertEqual(row["attempts"], 1)
        self.assertEqual(len(calls), 1 if checkpoint else 2)
        self.assertEqual(fixture.transcribe.call_count, 1)
        self.assertEqual(fixture.requests, 1)
