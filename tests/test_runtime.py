"""Executable cloud seams, ownership recovery and delivery contracts."""

import json
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import httpx
from test_pipeline import CID, VID

from tubescribe.blobs import ArtifactRef, LocalArtifacts, MemoryArtifacts, publish_bundle
from tubescribe.execution import Claims, LeaseLost, lease
from tubescribe.inference import AgentRunner, EndpointPolicy, HardwarePolicy, RemoteTools
from tubescribe.pipeline import Plan, status
from tubescribe.publication import Delivery
from tubescribe.storage import Store, worker_lock
from tubescribe.submission import Dispatcher, Inline, JobRequest, LocalProcess


class Runtime(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.store = Store(self.root)
        self.addCleanup(self.store.db.close)
        self.store.channel(
            {"id": CID, "url": "https://youtube.com/channel/" + CID, "title": "Channel"}
        )
        self.store.index(
            CID,
            "videos",
            [{"id": VID, "title": "Video", "url": "https://youtube.com/watch?v=" + VID}],
            "turbo-v1",
        )
        self.job_id = self.store.db.execute("SELECT id FROM jobs").fetchone()[0]

    def test_storage_adapters_publish_identical_refs_and_reject_corruption(self):
        staging = self.root / "scratch"
        staging.mkdir()
        (staging / "artifact.md").write_bytes(b"hello")
        for adapter in (LocalArtifacts(self.root / "objects"), MemoryArtifacts()):
            with self.subTest(adapter=type(adapter).__name__):
                ref = publish_bundle(adapter, staging, "artifacts/run")
                manifest = json.loads(adapter.read(ref))
                key = "artifacts/run/artifact.md"
                source = ArtifactRef(key, manifest["files"]["artifact.md"])
                self.assertEqual(adapter.read(source), b"hello")
                adapter.put(key, b"changed")
                with self.assertRaisesRegex(ValueError, "reference"):
                    adapter.read(source)
                with self.assertRaises(ValueError):
                    adapter.put("../escape", b"bad")
                (staging / "artifact.md").write_bytes(b"hello")

    def test_symlink_cannot_escape_storage_or_publication(self):
        (self.root / "link").symlink_to(self.root.parent, target_is_directory=True)
        with self.assertRaises(ValueError):
            self.store.blobs.put("link/escaped", b"bad")
        staging = self.root / "scratch"
        staging.mkdir()
        (staging / "outside").symlink_to(self.root / "archive.sqlite3")
        with self.assertRaises(ValueError):
            publish_bundle(MemoryArtifacts(), staging, "artifacts/run")

    def test_expired_claim_can_be_reclaimed_but_old_owner_cannot_renew_release(self):
        claims = Claims(self.root)
        self.addCleanup(claims.close)
        resource = f"job:{self.job_id}"
        self.assertTrue(claims.acquire(resource, "old", ttl=5, clock=100))
        self.assertFalse(claims.acquire(resource, "new", clock=104))
        self.assertTrue(claims.acquire(resource, "new", ttl=20, clock=106))
        self.assertFalse(claims.renew(resource, "old", clock=107))
        claims.release(resource, "old")
        self.assertEqual(claims.db.execute("SELECT owner FROM claims").fetchone()[0], "new")

    def test_lost_lease_fences_a_real_state_commit(self):
        resource = f"job:{self.job_id}"
        with self.assertRaises(LeaseLost):
            with lease(self.root, resource):
                with self.store.db:
                    self.store.db.execute(
                        "UPDATE claims SET owner='replacement' WHERE resource=?", (resource,)
                    )
                self.store.update(self.job_id, state="complete")
        self.assertEqual(self.store.db.execute("SELECT state FROM jobs").fetchone()[0], "pending")
        self.assertEqual(
            self.store.db.execute("SELECT owner FROM claims").fetchone()[0], "replacement"
        )

    def test_heartbeat_preserves_ownership_past_initial_expiry(self):
        with lease(self.root, "test", ttl=0.3):
            time.sleep(0.4)
            self.assertGreater(
                self.store.db.execute(
                    "SELECT expires FROM claims WHERE resource='test'"
                ).fetchone()[0],
                time.time(),
            )
        self.assertIsNone(
            self.store.db.execute("SELECT owner FROM claims WHERE resource='test'").fetchone()
        )

    def test_recovery_does_not_reset_another_live_job(self):
        with lease(self.root, f"job:{self.job_id}"):
            self.store.update(self.job_id, state="active", download_state="active")
            self.store.recover()
            self.assertEqual(
                self.store.db.execute("SELECT state FROM jobs").fetchone()[0], "active"
            )
        self.store.recover()
        self.assertEqual(self.store.db.execute("SELECT state FROM jobs").fetchone()[0], "pending")

    def test_archive_capacity_is_one_and_released_after_exception(self):
        with self.assertRaisesRegex(RuntimeError, "Another worker"):
            with worker_lock(self.root):
                with worker_lock(self.root):
                    pass
        with worker_lock(self.root):
            self.assertEqual(
                self.store.db.execute(
                    "SELECT count(*) FROM claims WHERE resource='archive'"
                ).fetchone()[0],
                1,
            )

    def test_pipeline_state_budgets_and_ui_share_dependencies(self):
        row = {
            "state": "complete",
            "asr_attempts": 99,
            "diarization_state": "complete",
            "postprocessing_state": "complete",
            "summary_state": "failed",
            "summary_attempts": 3,
            "summary_save_attempts": 0,
            "summary_stage": "save",
            "summary_next_retry": 123,
        }
        self.assertEqual(Plan().due(row), 123)
        self.assertEqual(status(row), "failed")
        self.assertTrue(Plan(True, True, False).complete(row))
        row["summary_stage"] = "model"
        self.assertIsNone(Plan().due(row))
        row["summary_state"] = "complete"
        self.assertTrue(Plan().complete(row))
        self.assertEqual(status(row), "complete")

    def test_submission_deduplication_survives_dispatcher_restart(self):
        process = Mock()
        process.start.return_value = 321
        request = JobRequest("artifacts", {"video": [VID], "wait": True})
        identifier = Dispatcher(self.store, process=process).submit(request, background=True)
        same = JobRequest(
            "artifacts",
            {
                "video": [VID],
                "wait": True,
                "profile": "turbo-v1",
                "workflow": "summary",
                "bridge": None,
                "force": False,
                "max_attempts": 3,
            },
        )
        self.assertEqual(
            Dispatcher(self.store, process=process).submit(same, background=True), identifier
        )
        self.assertEqual(process.start.call_count, 1)
        result = Dispatcher(self.store).execute(
            same, Inline(lambda: {"completed": 1, "failed": 0}), identifier=identifier
        )
        self.assertEqual(result["completed"], 1)
        self.assertEqual(
            self.store.db.execute("SELECT state FROM submissions").fetchone()[0], "complete"
        )

    def test_dead_submission_requeued_and_launch_failure_is_recorded(self):
        process = Mock()
        request = JobRequest("artifacts", {"video": [VID]})
        dispatcher = Dispatcher(self.store, process=process)
        previous = dispatcher.submit(request)
        with self.store.db:
            self.store.db.execute("UPDATE submissions SET updated=0")
        process.start.side_effect = OSError("cannot spawn")
        with self.assertRaises(OSError):
            dispatcher.submit(request, background=True)
        rows = self.store.db.execute("SELECT state FROM submissions ORDER BY created").fetchall()
        self.assertEqual([r[0] for r in rows], ["interrupted", "failed"])
        self.assertNotEqual(dispatcher.submit(request), previous)

    def test_local_process_uses_argv_and_persistent_submission(self):
        spawn = Mock(return_value=Mock(pid=321))
        request = JobRequest("artifacts", {"video": [VID], "wait": True})
        self.assertEqual(LocalProcess(spawn=spawn).start(self.root, request, "id"), 321)
        argv = spawn.call_args.args[0]
        self.assertIn("--submission", argv)
        self.assertIn("id", argv)
        self.assertIn("--video", argv)
        self.assertNotIn("shell", spawn.call_args.kwargs)

    def test_agent_runner_uses_remote_adapter_without_local_callback_server(self):
        sent = []

        def handler(request):
            sent.append(json.loads(request.content))
            self.assertEqual(request.headers["Authorization"], "Bearer endpoint-secret")
            return httpx.Response(
                200, json={"output": {"markdown": "done"}, "usage": {"tokens": 1}}
            )

        runner = AgentRunner(
            "https://bridge.example",
            policy=EndpointPolicy(local=False),
            headers={"Authorization": "Bearer endpoint-secret"},
            transport=httpx.MockTransport(handler),
        )
        with patch("tubescribe.inference.ThreadingHTTPServer") as server:
            result = runner.run(
                {"input": "source", "timeout_seconds": 30, "tools": [{"name": "figure"}]},
                RemoteTools("https://tools.example/callback", "tool-secret"),
            )
            server.assert_not_called()
        self.assertEqual(result["output"]["markdown"], "done")
        self.assertEqual(sent[0]["model"], "codex/gpt-6.1-sol")
        self.assertEqual(sent[0]["reasoning_effort"], "high")
        self.assertEqual(sent[0]["tool_executor"]["token"], "tool-secret")
        with self.assertRaises(ValueError):
            AgentRunner("https://bridge.example")
        with self.assertRaises(ValueError):
            HardwarePolicy().check("cpu", "")
        HardwarePolicy(device=None, gpu=None).check("cpu", "")

    def test_logical_references_resolve_for_host_and_portable_export(self):
        references = {
            "transcript": {
                "job_id": 7,
                "key": "transcripts/channel/video/profile/postprocessing/run/processed.md",
            },
            "figures": {"frame_1": "figures/frame_1.png"},
        }
        text = "[Source](transcript:7) [Chart](figure:frame_1)"
        web, url = Delivery(base_url="https://archive.example").resolve(text, references)
        self.assertIn("(https://archive.example/processed/7/preview.html)", web)
        self.assertIn("(figures/frame_1.png)", web)
        export, url = Delivery(
            export_directory="artifacts/channel/video/profile/summary/run"
        ).resolve(text, references)
        self.assertNotIn("/processed/7/", export)
        self.assertEqual(
            (self.root / "artifacts/channel/video/profile/summary/run" / url).resolve(),
            (self.root / references["transcript"]["key"]).with_name("preview.html").resolve(),
        )

    def test_concurrent_dashboard_submissions_launch_only_once(self):
        import threading
        from concurrent.futures import ThreadPoolExecutor

        started = threading.Event()
        process = Mock()

        def spawn(*args):
            started.set()
            time.sleep(0.1)
            return 321

        process.start.side_effect = spawn
        request = JobRequest("artifacts", {"video": [VID], "wait": True})

        def submit():
            store = Store(self.root)
            try:
                return Dispatcher(store, process=process).submit(request, background=True)
            finally:
                store.db.close()

        with ThreadPoolExecutor(max_workers=2) as pool:
            first = pool.submit(submit)
            self.assertTrue(started.wait(3))
            second = pool.submit(submit)
            self.assertEqual(first.result(timeout=5), second.result(timeout=5))
        self.assertEqual(process.start.call_count, 1)

    def test_configured_inference_policy_and_transport_do_not_change_local_defaults(self):
        from test_pipeline import DIARIZATION, RESULT

        from tubescribe.asr import Client
        from tubescribe.diarization import Client as DiarizationClient

        audio = self.root / "audio.raw"
        audio.write_bytes(b"audio")
        health = {**RESULT, "service": "tubescribe-asr", "device": "cpu", "gpu_name": ""}

        def asr(request):
            return httpx.Response(200, json=health if request.url.path == "/health" else RESULT)

        client = Client(
            "https://asr.example",
            policy=EndpointPolicy(local=False),
            hardware=HardwarePolicy(device=None, gpu=None),
            transport=httpx.MockTransport(asr),
        )
        self.assertEqual(client.health()["device"], "cpu")
        self.assertEqual(client.transcribe(audio)["segments"], RESULT["segments"])
        prediction = {**DIARIZATION, "device": "cpu", "gpu": ""}

        def diarization(request):
            return httpx.Response(
                200, json={**prediction, "service": "tubescribe-diarization", "ready": True}
            )

        client = DiarizationClient(
            "https://diar.example",
            policy=EndpointPolicy(local=False),
            hardware=HardwarePolicy(device=None, gpu=None),
            transport=httpx.MockTransport(diarization),
        )
        self.assertEqual(client.health()["device"], "cpu")
        self.assertEqual(client.diarize(audio)["exclusive_turns"], DIARIZATION["exclusive_turns"])
