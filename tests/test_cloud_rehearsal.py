"""Separate scratch/processes and failure boundaries, with no paid inference."""

import hashlib
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import Mock, patch

import httpx
import test_postprocessing_pipeline as pipeline
from test_pipeline import VID

from tubescribe.agent_gateway import DurableAgentRunner, Runs, make_gateway
from tubescribe.artifacts import build_one, complete_valid
from tubescribe.blobs import DirectoryObjects, MemoryArtifacts
from tubescribe.config import RuntimeConfig
from tubescribe.dashboard import make_server
from tubescribe.execution import lease
from tubescribe.maintenance import reconcile
from tubescribe.storage import Store, worker_lock
from tubescribe.submission import Dispatcher, Inline, JobRequest


class CloudRehearsal(unittest.TestCase):
    def setUp(self):
        self.fixture = pipeline.FinalStage()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.fixture.full()
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.objects = self.base / "objects"
        self.blobs = DirectoryObjects(self.objects)
        for key in self.fixture.store.blobs.keys():
            if key.startswith(("transcripts/", "artifacts/", "workflows/")):
                self.blobs.put(key, self.fixture.store.read_key(key))
        self.database = self.fixture.store.database
        self.env = {
            "TUBESCRIBE_DATABASE": str(self.database),
            "TUBESCRIBE_OBJECTS": str(self.objects),
            "TUBESCRIBE_DISPATCH": "queue",
            "TUBESCRIBE_WORKER_POLL": "0.05",
        }
        self.env_patch = patch.dict(os.environ, self.env)
        self.env_patch.start()
        self.addCleanup(self.env_patch.stop)
        self.worker = Store(self.base / "worker")
        self.addCleanup(self.worker.db.close)
        self.job = self.fixture.job

    def cli(self, root, *arguments):
        return [sys.executable, "-m", "tubescribe.cli", "--data", str(root), *arguments]

    def test_memory_objects_recover_without_any_original_filesystem_paths(self):
        memory = MemoryArtifacts()
        for key in self.blobs.keys():
            memory.put(key, self.blobs.get(key))
        store = Store(self.base / "memory-worker", database=self.database, blobs=memory)
        self.addCleanup(store.db.close)
        self.assertFalse(hasattr(memory, "path"))
        with worker_lock(store.root):
            store.recover()
        self.assertEqual(store.stage_row("postprocessing", self.job["id"])["state"], "complete")
        build_one(store, self.job["id"], "summary", bridge=self.fixture.bridge)
        self.assertTrue(
            complete_valid(store, store.stage_row("artifact", self.job["id"], workflow="summary"))
        )
        self.assertEqual(self.fixture.transcribe.call_count, 1)

    def test_queued_request_survives_web_exit_and_real_worker_delivers_from_separate_scratch(self):
        process = Mock()
        request = JobRequest("artifacts", {"video": [VID], "bridge": self.fixture.bridge})
        identifier = Dispatcher(self.worker, process=process).submit(request, background=True)
        process.start.assert_not_called()
        with self.worker.db:
            self.worker.db.execute("UPDATE submissions SET updated=0 WHERE id=?", (identifier,))
        self.assertEqual(Dispatcher(self.worker).submit(request), identifier)
        result = subprocess.run(
            self.cli(self.base / "replacement", "queue", "once"), capture_output=True, timeout=20
        )
        self.assertEqual(result.returncode, 0, result.stdout.decode() + result.stderr.decode())
        self.assertEqual(self.worker.submission(identifier)["state"], "complete")
        duplicate = Mock()
        Dispatcher(self.worker).execute(request, Inline(duplicate), identifier=identifier)
        duplicate.assert_not_called()
        server = make_server(self.base / "web")
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        url = f"http://127.0.0.1:{server.server_port}"
        with httpx.Client(trust_env=False) as client:
            snapshot = client.get(url + "/api/snapshot").json()
            self.assertEqual(snapshot["jobs"][0]["summary_state"], "complete")
            for route in [
                f"/artifacts/{self.job['id']}/summary/",
                f"/processed/{self.job['id']}/preview.html",
                f"/transcripts/{self.job['id']}/srt",
            ]:
                response = client.get(url + route)
                self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(self.fixture.transcribe.call_count, 1)
        self.assertFalse((self.base / "web" / "archive.sqlite3").exists())

    def test_sigkill_during_packaging_resumes_on_replacement_without_model_repeat(self):
        marker = self.base / "packaging.marker"
        wrapper = (
            "import runpy,time; from pathlib import Path; from tubescribe import artifacts as m\n"
            "original=m.atomic_write\n"
            "def pause(path,content):\n"
            " if path.name=='artifact.md':\n"
            f"  Path({str(marker)!r}).write_text('saving'); time.sleep(30)\n"
            " return original(path,content)\n"
            "m.atomic_write=pause\nrunpy.run_module('tubescribe.cli',run_name='__main__')\n"
        )
        argv = self.cli(
            self.worker.root, "artifacts", "--video", VID, "--bridge", self.fixture.bridge
        )
        child = subprocess.Popen(
            [sys.executable, "-c", wrapper, *argv[3:]],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
        )
        try:
            deadline = time.monotonic() + 10
            while not marker.exists() and time.monotonic() < deadline:
                time.sleep(0.02)
            self.assertTrue(marker.exists())
            os.kill(child.pid, signal.SIGKILL)
            child.communicate(timeout=5)
        finally:
            if child.poll() is None:
                child.kill()
                child.communicate(timeout=5)
        calls = self.fixture.requests
        # Simulate expiry after the killed process stopped; do not sleep a full lease TTL.
        with self.worker.db:
            self.worker.db.execute("UPDATE claims SET expires=0")
        shutil.rmtree(self.worker.root)
        result = subprocess.run(
            self.cli(
                self.base / "replacement",
                "artifacts",
                "--video",
                VID,
                "--bridge",
                "http://127.0.0.1:9",
            ),
            capture_output=True,
            timeout=15,
        )
        self.assertEqual(result.returncode, 0, result.stdout.decode() + result.stderr.decode())
        self.assertEqual(self.fixture.requests, calls)
        self.assertEqual(
            self.worker.stage_row("artifact", self.job["id"], workflow="summary")["state"],
            "complete",
        )

    def test_publication_survives_pointer_failure_and_publishes_new_immutable_bundle(self):
        original = self.worker.stage_update

        def fail(stage_name, job_id, **values):
            if stage_name == "artifact" and values.get("state") == "complete":
                raise OSError("Commit lost after publication")
            return original(stage_name, job_id, **values)

        with patch.object(self.worker, "stage_update", side_effect=fail):
            with self.assertRaises(OSError):
                build_one(self.worker, self.job["id"], "summary", bridge=self.fixture.bridge)
        calls = self.fixture.requests
        fresh = Store(self.base / "replacement")
        self.addCleanup(fresh.db.close)
        build_one(fresh, self.job["id"], "summary", bridge="http://127.0.0.1:9")
        self.assertEqual(self.fixture.requests, calls)
        self.assertTrue(
            complete_valid(fresh, fresh.stage_row("artifact", self.job["id"], workflow="summary"))
        )
        self.assertTrue(reconcile(fresh)["orphaned_bundles"])

    def test_shared_database_fences_claims_across_scratch_directories(self):
        other = Store(self.base / "other")
        self.addCleanup(other.db.close)
        with lease(self.worker.root, "archive"):
            with self.assertRaises(RuntimeError):
                with worker_lock(other.root):
                    pass

    def test_bearer_auth_protects_every_archive_route(self):
        token = "x" * 48
        with patch.dict(os.environ, {"TUBESCRIBE_ACCESS_TOKEN": token}):
            server = make_server(self.base / "authenticated-web")
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        url = f"http://127.0.0.1:{server.server_port}"
        self.assertEqual(httpx.get(url + "/api/snapshot", trust_env=False).status_code, 401)
        self.assertEqual(
            httpx.get(
                url + "/api/snapshot", headers={"Authorization": "Bearer " + token}, trust_env=False
            ).status_code,
            200,
        )
        with patch.dict(os.environ, {"TUBESCRIBE_BIND": "0.0.0.0"}, clear=False):
            with self.assertRaises(ValueError):
                RuntimeConfig.load(self.base / "unprotected")

    def test_shared_workflow_events_and_video_cleanup_survive_missing_scratch(self):
        import json

        from tubescribe.artifacts import inputs, workflows
        from tubescribe.dashboard import recent_events
        from tubescribe.postprocessing_worker import cleanup_video

        definition = {
            "title": "Takeaways",
            "instructions": "Extract the main takeaways",
            "max_words": 300,
        }
        self.blobs.put("workflows/takeaways.json", json.dumps(definition).encode())
        fresh = self.base / "empty-web"
        self.assertEqual([w["name"] for w in workflows(fresh)], ["summary", "takeaways"])
        self.assertEqual(inputs(self.worker, self.job["id"], "takeaways")[1]["title"], "Takeaways")
        self.worker.record_event("request-test", "worker_checkpoint", {"job_id": self.job["id"]})
        self.assertEqual(recent_events(fresh)[0]["run_id"], "request-test")
        key = "videos/" + self.job["video_id"] + ".mp4"
        self.blobs.put(key, b"durable media")
        self.assertFalse((self.worker.root / "videos").exists())
        with patch.object(
            self.blobs, "get", side_effect=AssertionError("metadata must not read bytes")
        ):
            self.assertTrue(self.worker.exists(key))
            cleanup_video(self.worker, self.job["video_id"])
        self.assertFalse(self.worker.exists(key))

    def test_diarization_retry_reuses_durable_audio_after_worker_replacement(self):
        from test_pipeline import DIARIZATION

        from tubescribe.diarization_worker import enrich_one

        job = self.worker.stage_inputs("diarization", ids=[self.job["video_id"]])[0]
        media = self.worker.root / "audio" / (job["video_id"] + ".m4a")
        media.parent.mkdir(parents=True)
        media.write_bytes(b"retry audio source")
        key = self.worker.persist_media(media)
        self.worker.stage_update(
            "diarization",
            job["id"],
            state="pending",
            prediction_path=None,
            prediction_sha256=None,
            audio_path=key,
        )
        shutil.rmtree(self.worker.root)
        replacement = Store(self.base / "audio-replacement")
        self.addCleanup(replacement.db.close)
        client = Mock()
        client.health.return_value = DIARIZATION
        client.diarize.return_value = DIARIZATION
        with patch(
            "tubescribe.diarization_worker.download",
            side_effect=AssertionError("must reuse saved audio"),
        ):
            self.assertTrue(enrich_one(replacement, job, client))
        self.assertEqual(client.diarize.call_count, 1)
        self.assertFalse(replacement.exists(key))
        self.assertFalse(replacement.exists(key + ".reference.json"))
        self.assertFalse((replacement.root / key).exists())

    def test_runtime_rejects_unusable_configuration(self):
        for variables in [
            {"TUBESCRIBE_WORKER_POLL": "nan"},
            {"TUBESCRIBE_AGENT_CONCURRENCY": "0"},
            {"TUBESCRIBE_ACCESS_TOKEN": "short"},
            {"TUBESCRIBE_PUBLIC_ORIGIN": "http://archive.example"},
        ]:
            with self.subTest(variables=variables), patch.dict(os.environ, variables):
                with self.assertRaises(ValueError):
                    RuntimeConfig.load(self.base)

    def test_numbered_migrations_are_repeatable(self):
        self.assertEqual(
            [
                r[0]
                for r in self.worker.db.execute(
                    "SELECT version FROM schema_migrations ORDER BY version"
                )
            ],
            [1, 2, 3, 4],
        )
        another = Store(self.base / "migrated")
        self.addCleanup(another.db.close)
        self.assertEqual(another.stage_row("postprocessing", self.job["id"])["state"], "complete")

    def test_durable_gateway_returns_lost_response_once_and_owns_tool_callback(self):
        token = "g" * 48
        with patch.dict(
            os.environ, {"TUBESCRIBE_ACCESS_TOKEN": token, "TUBESCRIBE_AGENT_PROTOCOL": "durable"}
        ):
            gateway_store = Store(self.base / "gateway")
            self.addCleanup(gateway_store.db.close)
            server = make_gateway(gateway_store.root, self.fixture.bridge)
            caller = Store(self.base / "caller")
            self.addCleanup(caller.db.close)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        source = self.worker.stage_row("postprocessing", self.job["id"])
        request = {
            "model": "codex/gpt-6.1-sol",
            "reasoning_effort": "high",
            "instructions": "Summarize",
            "input": "A real transcript.",
            "tools": [{"name": "figure"}],
            "output_schema": {"properties": {"evidence": {}}},
            "max_tool_calls": 3,
            "timeout_seconds": 30,
            "tool_context": {
                "kind": "artifact",
                "source_key": source["output_dir"],
                "resources": {},
            },
        }
        url = f"http://127.0.0.1:{server.server_port}"
        key = "artifacts/test/run"
        identifier = hashlib.sha256(key.encode()).hexdigest()
        # Submit, lose the caller's connection/lifetime, then reconnect with a new caller.
        response = httpx.post(
            url + "/api/v1/durable/runs/" + identifier,
            json=request,
            headers={"Authorization": "Bearer " + token},
            trust_env=False,
        )
        self.assertIn(response.status_code, [200, 202])
        result = DurableAgentRunner(url, store=caller, run_key=key).run(request, None)
        calls = self.fixture.requests
        self.assertEqual(
            DurableAgentRunner(url, store=caller, run_key=key).run(request, None), result
        )
        self.assertEqual(self.fixture.requests, calls)
        changed = {**request, "input": "different"}
        self.assertEqual(
            httpx.post(
                url + "/api/v1/durable/runs/" + identifier,
                json=changed,
                headers={"Authorization": "Bearer " + token},
                trust_env=False,
            ).status_code,
            400,
        )
        with closing(Runs(gateway_store.root)) as runs:
            self.assertEqual(runs.get(identifier)["state"], "complete")

    def test_durable_gateway_runs_screenshot_postprocessing_and_summary_from_separate_scratch(self):
        from tubescribe.postprocessing_worker import process_one

        token = "s" * 48
        with patch.dict(
            os.environ, {"TUBESCRIBE_ACCESS_TOKEN": token, "TUBESCRIBE_AGENT_PROTOCOL": "durable"}
        ):
            gateway_store = Store(self.base / "gateway")
            self.addCleanup(gateway_store.db.close)
            server = make_gateway(gateway_store.root, self.fixture.bridge)
            caller = Store(self.base / "video-caller")
            self.addCleanup(caller.db.close)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        video = caller.root / "videos" / f"{VID}.mp4"
        video.parent.mkdir()
        subprocess.run(
            [
                "ffmpeg",
                "-hide_banner",
                "-loglevel",
                "error",
                "-f",
                "lavfi",
                "-i",
                "color=c=blue:s=320x240:d=4",
                "-y",
                str(video),
            ],
            check=True,
            capture_output=True,
        )
        with worker_lock(caller.root):
            caller.stage_update(
                "postprocessing",
                self.job["id"],
                state="pending",
                output_dir=None,
                attempts=0,
                save_attempts=0,
            )
        self.fixture.inspect_video = True
        bridge = f"http://127.0.0.1:{server.server_port}"
        self.assertTrue(process_one(caller, self.job, bridge=bridge, mode="video"))
        row = caller.stage_row("postprocessing", self.job["id"])
        self.assertEqual(row["figures"], 1)
        self.assertTrue(caller.bundle_valid(row["output_dir"], row["manifest_sha256"]))
        self.assertFalse(video.exists())
        self.assertFalse(caller.exists("videos/" + video.name))
        self.fixture.inspect_video = False
        self.assertTrue(build_one(caller, self.job["id"], "summary", bridge=bridge))
        self.assertEqual(self.fixture.transcribe.call_count, 1)
        self.assertEqual(
            caller.stage_row("artifact", self.job["id"], workflow="summary")["state"], "complete"
        )

    def test_gateway_status_reconciles_claim_that_expires_after_startup(self):
        from tubescribe.execution import Claims

        token = "l" * 48
        root = self.base / "late-expiry-gateway"
        identifier = "b" * 64
        with closing(Runs(root)) as runs:
            runs.create(identifier, {"tool_context": {"kind": "artifact"}})
            runs.update(identifier, state="active")
        claims = Claims(root)
        self.addCleanup(claims.close)
        self.assertTrue(claims.acquire("agent-run:" + identifier, "old-gateway", ttl=90))
        with patch.dict(os.environ, {"TUBESCRIBE_ACCESS_TOKEN": token}):
            server = make_gateway(root, self.fixture.bridge)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        url = f"http://127.0.0.1:{server.server_port}/api/v1/durable/runs/{identifier}"
        headers = {"Authorization": "Bearer " + token}
        self.assertEqual(httpx.get(url, headers=headers, trust_env=False).json()["state"], "active")
        with claims.db:
            claims.db.execute(
                "UPDATE claims SET expires=0 WHERE resource=?", ("agent-run:" + identifier,)
            )
        self.assertEqual(
            httpx.get(url, headers=headers, trust_env=False).json()["state"], "uncertain"
        )
        self.assertEqual(self.fixture.requests, 1)

    def test_queued_gateway_cancellation_prevents_an_upstream_request(self):
        from tubescribe.agent_gateway import execute

        runs = Runs(self.worker.root)
        self.addCleanup(runs.close)
        identifier = "b" * 64
        runs.create(identifier, {"tool_context": {"kind": "artifact"}})
        runs.update(identifier, cancelled=1)
        runner = Mock()
        execute(self.worker.root, identifier, self.fixture.bridge, runner_factory=runner)
        runner.assert_not_called()
        self.assertEqual(runs.get(identifier)["state"], "cancelled")

    def test_gateway_recovery_never_repeats_an_uncertain_upstream_run(self):
        runs = Runs(self.worker.root)
        self.addCleanup(runs.close)
        identifier = "a" * 64
        runs.create(identifier, {"input": "hello"})
        runs.update(identifier, state="active")
        self.assertEqual(runs.recover(), [])
        self.assertEqual(runs.get(identifier)["state"], "uncertain")
