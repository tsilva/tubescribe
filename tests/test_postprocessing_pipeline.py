"""Final-stage retry/recovery must never rerun earlier inference or corrupt raw speech."""

import json
import shutil
import subprocess
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import Mock, patch
from urllib.request import Request, urlopen

import httpx
from test_pipeline import CID, DIARIZATION, RESULT, VID

from tubescribe.dashboard import make_server, snapshot
from tubescribe.postprocessing_worker import cached_video, run
from tubescribe.storage import Store, atomic_write
from tubescribe.worker import work


class FinalStage(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = Store(Path(self.temp.name))
        self.addCleanup(self.store.db.close)
        self.store.channel(
            {"id": CID, "title": "Test", "url": "https://youtube.com/channel/" + CID}
        )
        self.store.index(
            CID,
            "upload",
            [{"id": VID, "title": "Test", "url": "https://youtube.com/watch?v=" + VID}],
            "turbo-v1",
        )
        self.job = dict(
            self.store.db.execute(
                "SELECT j.*,v.channel_id,v.title,v.url,v.kind FROM jobs j JOIN videos v ON v.id=j.video_id"
            ).fetchone()
        )
        self.requests = 0
        self.error_status = None
        self.inspect_video = False
        self.invalid_output = False
        owner = self

        class Bridge(BaseHTTPRequestHandler):
            def log_message(self, *_):
                pass

            def do_GET(self):
                data = json.dumps({"paths": {"/api/v1/agent/runs": {}}}).encode()
                self.send_response(200)
                self.end_headers()
                self.wfile.write(data)

            def do_POST(self):
                owner.requests += 1
                payload = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                owner.payload = payload
                if owner.error_status:
                    self.send_response(owner.error_status)
                    self.end_headers()
                    self.wfile.write(b'{"detail":"busy"}')
                    return
                result = {
                    "output": {
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
                    },
                    "tool_calls": [],
                    "elapsed_seconds": 0.01,
                    "usage": {
                        "total": {
                            "totalTokens": 120,
                            "inputTokens": 100,
                            "cachedInputTokens": 0,
                            "outputTokens": 20,
                        }
                    },
                }
                if owner.invalid_output:
                    result["output"]["sections"][0]["last_cue"] = 999
                if owner.inspect_video:
                    executor = payload["tool_executor"]
                    request = Request(
                        executor["url"],
                        data=json.dumps(
                            {
                                "tool": "video_screenshots",
                                "arguments": {
                                    "timestamps": [1],
                                    "resolution": "preview",
                                    "crop": None,
                                },
                                "run_id": "test",
                                "call_id": "frame-test",
                            }
                        ).encode(),
                        headers={
                            "Authorization": "Bearer " + executor["token"],
                            "Content-Type": "application/json",
                        },
                    )
                    with urlopen(request, timeout=30) as response:
                        tool_result = json.load(response)
                    assert tool_result["success"] and any(
                        x["type"] == "inputImage" for x in tool_result["contentItems"]
                    )
                    result["output"]["markdown"] += (
                        "\n\n![Frame at one second](image:frame_0001000)"
                    )
                    result["output"]["used_image_ids"] = ["frame_0001000"]
                    result["tool_calls"] = [
                        {
                            "tool": "video_screenshots",
                            "arguments": {"timestamps": [1], "resolution": "preview", "crop": None},
                            "success": True,
                            "image_count": 1,
                        }
                    ]
                self.send_response(200)
                self.end_headers()
                self.wfile.write(json.dumps(result).encode())

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Bridge)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self.close_bridge)
        self.bridge = f"http://127.0.0.1:{self.server.server_port}"
        diarization = Mock()
        diarization.health.return_value = DIARIZATION
        diarization.diarize.return_value = DIARIZATION
        for target, options in [
            ("tubescribe.worker.ready_diarization", {"return_value": diarization}),
            ("tubescribe.worker.Client.health", {"return_value": RESULT}),
            ("tubescribe.worker.Client.transcribe", {"return_value": RESULT}),
            ("tubescribe.worker.download", {"side_effect": self.download}),
        ]:
            patcher = patch(target, **options)
            setattr(self, target.rsplit(".", 1)[1], patcher.start())
            self.addCleanup(patcher.stop)

    def close_bridge(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()

    def download(self, *_):
        path = self.store.root / "audio" / (VID + ".webm")
        path.parent.mkdir(exist_ok=True)
        path.write_bytes(b"retained audio")
        return path, {"channel_id": CID, "duration": 4}

    def full(self):
        return work(
            self.store, "http://127.0.0.1:1", ids=[VID], bridge=self.bridge, postprocessing="text"
        )

    def row(self):
        return dict(self.store.db.execute("SELECT * FROM postprocessings").fetchone())

    def test_full_worker_publishes_final_stage_then_skips_all_inference(self):
        self.assertEqual(self.full()["completed"], 1)
        row = self.row()
        self.assertEqual(row["state"], "complete")
        directory = self.store.root / row["output_dir"]
        self.assertIn("A real transcript.", (directory / "processed.md").read_text())
        self.assertTrue((directory / "processed-with-figures.zip").is_file())
        self.assertEqual(self.payload["model"], "codex/gpt-6.1-sol")
        self.assertEqual(self.payload["reasoning_effort"], "high")
        self.assertEqual(self.full()["skipped"], 1)
        self.assertEqual(self.requests, 1)
        self.assertEqual(self.transcribe.call_count, 1)
        self.assertEqual(self.download.call_count, 1)

    def test_saved_agent_response_repairs_bundle_without_services_or_reinference(self):
        self.full()
        row = self.row()
        bundle = self.store.root / row["output_dir"] / "processed-with-figures.zip"
        original = bundle.read_bytes()
        bundle.write_bytes(b"corrupt")
        self.ready_diarization.side_effect = AssertionError("Diarization must stay stopped")
        self.health.side_effect = AssertionError("ASR must stay stopped")
        result = work(
            self.store,
            "http://127.0.0.1:1",
            ids=[VID],
            bridge="http://127.0.0.1:1",
            postprocessing="text",
        )
        self.assertEqual(result["completed"], 1)
        self.assertEqual(self.requests, 1)
        self.assertEqual(self.transcribe.call_count, 1)
        self.assertGreater(len(bundle.read_bytes()), 100)
        self.assertEqual(self.row()["state"], "complete")
        self.assertEqual(len(bundle.read_bytes()), len(original))

    def test_failure_after_model_response_resumes_without_another_model_call(self):
        def fail_manifest(path, content):
            if path.name == "manifest.json":
                raise OSError("disk busy")
            atomic_write(path, content)

        with patch("tubescribe.postprocessing_worker.atomic_write", side_effect=fail_manifest):
            self.assertEqual(self.full()["failed"], 1)
        self.assertEqual(self.row()["stage"], "save")
        self.assertEqual(self.requests, 1)
        self.store.retry([VID])
        self.assertEqual(self.full()["completed"], 1)
        self.assertEqual(self.requests, 1)

    def test_existing_diarized_source_runs_without_asr_and_force_preserves_previous_bundle(self):
        self.full()
        directory = self.store.root / self.row()["output_dir"]
        original_source = self.store.root / self.row()["source_path"]
        raw = original_source.read_bytes()
        self.health.side_effect = AssertionError("ASR stopped")
        self.ready_diarization.side_effect = AssertionError("Diarization stopped")
        self.assertEqual(run(self.store, bridge=self.bridge, ids=[VID], mode="text")["skipped"], 1)
        self.assertEqual(
            run(self.store, bridge=self.bridge, ids=[VID], mode="text", force=True)["completed"], 1
        )
        self.assertEqual(self.requests, 2)
        self.assertNotEqual(directory, self.store.root / self.row()["output_dir"])
        self.assertTrue((directory / "processed.md").is_file())
        self.assertEqual(original_source.read_bytes(), raw)

    def test_unavailable_bridge_preserves_diarization_and_does_not_download_video(self):
        work(self.store, "http://127.0.0.1:1", ids=[VID], postprocessing="off")
        with patch("tubescribe.postprocessing_worker.download_video") as video:
            result = run(self.store, bridge="http://127.0.0.1:1", ids=[VID])
        self.assertIn("paused", result)
        video.assert_not_called()
        self.assertEqual(self.row()["attempts"], 0)
        self.assertEqual(self.row()["state"], "pending")
        self.assertEqual(self.transcribe.call_count, 1)
        self.assertEqual(
            self.store.db.execute("SELECT state FROM diarizations").fetchone()[0], "complete"
        )

    def test_postprocessing_attempt_bound_is_independent_of_exhausted_successful_asr(self):
        self.store.update(self.job["id"], asr_attempts=2)
        self.error_status = 422
        self.assertEqual(self.full()["failed"], 1)
        self.assertEqual(self.store.db.execute("SELECT asr_attempts FROM jobs").fetchone()[0], 3)
        self.store.retry([VID])
        self.error_status = None
        self.assertEqual(self.full()["completed"], 1)
        self.assertEqual(self.transcribe.call_count, 1)
        self.assertEqual(self.requests, 2)

    def test_interruption_does_not_consume_stage_attempt_and_wrong_source_checkpoint_is_rejected(
        self,
    ):
        work(self.store, "http://127.0.0.1:1", ids=[VID], postprocessing="off")
        with patch(
            "tubescribe.postprocessing_worker.engine.process", side_effect=KeyboardInterrupt
        ):
            with self.assertRaises(KeyboardInterrupt):
                run(self.store, bridge=self.bridge, ids=[VID], mode="text")
        self.assertEqual(self.row()["attempts"], 0)
        self.assertEqual(
            run(self.store, bridge=self.bridge, ids=[VID], mode="text")["completed"], 1
        )
        directory = self.store.root / self.row()["output_dir"]
        (directory / "response.json").write_text("{}")
        result = self.full()
        self.assertEqual(result["failed"], 1)
        self.assertIn("checkpoint is corrupt", self.row()["error"])
        self.assertEqual(self.requests, 1)

    def test_server_failure_pauses_without_consuming_model_retry_budget(self):
        self.error_status = 503
        result = self.full()
        self.assertEqual(result["failed"], 1)
        self.assertEqual(self.row()["state"], "pending")
        self.assertEqual(self.row()["attempts"], 0)
        self.assertEqual(self.requests, 1)

    def test_force_limit_preserves_other_completed_outputs(self):
        self.full()
        other = "zyxwvutsrqp"
        self.store.index(
            CID,
            "upload",
            [{"id": other, "title": "Second", "url": "https://youtube.com/watch?v=" + other}],
            "turbo-v1",
        )
        self.assertEqual(
            work(
                self.store,
                "http://127.0.0.1:1",
                ids=[other],
                bridge=self.bridge,
                postprocessing="text",
            )["completed"],
            1,
        )
        before = {
            row["job_id"]: dict(row)
            for row in self.store.db.execute("SELECT * FROM postprocessings")
        }
        result = run(self.store, bridge=self.bridge, mode="text", force=True, limit=1)
        self.assertEqual(result["completed"], 1)
        after = {
            row["job_id"]: dict(row)
            for row in self.store.db.execute("SELECT * FROM postprocessings")
        }
        unchanged = [job_id for job_id in before if before[job_id] == after[job_id]]
        self.assertEqual(len(unchanged), 1)
        self.assertTrue(all(row["state"] == "complete" for row in after.values()))
        self.assertEqual(self.requests, 3)

    def test_wait_stops_when_final_stage_attempts_are_exhausted(self):
        work(self.store, "http://127.0.0.1:1", ids=[VID], postprocessing="off")
        self.error_status = 422
        with patch("tubescribe.postprocessing_worker.time.sleep") as sleep:
            result = run(
                self.store, bridge=self.bridge, ids=[VID], mode="text", wait=True, max_attempts=1
            )
        self.assertEqual(result["failed"], 1)
        sleep.assert_not_called()

    def test_final_model_attempt_can_retry_packaging_without_model_or_services(self):
        work(self.store, "http://127.0.0.1:1", ids=[VID], postprocessing="off")
        with self.store.db:
            self.store.db.execute(
                "INSERT INTO postprocessings(job_id,mode,attempts,updated_at) VALUES(?,'text',2,'test')",
                (self.job["id"],),
            )

        def fail_markdown(path, content):
            if path.name == "processed.md":
                raise OSError("disk busy after durable response")
            atomic_write(path, content)

        with patch("tubescribe.postprocessing.atomic_write", side_effect=fail_markdown):
            self.assertEqual(self.full()["failed"], 1)
        self.assertEqual(self.row()["stage"], "save")
        self.assertEqual(self.row()["attempts"], 3)
        self.assertEqual(self.requests, 1)

        with self.store.db:
            self.store.db.execute("UPDATE postprocessings SET next_retry=0")
        self.health.side_effect = AssertionError("ASR stopped")
        self.ready_diarization.side_effect = AssertionError("Diarization stopped")
        result = work(
            self.store,
            "http://127.0.0.1:1",
            ids=[VID],
            bridge="http://127.0.0.1:1",
            postprocessing="text",
        )
        self.assertEqual(result["completed"], 1)
        self.assertEqual(self.row()["attempts"], 3)
        self.assertEqual(self.requests, 1)

    def test_rejected_model_output_does_not_consume_save_budget(self):
        self.invalid_output = True
        for attempts in (1, 2):
            self.assertEqual(self.full()["failed"], 1)
            self.assertEqual(self.row()["stage"], "process")
            self.assertEqual(self.row()["attempts"], attempts)
            self.assertEqual(self.row()["save_attempts"], 0)
            self.assertIsNone(self.row()["output_dir"])
            with self.store.db:
                self.store.db.execute("UPDATE postprocessings SET next_retry=0")
        self.invalid_output = False
        self.assertEqual(self.full()["completed"], 1)
        self.assertEqual(self.row()["attempts"], 3)
        self.assertEqual(self.requests, 3)

    def test_wrong_channel_video_is_quarantined_before_cache_reuse(self):
        folder = self.store.root / "videos"
        folder.mkdir()
        path = folder / (VID + ".mp4")

        def mismatched_download(*_):
            path.write_bytes(b"wrong-channel video")
            return path, {"channel_id": "wrong"}

        with patch(
            "tubescribe.postprocessing_worker.download_video",
            side_effect=mismatched_download,
        ):
            with self.assertRaisesRegex(ValueError, "channel identity"):
                cached_video(self.store, self.job, None)
        self.assertFalse(path.exists())
        self.assertEqual(len(list(folder.glob("*.rejected-*.mp4"))), 1)

    @unittest.skipUnless(shutil.which("ffmpeg"), "FFmpeg required for video mode")
    def test_default_pipeline_delivers_native_image_and_dashboard_serves_complete_bundle(self):
        folder = self.store.root / "videos"
        folder.mkdir()
        subprocess.run(
            [
                "ffmpeg",
                "-v",
                "error",
                "-f",
                "lavfi",
                "-i",
                "color=c=green:s=64x32:d=4",
                "-c:v",
                "mpeg4",
                str(folder / (VID + ".mp4")),
            ],
            check=True,
        )
        self.inspect_video = True
        result = work(self.store, "http://127.0.0.1:1", ids=[VID], bridge=self.bridge)
        self.assertEqual(result["completed"], 1)
        self.assertEqual(self.row()["figures"], 1)
        server = make_server(self.store.root)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            base = f"http://127.0.0.1:{server.server_port}"
            job = snapshot(self.store.root)["jobs"][0]
            self.assertEqual(job["stage"], "complete")
            self.assertEqual(snapshot(self.store.root)["summary"]["complete"], 1)
            response = httpx.get(base + job["processed_url"], trust_env=False)
            self.assertEqual(response.status_code, 200)
            self.assertIn("figures/frame_0001000.png", response.text)
            self.assertIn("Source SRT", response.text)
            self.assertIn("script-src 'none'", response.headers["content-security-policy"])
            prefix = f"/processed/{job['job_id']}/"
            image = httpx.get(base + prefix + "figures/frame_0001000.png", trust_env=False)
            self.assertEqual(image.headers["content-type"], "image/png")
            self.assertTrue(image.content.startswith(b"\x89PNG"))
            bundle = httpx.get(base + prefix + "processed-with-figures.zip", trust_env=False)
            self.assertEqual(bundle.headers["content-type"], "application/zip")
            self.assertTrue(bundle.content.startswith(b"PK"))
            self.assertEqual(
                httpx.get(base + prefix + "source.srt", trust_env=False).content,
                (self.store.root / job["json_path"]).with_suffix(".srt").read_bytes(),
            )
            self.assertEqual(
                httpx.get(
                    base + f"/processed/{job['job_id'] + 1}/preview.html", trust_env=False
                ).status_code,
                404,
            )
            self.assertEqual(
                httpx.get(base + prefix + "request.json", trust_env=False).status_code, 404
            )
        finally:
            server.shutdown()
            server.server_close()
            thread.join()


if __name__ == "__main__":
    unittest.main()
