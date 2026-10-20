"""Executable cloud seams, ownership recovery and delivery contracts."""

import json
import sqlite3
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
    def test_heartbeat_reconnects_after_database_error_but_never_reacquires_expired_owner(self):
        from tubescribe.execution import _heartbeat

        broken, healthy = Mock(), Mock()
        broken.renew.side_effect = sqlite3.OperationalError("connection lost")
        healthy.renew.side_effect = [True, False]
        stop = Mock()
        stop.wait.return_value = False
        with patch("tubescribe.execution.Claims", side_effect=[broken, healthy]) as connect:
            _heartbeat(self.root, "feed-watcher", "original-owner", 90, stop)
        self.assertEqual(connect.call_count, 2)
        self.assertEqual([c.args[0] for c in stop.wait.call_args_list], [30, 5, 30])
        self.assertEqual(healthy.renew.call_count, 2)
        healthy.renew.assert_called_with("feed-watcher", "original-owner", ttl=90)
        broken.close.assert_called_once()
        healthy.close.assert_called_once()
        broken.acquire.assert_not_called()
        healthy.acquire.assert_not_called()

    def test_scoped_recovery_only_resets_selected_profile_and_jobs(self):
        other = "lmnopqrstuv"
        self.store.index(CID, "upload", [{"id": other, "title": "Other", "url": "https://youtube.com/watch?v=" + other}], "turbo-v1")
        self.store.index(CID, "upload", [{"id": VID, "title": "Test", "url": "https://youtube.com/watch?v=" + VID}], "other-profile")
        with self.store.db:
            self.store.db.execute("UPDATE jobs SET state='active',asr_state='active'")
        self.store.recover(ids=[VID], profile="turbo-v1")
        selected = self.store.pipeline_jobs(ids=[VID])[0]
        self.assertEqual(selected["state"], "pending")
        self.assertEqual(self.store.pipeline_jobs(ids=[other])[0]["state"], "active")
        self.assertEqual(self.store.pipeline_jobs(ids=[VID], profile="other-profile")[0]["state"], "active")
        self.store.recover(ids=[])
        self.assertEqual(self.store.pipeline_jobs(ids=[other])[0]["state"], "active")
        self.store.recover()
        self.assertEqual(self.store.pipeline_jobs(ids=[other])[0]["state"], "pending")

    def test_shared_download_cooldown_is_durable_and_never_shortened(self):
        claims = Claims(self.root)
        self.addCleanup(claims.close)
        claims.defer_downloads(60)
        deadline = claims.db.execute(
            "SELECT expires FROM claims WHERE resource='backoff:download'"
        ).fetchone()[0]
        claims.defer_downloads(1)
        self.assertEqual(
            claims.db.execute(
                "SELECT expires FROM claims WHERE resource='backoff:download'"
            ).fetchone()[0],
            deadline,
        )
        self.assertGreater(claims.download_delay(), 50)
        with claims.db:
            claims.db.execute("UPDATE claims SET expires=0 WHERE resource='backoff:download'")
        self.assertEqual(claims.download_delay(), 0)

    def test_recovery_materialization_reuses_only_verified_copies_in_current_scan(self):
        blobs = MemoryArtifacts()
        refs = [blobs.put(f"bundle/{i}.json", str(i).encode()) for i in range(10)]
        store = Store(self.root / "remote", blobs=blobs)
        self.addCleanup(store.db.close)

        def scan():
            store.materialize_prefix("bundle")
            store.materialize_prefix("bundle")

        with (
            patch.object(store, "_recover", side_effect=scan),
            patch.object(blobs, "copy_to", wraps=blobs.copy_to) as copies,
        ):
            store.recover()
            self.assertEqual(copies.call_count, 10)
            self.assertIsNone(store._recovery_materialized)
            # A subsequent scan must fetch and verify remote content again.
            blobs.put(refs[0].key, b"changed")
            store.recover()
            self.assertEqual(copies.call_count, 20)
            self.assertEqual((store.root / refs[0].key).read_bytes(), b"changed")
        with patch.object(store, "_recover", side_effect=ValueError("failure")):
            with self.assertRaises(ValueError):
                store.recover()
        self.assertIsNone(store._recovery_materialized)

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
            self.store.db.execute("UPDATE submissions SET updated=0,state='active',pid=321")
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
        self.assertIn("--video=" + VID, argv)
        self.assertNotIn("shell", spawn.call_args.kwargs)

    def test_local_process_preserves_dash_prefixed_video_ids(self):
        import argparse

        spawn = Mock(return_value=Mock(pid=321))
        request = JobRequest("work", {"video": ["-r8aNg-uH3g", VID]})
        LocalProcess(spawn=spawn).start(self.root, request, "id")
        argv = spawn.call_args.args[0]
        parser = argparse.ArgumentParser()
        parser.add_argument("--video", action="append")
        selected = parser.parse_args([value for value in argv if value.startswith("--video=")])
        self.assertEqual(selected.video, request.options["video"])

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

    def test_agent_runner_preserves_upstream_failure_detail(self):
        runner = AgentRunner(
            "http://127.0.0.1:1",
            transport=httpx.MockTransport(
                lambda _: httpx.Response(502, json={"detail": "Run exceeded its tool-call budget"})
            ),
        )
        with self.assertRaisesRegex(httpx.HTTPStatusError, "tool-call budget"):
            runner.run(
                {"timeout_seconds": 30}, RemoteTools("https://tools.example/callback", "secret")
            )

    def test_durable_terminal_failure_is_distinct_from_uncertain_or_cancelled(self):
        from tubescribe.agent_gateway import DurableAgentRunner, DurableRunFailed

        for state in ("failed", "uncertain", "cancelled"):
            with self.subTest(state=state):
                runner = DurableAgentRunner(
                    "http://127.0.0.1:1",
                    store=self.store,
                    run_key="runs/" + state,
                    transport=httpx.MockTransport(
                        lambda _, value=state: httpx.Response(200, json={"state": value})
                    ),
                )
                with self.assertRaises(RuntimeError) as caught:
                    runner.run({"timeout_seconds": 30}, None)
                self.assertEqual(isinstance(caught.exception, DurableRunFailed), state == "failed")

    def test_durable_status_retries_reconnect_to_same_run_without_new_model_request(self):
        from tubescribe.agent_gateway import DurableAgentRunner

        calls = []
        replies = iter([
            httpx.Response(503), httpx.Response(202, json={"state": "active"}),
            httpx.ReadTimeout("temporary network loss"),
            httpx.Response(200, json={"state": "complete", "result": {"output": {"markdown": "done"}}}),
        ])

        def transport(request):
            calls.append((request.method, str(request.url), request.content))
            reply = next(replies)
            if isinstance(reply, Exception):
                raise reply
            return reply

        runner = DurableAgentRunner("http://127.0.0.1:1", store=self.store,
                                    run_key="runs/reconnect", transport=httpx.MockTransport(transport))
        with patch("tubescribe.agent_gateway.time.sleep"):
            result = runner.run({"timeout_seconds": 30}, None)
        self.assertEqual(result["output"]["markdown"], "done")
        self.assertEqual([c[0] for c in calls], ["POST", "POST", "GET", "GET"])
        self.assertEqual(len({c[1] for c in calls}), 1)
        self.assertEqual(calls[0][2], calls[1][2])

    def test_durable_unavailable_gateway_has_bounded_transport_attempts(self):
        from tubescribe.agent_gateway import DurableAgentRunner

        transport = Mock(return_value=httpx.Response(503))
        runner = DurableAgentRunner("http://127.0.0.1:1", store=self.store,
                                    run_key="runs/unavailable", transport=httpx.MockTransport(transport))
        with patch("tubescribe.agent_gateway.time.sleep"), self.assertRaises(httpx.HTTPStatusError):
            runner.run({"timeout_seconds": 30}, None)
        self.assertEqual(transport.call_count, 3)

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
