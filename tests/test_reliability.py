"""Durable outages preserve state; verified backups and operations remain portable."""

import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

import httpx
import test_postprocessing_pipeline as pipeline

from tubescribe.artifacts import build_one
from tubescribe.backup import export_archive, restore_archive
from tubescribe.blobs import StorageUnavailable
from tubescribe.dashboard import make_server
from tubescribe.operations import metrics, prometheus, readiness
from tubescribe.storage import Store
from tubescribe.submission import Dispatcher, Inline, JobRequest


class Reliability(unittest.TestCase):
    def setUp(self):
        self.fixture = pipeline.FinalStage()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.fixture.full()
        self.store = self.fixture.store
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)

    def test_outage_does_not_become_missing_or_corrupt_and_request_requeues(self):
        build_one(self.store, self.fixture.job["id"], "summary", bridge=self.fixture.bridge)
        before = self.store.pipeline_jobs()
        request = JobRequest("artifacts", {"workflow": "notes"})
        identifier = Dispatcher(self.store).submit(request)
        with patch.object(self.store.blobs, "get", side_effect=StorageUnavailable("offline")):
            with self.assertRaises(StorageUnavailable):
                self.store.exists("some/key", "0" * 64)
            with self.assertRaises(StorageUnavailable):
                self.store.recover()
            with self.assertRaises(StorageUnavailable):
                Dispatcher(self.store).execute(
                    request, Inline(lambda: self.store.recover()), identifier=identifier
                )
        after = self.store.pipeline_jobs()
        for old, new in zip(before, after, strict=True):
            for key in ("asr_state", "diarization_state", "postprocessing_state", "summary_state"):
                self.assertEqual(old[key], new[key])
        self.assertEqual(self.store.submission(identifier)["state"], "queued")
        self.assertEqual(self.fixture.requests, 2)
        self.store.recover()
        Dispatcher(self.store).execute(
            request, Inline(lambda: {"completed": 1, "failed": 0}), identifier=identifier
        )
        self.assertEqual(self.store.submission(identifier)["state"], "complete")
        self.assertIsNone(self.store.submission(identifier)["error"])

    def test_packaging_outage_resumes_without_model_or_attempt_budget(self):
        original = self.store.blobs.put

        def fail_package(key, data):
            if key.startswith("artifacts/") and key.endswith("artifact.md"):
                raise StorageUnavailable("offline")
            return original(key, data)

        with patch.object(self.store.blobs, "put", side_effect=fail_package):
            with self.assertRaises(StorageUnavailable):
                build_one(self.store, self.fixture.job["id"], "summary", bridge=self.fixture.bridge)
        row = self.store.stage_row("artifact", self.fixture.job["id"], workflow="summary")
        self.assertEqual(row["state"], "pending")
        self.assertEqual(row["save_attempts"], 0)
        self.assertEqual(row["attempts"], 1)
        self.assertEqual(self.fixture.requests, 2)
        build_one(self.store, self.fixture.job["id"], "summary", bridge=self.fixture.bridge)
        self.assertEqual(self.fixture.requests, 2)

    def test_backup_restore_verifies_integrity_and_preserves_queued_work(self):
        build_one(self.store, self.fixture.job["id"], "summary", bridge=self.fixture.bridge)
        identifier = Dispatcher(self.store).submit(JobRequest("artifacts", {"workflow": "notes"}))
        backup = self.base / "backup"
        export_archive(self.store, backup)
        target = Store(self.base / "restore")
        self.addCleanup(target.db.close)
        restore_archive(target, backup)
        target.recover()
        self.assertEqual(target.status_data()["completed_jobs"], 1)
        self.assertEqual(target.submission(identifier)["state"], "queued")
        self.assertEqual(self.fixture.requests, 2)
        with self.assertRaisesRegex(ValueError, "empty archive"):
            restore_archive(target, backup)
        (backup / "archive.sqlite3").write_bytes(b"corrupt")
        empty = Store(self.base / "empty")
        self.addCleanup(empty.db.close)
        with self.assertRaisesRegex(ValueError, "checksum"):
            restore_archive(empty, backup)
        self.assertEqual(empty.status_data()["transcripts"]["count"], 0)

    def test_authenticated_readiness_and_metrics_report_outages(self):
        build_one(self.store, self.fixture.job["id"], "summary", bridge=self.fixture.bridge)
        self.assertTrue(readiness(self.store.root)["ready"])
        data = metrics(self.store.root)
        self.assertGreater(sum(r["tokens"] for r in data["usage"]), 0)
        build_one(self.store, self.fixture.job["id"], "summary", bridge=self.fixture.bridge)
        self.assertEqual(metrics(self.store.root)["usage"], data["usage"])
        self.assertIn("tubescribe_tokens", prometheus(data))
        token = "operations-rehearsal-" + "a" * 32
        with patch.dict("os.environ", {"TUBESCRIBE_ACCESS_TOKEN": token}):
            server = make_server(self.store.root)
            threading.Thread(target=server.serve_forever, daemon=True).start()
            self.addCleanup(server.server_close)
            self.addCleanup(server.shutdown)
            base = f"http://127.0.0.1:{server.server_port}"
            self.assertEqual(httpx.get(base + "/api/ready", trust_env=False).status_code, 401)
            headers = {"Authorization": "Bearer " + token}
            for path in ("/api/ready", "/api/metrics", "/metrics"):
                self.assertEqual(
                    httpx.get(base + path, headers=headers, trust_env=False).status_code, 200
                )
            with patch(
                "tubescribe.operations.LocalArtifacts.probe",
                side_effect=StorageUnavailable("offline"),
            ):
                reply = httpx.get(base + "/api/ready", headers=headers, trust_env=False)
                self.assertEqual(reply.status_code, 503)
                self.assertEqual(reply.json()["checks"], {"database": True, "objects": False})
                self.assertEqual(
                    httpx.get(base + "/api/health", headers=headers, trust_env=False).status_code,
                    200,
                )
