"""Opt-in real PostgreSQL and HTTP S3 rehearsal, with disposable test databases."""

import os
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import uuid
from pathlib import Path
from unittest.mock import patch

import httpx
import test_postprocessing_pipeline as pipeline
from test_pipeline import VID

from tubescribe.artifacts import build_one
from tubescribe.dashboard import make_server, snapshot
from tubescribe.execution import ClaimBusy, capacity, lease, pipeline_lock
from tubescribe.storage import Store
from tubescribe.submission import Dispatcher, JobRequest


@unittest.skipUnless(
    os.environ.get("TUBESCRIBE_NETWORK_TESTS") == "1", "run scripts/rehearse-network"
)
class NetworkRuntime(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import psycopg
        from moto.server import ThreadedMotoServer

        cls.base = tempfile.TemporaryDirectory(prefix="tubescribe-network-")
        cls.addClassCleanup(cls.base.cleanup)
        cls.database_url = os.environ.get("TUBESCRIBE_TEST_DATABASE_URL")
        if not cls.database_url:
            if not shutil.which("initdb") or not shutil.which("pg_ctl"):
                raise RuntimeError(
                    "Supply TUBESCRIBE_TEST_DATABASE_URL or install PostgreSQL tools"
                )
            directory = Path(cls.base.name) / "postgres"
            subprocess.run(
                ["initdb", "-D", str(directory), "-A", "trust", "--no-locale", "-E", "UTF8"],
                check=True,
                capture_output=True,
                timeout=30,
            )
            with socket.socket() as sock:
                sock.bind(("127.0.0.1", 0))
                port = sock.getsockname()[1]
            subprocess.run(
                [
                    "pg_ctl",
                    "-D",
                    str(directory),
                    "-l",
                    str(Path(cls.base.name) / "postgres.log"),
                    "-o",
                    f"-h 127.0.0.1 -p {port} -k {cls.base.name}",
                    "-w",
                    "start",
                ],
                check=True,
                capture_output=True,
                timeout=30,
            )
            cls.addClassCleanup(
                lambda: subprocess.run(
                    ["pg_ctl", "-D", str(directory), "-m", "fast", "-w", "stop"],
                    capture_output=True,
                    timeout=30,
                )
            )
            cls.database_url = f"postgresql://127.0.0.1:{port}/postgres"
        cls.admin = psycopg.connect(cls.database_url, autocommit=True)
        cls.addClassCleanup(cls.admin.close)
        cls.endpoint = os.environ.get("TUBESCRIBE_TEST_S3_ENDPOINT")
        if not cls.endpoint:
            cls.objects = ThreadedMotoServer(ip_address="127.0.0.1", port=0, verbose=False)
            cls.objects.start()
            cls.addClassCleanup(cls.objects.stop)
            host, port = cls.objects.get_host_and_port()
            cls.endpoint = f"http://{host}:{port}"

    def setUp(self):
        from urllib.parse import quote, urlencode

        import boto3
        from psycopg import sql

        self.schema = "rehearsal_" + uuid.uuid4().hex
        self.admin.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(self.schema)))
        self.addCleanup(
            lambda: self.admin.execute(
                sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(self.schema))
            )
        )
        self.bucket = "rehearsal-" + uuid.uuid4().hex
        self.environment = patch.dict(
            os.environ,
            {
                "TUBESCRIBE_DATABASE_URL": self.database_url
                + ("&" if "?" in self.database_url else "?")
                + urlencode({"options": "-c search_path=" + self.schema}, quote_via=quote),
                "TUBESCRIBE_S3_BUCKET": self.bucket,
                "TUBESCRIBE_S3_ENDPOINT": self.endpoint,
                "TUBESCRIBE_OBJECTS": "",
                "TUBESCRIBE_BIND": "127.0.0.1",
                "TUBESCRIBE_ACCESS_TOKEN": "",
                "TUBESCRIBE_DISPATCH": "queue",
                "TUBESCRIBE_MIGRATIONS": "check",
                "TUBESCRIBE_WORKER_POLL": "0.05",
                "AWS_ACCESS_KEY_ID": os.environ.get("AWS_ACCESS_KEY_ID", "rehearsal"),
                "AWS_SECRET_ACCESS_KEY": os.environ.get("AWS_SECRET_ACCESS_KEY", "rehearsal"),
                "AWS_DEFAULT_REGION": "us-east-1",
                "AWS_EC2_METADATA_DISABLED": "true",
                "TUBESCRIBE_AGENT_PROTOCOL": "sync",
            },
        )
        self.environment.start()
        self.addCleanup(self.environment.stop)
        from tubescribe.migrations import manage

        # Network services validate only. The migration role initializes each test schema.
        manage(Path(self.base.name) / self.schema, "apply")
        boto3.client("s3", endpoint_url=self.endpoint).create_bucket(Bucket=self.bucket)
        self.fixture = pipeline.FinalStage()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.store = self.fixture.store
        self.assertFalse((self.store.root / "archive.sqlite3").exists())

    def cli(self, root, *arguments):
        return [sys.executable, "-m", "tubescribe.cli", "--data", str(root), *arguments]

    def replacement(self, name):
        root = Path(self.base.name) / (self.schema + "-" + name)
        store = Store(root)
        self.addCleanup(store.db.close)
        self.addCleanup(lambda: shutil.rmtree(root, ignore_errors=True))
        return store

    def test_transfer_counts_real_buffered_postgres_values_without_consuming_rows(self):
        from tubescribe.database import connect
        from tubescribe.transfer import metrics, operation

        root = Path(self.base.name) / (self.schema + "-transfer")

        @operation("agent.status")
        def read(db):
            # Text protocol returns bytea as \\x00ff (six bytes), plus two UTF-8 bytes.
            return db.execute("SELECT 'é'::text, NULL::text, decode('00ff','hex')")

        db = connect(root)
        try:
            cursor = read(db)
            self.assertEqual(tuple(cursor.fetchone()), ("é", None, b"\x00\xff"))
            self.assertIsNone(cursor.fetchone())
        finally:
            db.close()
        record = metrics(root)["operations"][0]
        self.assertEqual(record["operation"], "agent.status")
        self.assertEqual((record["queries"], record["rows"], record["result_bytes"]), (1, 1, 8))

    def test_realtime_queries_keep_owner_scope_and_revoke_access_without_polling(self):
        import json

        from tubescribe.auth import initialize_library

        initialize_library(self.store.root, issuer="https://clerk.test", owner_email="owner@example.test")
        self.store.db.execute("UPDATE reader_library SET clerk_user_id='user_owner' WHERE id='archive'")
        query_source = subprocess.check_output([
            "node", "--input-type=module", "-e",
            "import {queries} from './src/reader/queries.js'; console.log(JSON.stringify(queries))",
        ], text=True, cwd=Path(__file__).resolve().parents[1])
        queries = json.loads(query_source)
        for name, sql in queries.items():
            sql = sql.replace("$1", "%s").replace("$2", "%s")
            with self.subTest(query=name):
                rows = self.store.db.connection.execute(sql, ("user_owner", "https://clerk.test")).fetchall()
                if name in {"channels", "feed"}:
                    self.assertTrue(rows)
                self.assertEqual(
                    self.store.db.connection.execute(sql, ("user_other", "https://clerk.test")).fetchall(), []
                )
                self.assertEqual(
                    self.store.db.connection.execute(sql, ("user_owner", "https://wrong.test")).fetchall(), []
                )
        self.store.db.execute("UPDATE reader_library SET clerk_user_id=NULL WHERE id='archive'")
        for sql in queries.values():
            sql = sql.replace("$1", "%s").replace("$2", "%s")
            self.assertEqual(self.store.db.connection.execute(sql, ("user_owner", "https://clerk.test")).fetchall(), [])

    def test_parallel_videos_overlap_agents_with_independent_connections_and_gpu_leases(self):
        from test_pipeline import CID, RESULT

        from tubescribe.inference import AgentRunner
        from tubescribe.pipeline import Plan
        from tubescribe.worker import work

        ids = [VID, "lmnopqrstuv"]
        self.store.index(
            CID,
            "upload",
            [{"id": ids[1], "title": "Second", "url": "https://youtube.com/watch?v=" + ids[1]}],
            "turbo-v1",
        )
        barrier = threading.Barrier(2)
        guard = threading.Lock()
        gpu_active = gpu_peak = 0
        original = AgentRunner.run

        def download(root, video_id):
            path = root / "audio" / (video_id + ".webm")
            path.parent.mkdir(exist_ok=True)
            path.write_bytes(b"audio")
            return path, {"channel_id": CID, "duration": 4}

        def transcribe(_):
            nonlocal gpu_active, gpu_peak
            with guard:
                gpu_active += 1
                gpu_peak = max(gpu_peak, gpu_active)
            time.sleep(0.05)
            with guard:
                gpu_active -= 1
            return RESULT

        def agent(runner, payload, tools):
            if "evidence" not in payload["output_schema"]["properties"]:
                # Both real video stage handlers must reach the agent concurrently.
                # This deadlocks if their worker lifetime/DB connection is shared.
                barrier.wait(timeout=60)
            return original(runner, payload, tools)

        with (
            patch.dict(
                os.environ, {"TUBESCRIBE_AGENT_CONCURRENCY": "2", "TUBESCRIBE_AGENT_SLOTS": "2"}
            ),
            patch("tubescribe.worker.download", side_effect=download),
            patch("tubescribe.worker.Client.transcribe", side_effect=transcribe),
            patch.object(AgentRunner, "run", agent),
        ):
            worker = self.replacement("parallel-videos")
            result = work(
                worker,
                "http://127.0.0.1:1",
                ids=ids,
                postprocessing="text",
                bridge=self.fixture.bridge,
            )
        self.assertEqual(result["completed"], 2)
        self.assertEqual(result["failed"], 0)
        self.assertEqual(gpu_peak, 1)
        self.assertTrue(all(Plan().complete(row) for row in self.store.pipeline_jobs(ids=ids)))
        self.assertEqual(self.fixture.requests, 4)

    def test_youtube_cooldown_is_shared_across_worker_scratch_directories(self):
        from tubescribe.execution import defer_downloads

        replica = self.replacement("cooldown-replica")
        defer_downloads(self.store.root, seconds=1)
        started = time.monotonic()
        with capacity(replica.root, "download"):
            elapsed = time.monotonic() - started
        self.assertGreaterEqual(elapsed, 0.7)
        self.assertIsNone(
            self.store.db.execute(
                "SELECT 1 FROM claims WHERE resource='capacity:download:0'"
            ).fetchone()
        )

    def test_notifications_wake_on_committed_work_and_revisions_are_transactional(self):
        from tubescribe.changes import notifications, revisions

        before = revisions(self.store.db)
        with notifications() as wait:
            started = time.monotonic()
            with self.assertRaises(RuntimeError), self.store.db:
                self.store.db.execute("UPDATE videos SET title='Rolled back'")
                raise RuntimeError()
            wait(time.time() + 0.15)
            self.assertGreaterEqual(time.monotonic() - started, 0.1)
            self.assertEqual(revisions(self.store.db), before)
            # Commit BEFORE entering wait: notifications queued after LISTEN
            # must not be missed between checking the queue and sleeping.
            Dispatcher(self.store).submit(JobRequest("work", {"video": [VID]}))
            started = time.monotonic()
            wait(time.time() + 2)
            self.assertLess(time.monotonic() - started, 1)
        self.assertNotEqual(revisions(self.store.db)["feed"], before["feed"])

    def test_idle_consumer_uses_notification_wait_instead_of_polling(self):
        from contextlib import contextmanager

        from tubescribe.submission import consume_network

        class StopWait(Exception):
            pass

        waits = []

        @contextmanager
        def subscription():
            def wait(deadline):
                waits.append(deadline)
                raise StopWait()
            yield wait

        with (patch("tubescribe.changes.notifications", subscription),
              patch("tubescribe.submission.time.sleep", side_effect=AssertionError("polling")),
              patch("tubescribe.rollups.schedule") as schedule,
              self.assertRaises(StopWait)):
            consume_network(self.store, once=False, poll=0.01, process=None)
        schedule.assert_called_once()
        self.assertEqual(waits, [None])

    def test_explicit_postgres_upgrade_preserves_publications_and_queued_work(self):
        from contextlib import closing
        from urllib.parse import quote, urlencode

        from psycopg import sql
        from upgrade_fixture import load_postgres_v4, write_v4

        from tubescribe.database import columns, connect
        from tubescribe.migrations import SCHEMA_VERSION, SchemaCompatibilityError, manage, migrate
        from tubescribe.worker import work

        self.fixture.full()
        build_one(self.store, self.fixture.job["id"], "summary", bridge=self.fixture.bridge)
        queued = Dispatcher(self.store).submit(JobRequest("artifacts", {"workflow": "notes"}))
        calls = self.fixture.requests
        before = {key: self.store.read_key(key) for key in self.store.blobs.keys()}
        old = Path(self.base.name) / (self.schema + "-v4.sqlite3")
        self.addCleanup(old.unlink, missing_ok=True)
        write_v4(self.store.db, old)
        schema = "upgrade_" + uuid.uuid4().hex
        self.admin.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema)))
        self.addCleanup(
            lambda: self.admin.execute(
                sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(schema))
            )
        )
        url = (
            self.database_url
            + ("&" if "?" in self.database_url else "?")
            + urlencode({"options": "-c search_path=" + schema}, quote_via=quote)
        )
        with patch.dict(os.environ, {"TUBESCRIBE_DATABASE_URL": url}):
            root = self.store.root / "upgraded-worker"
            with self.assertRaisesRegex(SchemaCompatibilityError, "requires migration"):
                Store(root)
            with closing(connect(root)) as db:
                load_postgres_v4(db, old)
            with self.assertRaisesRegex(SchemaCompatibilityError, "requires migration"):
                Store(root)

            with closing(connect(root)) as db:

                class FailLedger:
                    def execute(self, statement, *args):
                        if statement == "INSERT INTO schema_migrations VALUES(5)":
                            raise RuntimeError("simulated PostgreSQL migration failure")
                        return db.execute(statement, *args)

                    def __enter__(self):
                        db.__enter__()
                        return self

                    def __exit__(self, *args):
                        return db.__exit__(*args)

                    def __getattr__(self, name):
                        return getattr(db, name)

                with self.assertRaisesRegex(RuntimeError, "simulated PostgreSQL"):
                    migrate(FailLedger())
                self.assertEqual(manage(root, "status")["schema_version"], 4)
                self.assertNotIn("worker_id", columns(db, "submissions"))
            self.assertEqual(manage(root, "apply")["previous_version"], 4)
            self.assertEqual(manage(root, "apply")["previous_version"], SCHEMA_VERSION)
            upgraded = self.replacement("upgraded")
            self.assertEqual(upgraded.submission(queued)["state"], "queued")
            upgraded.recover()
            result = work(upgraded, "http://127.0.0.1:1", bridge=self.fixture.bridge)
            self.assertEqual(result["skipped"], 1)
            self.assertEqual(self.fixture.requests, calls)
            for key, data in before.items():
                self.assertEqual(upgraded.read_key(key), data, key)
            with upgraded.db:
                upgraded.db.execute("INSERT INTO schema_migrations VALUES(?)", (SCHEMA_VERSION + 1,))
            with self.assertRaisesRegex(SchemaCompatibilityError, "newer"):
                Store(root)
            with self.assertRaisesRegex(SchemaCompatibilityError, "newer"):
                manage(root, "apply")
            self.assertEqual(upgraded.db.execute("SELECT COUNT(*) FROM jobs").fetchone()[0], 1)

    def test_pipeline_and_publications_survive_all_application_scratch_deletion(self):
        from tubescribe.worker import work

        result = work(
            self.store,
            "http://127.0.0.1:1",
            ids=[VID],
            bridge=self.fixture.bridge,
            postprocessing="text",
        )
        self.assertEqual(result["completed"], 1)
        self.assertEqual(self.fixture.requests, 2)
        before = snapshot(self.store.root)["jobs"][0]
        self.store.db.close()
        shutil.rmtree(self.store.root)
        replacement = self.replacement("replacement")
        replacement.recover()
        server = make_server(replacement.root)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        base = f"http://127.0.0.1:{server.server_port}"
        for link in [before["processed_url"], before["summary_url"], before["srt_url"]]:
            self.assertEqual(httpx.get(base + link, trust_env=False).status_code, 200)
        again = work(
            replacement,
            "http://127.0.0.1:1",
            ids=[VID],
            bridge=self.fixture.bridge,
            postprocessing="text",
        )
        self.assertEqual(again["skipped"], 1)
        self.assertEqual(self.fixture.requests, 2)
        import json

        status = subprocess.run(
            self.cli(replacement.root, "status"),
            check=True,
            capture_output=True,
            text=True,
            timeout=15,
        )
        self.assertEqual(json.loads(status.stdout)["completed_jobs"], 1)

    def test_real_queue_consumer_and_killed_packager_reuse_model_response(self):
        self.assertEqual(self.fixture.full()["completed"], 1)
        request = JobRequest("artifacts", {"video": [VID], "bridge": self.fixture.bridge})
        identifier = Dispatcher(self.store).submit(request)
        marker = Path(self.base.name) / (self.schema + ".marker")
        wrapper = (
            "import runpy,time; from pathlib import Path; from tubescribe import artifacts as m\n"
            "original=m.atomic_write\n"
            "def pause(path,content):\n"
            " if path.name=='artifact.md':\n"
            f"  Path({str(marker)!r}).write_text('saving'); time.sleep(60)\n"
            " return original(path,content)\n"
            "m.atomic_write=pause\nrunpy.run_module('tubescribe.cli',run_name='__main__')\n"
        )
        scratch = self.replacement("killed").root
        child = subprocess.Popen(
            [
                sys.executable,
                "-c",
                wrapper,
                "--data",
                str(scratch),
                "--submission",
                identifier,
                "artifacts",
                "--video",
                VID,
                "--bridge",
                self.fixture.bridge,
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
        )
        try:
            deadline = time.monotonic() + 15
            while not marker.exists() and time.monotonic() < deadline and child.poll() is None:
                time.sleep(0.02)
            self.assertTrue(
                marker.exists(),
                child.stderr.read().decode()
                if child.poll() is not None
                else "Packaging not reached",
            )
            child.send_signal(signal.SIGKILL)
            child.wait(timeout=5)
        finally:
            if child.poll() is None:
                child.kill()
                child.wait(timeout=5)
            child.stderr.close()
        shutil.rmtree(scratch)
        with self.store.db:
            self.store.db.execute("UPDATE claims SET expires=0")
            self.store.db.execute("UPDATE submissions SET updated=0")
        replacement = self.replacement("queue")
        result = subprocess.run(
            self.cli(replacement.root, "queue", "once"), capture_output=True, timeout=30
        )
        self.assertEqual(result.returncode, 0, result.stderr.decode() + result.stdout.decode())
        self.assertEqual(self.store.submission(identifier)["state"], "complete")
        self.assertEqual(self.fixture.requests, 2)
        self.assertEqual(
            self.store.stage_row("artifact", self.fixture.job["id"], workflow="summary")["state"],
            "complete",
        )

    def test_consumers_reserve_distinct_requests_and_expired_ownership_recovers(self):
        dispatcher = Dispatcher(self.store)
        first = dispatcher.submit(JobRequest("artifacts", {"video": [VID]}))
        second = dispatcher.submit(JobRequest("artifacts", {"video": [VID], "workflow": "notes"}))
        other = self.replacement("consumer")
        row = self.store.claim_submission("worker-a")
        next_row = other.claim_submission("worker-b")
        self.assertEqual({row["id"], next_row["id"]}, {first, second})
        self.assertIsNone(other.claim_submission("worker-c"))
        with self.store.db:
            self.store.db.execute("UPDATE submissions SET updated=0")
        self.assertEqual(other.reclaim_submissions(), 2)
        self.assertIsNotNone(other.claim_submission("replacement"))

    def test_network_backup_restores_into_new_database_and_bucket(self):
        from urllib.parse import quote, urlencode

        import boto3
        from psycopg import sql

        from tubescribe.backup import export_archive, restore_archive
        from tubescribe.operations import metrics, readiness
        from tubescribe.postprocessing_worker import process_one

        self.fixture.full()
        video = self.store.root / "backup-fixture.mp4"
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
        self.fixture.inspect_video = True
        self.store.stage_update(
            "postprocessing",
            self.fixture.job["id"],
            state="pending",
            mode="video",
            output_dir=None,
            manifest_path=None,
            attempts=0,
        )
        with patch("tubescribe.postprocessing_worker.cached_video", return_value=str(video)):
            process_one(self.store, self.fixture.job, bridge=self.fixture.bridge, mode="video")
        self.assertEqual(self.fixture.row()["figures"], 1)
        build_one(self.store, self.fixture.job["id"], "summary", bridge=self.fixture.bridge)
        queued = Dispatcher(self.store).submit(JobRequest("artifacts", {"workflow": "notes"}))
        backup = Path(self.base.name) / (self.schema + "-backup")
        self.addCleanup(lambda: shutil.rmtree(backup, ignore_errors=True))
        export_archive(self.store, backup)
        restored_schema = "restore_" + uuid.uuid4().hex
        bucket = "restore-" + uuid.uuid4().hex
        self.admin.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(restored_schema)))
        self.addCleanup(
            lambda: self.admin.execute(
                sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(restored_schema))
            )
        )
        boto3.client("s3", endpoint_url=self.endpoint).create_bucket(Bucket=bucket)
        url = (
            self.database_url
            + ("&" if "?" in self.database_url else "?")
            + urlencode({"options": "-c search_path=" + restored_schema}, quote_via=quote)
        )
        with patch.dict(
            os.environ, {"TUBESCRIBE_DATABASE_URL": url, "TUBESCRIBE_S3_BUCKET": bucket}
        ):
            from tubescribe.migrations import manage

            manage(Path(self.base.name) / "restored", "apply")
            restored = self.replacement("restored")
            restore_archive(restored, backup)
            restored.recover()
            self.assertEqual(restored.status_data()["completed_jobs"], 1)
            self.assertEqual(restored.submission(queued)["state"], "queued")
            self.assertTrue(readiness(restored.root)["ready"])
            self.assertGreater(sum(r["tokens"] for r in metrics(restored.root)["usage"]), 0)
            processed = restored.stage_row("postprocessing", self.fixture.job["id"])
            self.assertEqual(processed["figures"], 1)
            self.assertTrue(restored.exists(processed["output_dir"] + "/figures/frame_0001000.png"))
            for key in restored.blobs.keys():
                self.assertEqual(restored.read_key(key), (backup / key).read_bytes())
            # Restored PostgreSQL identity sequences must accept new records.
            restored.index(
                self.fixture.job["channel_id"],
                "videos",
                [
                    {
                        "id": "newvideo123",
                        "title": "New",
                        "url": "https://youtube.com/watch?v=newvideo123",
                    }
                ],
                "turbo-v1",
            )
            self.assertEqual(len(restored.pipeline_jobs()), 2)

    def test_s3_http_outage_preserves_completed_records_and_resumes(self):
        from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

        import boto3
        from botocore.config import Config

        from tubescribe.artifacts import complete_valid
        from tubescribe.blobs import StorageUnavailable
        from tubescribe.s3 import S3Artifacts

        self.fixture.full()
        build_one(self.store, self.fixture.job["id"], "summary", bridge=self.fixture.bridge)

        class Offline(BaseHTTPRequestHandler):
            status = 503

            def log_message(self, *_):
                pass

            def do_GET(self):
                self.send_response(self.status)
                self.end_headers()

            do_HEAD = do_GET

        server = ThreadingHTTPServer(("127.0.0.1", 0), Offline)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        cold = self.replacement("outage")
        normal = cold.blobs
        cold.blobs = S3Artifacts(
            self.bucket,
            client=boto3.client(
                "s3",
                endpoint_url=f"http://127.0.0.1:{server.server_port}",
                config=Config(retries={"max_attempts": 0}),
            ),
        )
        before = cold.status_data()
        for status in (503, 404):
            Offline.status = status
            with self.assertRaises(StorageUnavailable):
                cold.recover()
            with self.assertRaises(StorageUnavailable):
                complete_valid(
                    cold, cold.stage_row("artifact", self.fixture.job["id"], workflow="summary")
                )
            self.assertEqual(before, cold.status_data())
        cold.blobs = normal
        cold.recover()
        self.assertEqual(cold.status_data()["completed_jobs"], 1)
        self.assertEqual(self.fixture.requests, 2)

    def test_recovery_skips_a_live_job_in_another_process(self):
        self.fixture.full()
        job_id = self.fixture.job["id"]
        row = self.store.stage_row("postprocessing", job_id)
        self.store.blobs.put(row["manifest_path"], b"corrupt manifest")
        replacement = self.replacement("concurrent-repair")
        code = (
            "import sys; from pathlib import Path; from tubescribe.storage import Store; "
            "s=Store(Path(sys.argv[1])); s.recover(); "
            "assert s.stage_row('postprocessing',int(sys.argv[2]))['state']=='complete'; s.db.close()"
        )
        with (
            pipeline_lock(self.store.root),
            lease(self.store.root, f"video:{VID}"),
            lease(self.store.root, f"job:{job_id}"),
        ):
            subprocess.run(
                [sys.executable, "-c", code, str(replacement.root), str(job_id)],
                check=True,
                capture_output=True,
                timeout=20,
            )
            # Host clock skew must not expire another network worker's ownership.
            with patch("time.time", return_value=time.time() + 3600):
                self.assertTrue(replacement.owned(f"job:{job_id}"))
        replacement.recover()
        self.assertEqual(replacement.stage_row("postprocessing", job_id)["state"], "pending")

    def test_gateway_owns_screenshots_and_summary_across_separate_scratch(self):
        from tubescribe.agent_gateway import make_gateway
        from tubescribe.postprocessing_worker import process_one

        self.fixture.full()
        token = "rehearsal-access-token-" + "a" * 32
        with patch.dict(
            os.environ, {"TUBESCRIBE_ACCESS_TOKEN": token, "TUBESCRIBE_AGENT_PROTOCOL": "durable"}
        ):
            gateway_store = self.replacement("gateway")
            server = make_gateway(gateway_store.root, self.fixture.bridge)
            threading.Thread(target=server.serve_forever, daemon=True).start()
            self.addCleanup(server.server_close)
            self.addCleanup(server.shutdown)
            worker = self.replacement("gateway-worker")
            video = worker.root / "videos" / (VID + ".mp4")
            video.parent.mkdir()
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
            self.fixture.inspect_video = True
            worker.stage_update(
                "postprocessing",
                self.fixture.job["id"],
                state="pending",
                mode="video",
                output_dir=None,
                manifest_path=None,
                attempts=0,
            )
            endpoint = f"http://127.0.0.1:{server.server_port}"
            with patch("tubescribe.postprocessing_worker.cached_video", return_value=str(video)):
                self.assertTrue(
                    process_one(worker, self.fixture.job, bridge=endpoint, mode="video")
                )
            row = worker.stage_row("postprocessing", self.fixture.job["id"])
            figure = row["output_dir"] + "/figures/frame_0001000.png"
            self.assertTrue(worker.blobs.get(figure).startswith(b"\x89PNG"))
            self.assertFalse(worker.exists("videos/" + video.name))
            shutil.rmtree(worker.root)
            with patch.dict(os.environ, {"TUBESCRIBE_WORKER_ID": "gateway-replica"}):
                replica = make_gateway(
                    self.replacement("gateway-replica").root, self.fixture.bridge
                )
            threading.Thread(target=replica.serve_forever, daemon=True).start()
            self.addCleanup(replica.server_close)
            self.addCleanup(replica.shutdown)
            endpoint = f"http://127.0.0.1:{replica.server_port}"
            cold = self.replacement("cold-summary")
            self.assertTrue(build_one(cold, self.fixture.job["id"], "summary", bridge=endpoint))
            self.assertEqual(self.fixture.requests, 3)
            self.assertEqual(
                cold.stage_row("artifact", self.fixture.job["id"], workflow="summary")["state"],
                "complete",
            )

    def test_stage_capacity_is_independent_and_same_video_is_fenced(self):
        with capacity(self.store.root, "gpu"):
            result = subprocess.run(
                [
                    sys.executable,
                    "-c",
                    "from pathlib import Path; from tubescribe.execution import capacity; import os;\nwith capacity(Path(os.environ['SCRATCH']), 'agent'): print('independent')",
                ],
                env={**os.environ, "SCRATCH": str(self.store.root)},
                capture_output=True,
                timeout=10,
            )
            self.assertEqual(result.returncode, 0, result.stderr.decode())
            self.assertIn(b"independent", result.stdout)
        with lease(self.store.root, "video:" + VID):
            result = subprocess.run(
                [
                    sys.executable,
                    "-c",
                    "from pathlib import Path; from tubescribe.execution import lease,ClaimBusy; import os;\ntry:\n with lease(Path(os.environ['SCRATCH']), 'video:' + os.environ['VIDEO']): raise AssertionError('duplicate')\nexcept ClaimBusy: print('fenced')",
                ],
                env={**os.environ, "SCRATCH": str(self.store.root), "VIDEO": VID},
                capture_output=True,
                timeout=10,
            )
            self.assertIn(b"fenced", result.stdout, result.stderr.decode())
        with pipeline_lock(self.store.root):
            with self.assertRaises(ClaimBusy):
                with lease(self.store.root, "archive"):
                    pass

    def test_s3_streaming_checksums_missing_keys_and_portable_database_export(self):
        from tubescribe.database import export_sqlite

        content = b"streaming media\n" * 700000
        path = self.store.root / "input"
        path.write_bytes(content)
        ref = self.store.blobs.put_file("videos/test.mp4", path)
        from boto3.exceptions import S3UploadFailedError

        with (
            patch.object(
                self.store.blobs.client, "upload_file", side_effect=S3UploadFailedError("offline")
            ),
            self.assertRaises(OSError),
        ):
            self.store.blobs.put_file("videos/failed.mp4", path)
        target = self.store.root / "streamed"
        self.store.blobs.copy_to(ref.key, target, ref.sha256)
        self.assertEqual(target.read_bytes(), content)
        with self.assertRaises(ValueError):
            self.store.blobs.copy_to(ref.key, target, "0" * 64)
        self.assertEqual(target.read_bytes(), content)
        with self.assertRaises(FileNotFoundError):
            self.store.blobs.get("missing")
        self.assertEqual(self.store.blobs.keys("videos/"), [ref.key])
        self.store.blobs.delete(ref.key)
        self.assertFalse(self.store.exists(ref.key))
        self.fixture.full()
        destination = self.store.root / "export.sqlite3"
        export_sqlite(self.store.root, destination)
        import sqlite3
        from contextlib import closing

        with closing(sqlite3.connect(destination)) as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM transcripts").fetchone()[0], 1)
