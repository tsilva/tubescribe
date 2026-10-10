"""Versioned archive upgrades preserve published files and durable work."""

import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

import httpx
import test_postprocessing_pipeline as pipeline
from upgrade_fixture import SQL, write_v4

from tubescribe.artifacts import build_one
from tubescribe.dashboard import make_server
from tubescribe.migrations import SCHEMA_VERSION, SchemaCompatibilityError, manage, migrate
from tubescribe.operations import readiness
from tubescribe.storage import Store
from tubescribe.submission import Dispatcher, JobRequest


class SchemaUpgrades(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.path = self.root / "archive.sqlite3"
        environment = patch.dict(os.environ, {"TUBESCRIBE_MIGRATIONS": "check"})
        environment.start()
        self.addCleanup(environment.stop)
        with closing(sqlite3.connect(self.path)) as db:
            db.executescript(SQL)

    def cli(self, *args):
        return subprocess.run(
            [sys.executable, "-m", "tubescribe.cli", "--data", str(self.root), *args],
            capture_output=True,
            text=True,
            timeout=15,
        )

    def dump(self):
        with closing(sqlite3.connect(self.path)) as db:
            return list(db.iterdump())

    def test_old_schema_refuses_runtime_until_explicit_upgrade(self):
        before = self.dump()
        result = self.cli("status")
        self.assertEqual(result.returncode, 1)
        self.assertIn("tubescribe migrate apply", result.stderr)
        self.assertEqual(self.dump(), before)
        self.assertFalse(readiness(self.root)["checks"]["schema"])
        status = self.cli("migrate", "status")
        self.assertEqual(status.returncode, 1)
        self.assertEqual(json.loads(status.stdout)["schema_version"], 4)
        self.assertEqual(self.dump(), before)
        applied = self.cli("migrate", "apply")
        self.assertEqual(applied.returncode, 0, applied.stderr)
        self.assertEqual(json.loads(applied.stdout)["previous_version"], 4)
        upgraded = self.dump()
        self.assertEqual(self.cli("migrate", "apply").returncode, 0)
        self.assertEqual(self.dump(), upgraded)
        self.assertEqual(self.cli("migrate", "status").returncode, 0)
        self.assertEqual(self.cli("status").returncode, 0)
        for mode in ("auto", "check"):
            with closing(sqlite3.connect(self.path)) as db:
                db.execute("PRAGMA journal_mode=DELETE")
            with patch.dict(os.environ, {"TUBESCRIBE_MIGRATIONS": mode}):
                store = Store(self.root)
                try:
                    self.assertEqual(store.db.execute("PRAGMA journal_mode").fetchone()[0], "wal")
                    with self.assertRaises(sqlite3.IntegrityError), store.db:
                        store.db.execute(
                            "INSERT INTO discovery(channel_id,kind) VALUES('missing-channel','videos')"
                        )
                finally:
                    store.db.close()

    def test_future_schema_is_rejected_before_writes_in_both_modes(self):
        manage(self.root, "apply")
        with closing(sqlite3.connect(self.path)) as db, db:
            db.execute("INSERT INTO schema_migrations VALUES(?)", (SCHEMA_VERSION + 1,))
        before = self.dump()
        for mode in ("auto", "check"):
            with patch.dict(os.environ, {"TUBESCRIBE_MIGRATIONS": mode}):
                with self.assertRaisesRegex(SchemaCompatibilityError, "newer"):
                    Store(self.root)
        result = self.cli("migrate", "apply")
        self.assertEqual(result.returncode, 1)
        self.assertIn("newer", result.stderr)
        self.assertEqual(self.dump(), before)
        self.assertFalse(readiness(self.root)["ready"])

    def test_active_archive_and_incomplete_ledger_cannot_migrate(self):
        with closing(sqlite3.connect(self.path)) as db, db:
            db.execute("INSERT INTO claims VALUES('archive','old-worker',?)", (time.time() + 90,))
        before = self.dump()
        with self.assertRaisesRegex(SchemaCompatibilityError, "Stop services"):
            manage(self.root, "apply")
        self.assertEqual(self.dump(), before)
        with closing(sqlite3.connect(self.path)) as db, db:
            db.execute("DELETE FROM claims")
            db.execute("DELETE FROM schema_migrations WHERE version=2")
        before = self.dump()
        with self.assertRaisesRegex(SchemaCompatibilityError, "incomplete"):
            manage(self.root, "apply")
        self.assertEqual(self.dump(), before)

    def test_failed_ddl_rolls_back_schema_and_ledger(self):
        before = self.dump()
        with closing(sqlite3.connect(self.path)) as db:

            class FailDDL:
                def execute(self, sql, *args):
                    if sql == "INSERT INTO schema_migrations VALUES(5)":
                        raise sqlite3.OperationalError("simulated migration failure")
                    return db.execute(sql, *args)

                def __getattr__(self, name):
                    return getattr(db, name)

            with self.assertRaisesRegex(sqlite3.OperationalError, "simulated"):
                migrate(FailDDL())
        self.assertEqual(self.dump(), before)
        self.assertTrue(manage(self.root, "apply")["compatible"])

    def test_pre_versioned_archive_is_adopted_without_losing_records(self):
        with closing(sqlite3.connect(self.path)) as db, db:
            db.execute("DROP TABLE schema_migrations")
            db.execute(
                "INSERT INTO channels VALUES('old-channel','https://youtube.com/old','Old','2026-01-01')"
            )
        result = manage(self.root, "apply")
        self.assertEqual(result["previous_version"], 0)
        with closing(sqlite3.connect(self.path)) as db:
            self.assertEqual(db.execute("SELECT title FROM channels").fetchone()[0], "Old")
        self.assertTrue(manage(self.root, "status")["compatible"])

    def test_status_does_not_create_an_archive_and_local_auto_remains_available(self):
        shutil.rmtree(self.root)
        result = self.cli("migrate", "status")
        self.assertEqual(result.returncode, 1)
        self.assertFalse(self.root.exists())
        self.assertEqual(self.cli("status").returncode, 1)
        self.assertFalse(self.path.exists())
        with patch.dict(os.environ, {"TUBESCRIBE_MIGRATIONS": "auto"}):
            store = Store(self.root)
            store.db.close()
        self.assertTrue(manage(self.root, "status")["compatible"])


class PublishedArchiveUpgrade(unittest.TestCase):
    @unittest.skipUnless(shutil.which("ffmpeg"), "FFmpeg required for figure verification")
    def test_historical_archive_keeps_publications_queue_and_completed_inference(self):
        fixture = pipeline.FinalStage()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        fixture.full()
        source = fixture.store
        video = source.root / "upgrade-fixture.mp4"
        subprocess.run(
            [
                "ffmpeg",
                "-v",
                "error",
                "-f",
                "lavfi",
                "-i",
                "color=c=blue:s=320x180:r=10:d=4",
                "-c:v",
                "libx264",
                "-pix_fmt",
                "yuv420p",
                str(video),
            ],
            check=True,
            capture_output=True,
            timeout=15,
        )
        fixture.inspect_video = True
        source.stage_update(
            "postprocessing",
            fixture.job["id"],
            state="pending",
            mode="video",
            output_dir=None,
            manifest_path=None,
            attempts=0,
        )
        from tubescribe.postprocessing_worker import process_one

        with patch("tubescribe.postprocessing_worker.cached_video", return_value=str(video)):
            process_one(source, fixture.job, bridge=fixture.bridge, mode="video")
        build_one(source, fixture.job["id"], "summary", bridge=fixture.bridge)
        queued = Dispatcher(source).submit(JobRequest("artifacts", {"workflow": "notes"}))
        calls = fixture.requests
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            write_v4(source.db, root / "archive.sqlite3")
            content = {}
            for key in source.blobs.keys():
                if key.startswith(("transcripts/", "artifacts/", "agent-results/", "workflows/")):
                    content[key] = source.read_key(key)
                    path = root / key
                    path.parent.mkdir(parents=True, exist_ok=True)
                    path.write_bytes(content[key])
            with patch.dict(os.environ, {"TUBESCRIBE_MIGRATIONS": "check"}):
                with self.assertRaises(SchemaCompatibilityError):
                    Store(root)
                manage(root, "apply")
                store = Store(root)
                try:
                    self.assertEqual(store.submission(queued)["state"], "queued")
                    store.recover()
                    from tubescribe.worker import work

                    result = work(store, "http://127.0.0.1:1", bridge=fixture.bridge)
                    self.assertEqual(result["skipped"], 1)
                    self.assertEqual(fixture.requests, calls)
                    for key, data in content.items():
                        self.assertEqual(store.read_key(key), data, key)
                    server = make_server(root)
                    threading.Thread(target=server.serve_forever, daemon=True).start()
                    try:
                        base = f"http://127.0.0.1:{server.server_port}"
                        job_id = fixture.job["id"]
                        for path in (
                            f"/transcripts/{job_id}/srt",
                            f"/processed/{job_id}/preview.html",
                            f"/processed/{job_id}/figures/frame_0001000.png",
                            f"/artifacts/{job_id}/summary/",
                        ):
                            self.assertEqual(
                                httpx.get(base + path, trust_env=False).status_code, 200, path
                            )
                    finally:
                        server.shutdown()
                        server.server_close()
                finally:
                    store.db.close()
