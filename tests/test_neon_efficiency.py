"""Guard Neon transfer at the SQL boundary, rather than only the HTTP response size."""

import sqlite3
import tempfile
import threading
import time
import unittest
from contextlib import closing
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import patch

import httpx

from tubescribe.agent_gateway import Runs, make_gateway
from tubescribe.changes import queue_deadline, revisions
from tubescribe.dashboard import FeedChanges
from tubescribe.rollups import catalogue, index, schedule
from tubescribe.storage import Store


class NeonEfficiency(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.store = Store(self.root)
        self.addCleanup(self.store.db.close)
        self.store.channel({"id": "channel", "url": "https://youtube.com/test", "title": "Test"})
        self.now = datetime.now(UTC)
        self.store.index(
            "channel",
            "upload",
            [
                {
                    "id": "video",
                    "title": "Video",
                    "url": "https://youtube.com/watch?v=video",
                    "published_at": str((self.now - timedelta(days=1)).timestamp()),
                }
            ],
            "test",
        )

    def test_cold_and_warm_unchanged_feed_never_select_video_rows(self):
        feed = FeedChanges(self.root)
        first = feed.get()
        for reader in (feed, FeedChanges(self.root)):
            with patch(
                "tubescribe.dashboard.reader_snapshot", side_effect=AssertionError("full scan")
            ):
                delta = reader.get(first["revision"])
            self.assertFalse(delta["full"])
            self.assertEqual(delta["jobs"], [])
            self.assertEqual(delta["summary"], first["summary"])

    def test_revision_tracks_sql_writes_deletions_and_transaction_rollback(self):
        old = revisions(self.store.db)
        with self.assertRaises(RuntimeError), self.store.db:
            self.store.db.execute("UPDATE videos SET title='Rolled back'")
            self.assertNotEqual(revisions(self.store.db), old)
            raise RuntimeError()
        self.assertEqual(revisions(self.store.db), old)
        with self.store.db:
            self.store.db.execute("UPDATE videos SET title='Changed'")
        self.assertNotEqual(revisions(self.store.db), old)
        feed = FeedChanges(self.root)
        first = feed.get()
        with self.store.db:
            self.store.db.execute("DELETE FROM jobs")
        self.assertNotEqual(feed.get(first["revision"])["revision"], first["revision"])

    def test_summary_cache_reuses_unchanged_inputs_and_expires_without_writes(self):
        first = index(self.store, clock=self.now)
        with patch("tubescribe.rollups.catalogue", side_effect=AssertionError("full scan")):
            unchanged = index(self.store, clock=self.now + timedelta(hours=1))
        self.assertEqual(first["items"], unchanged["items"])
        with patch("tubescribe.rollups.catalogue", wraps=catalogue) as read:
            expired = index(self.store, clock=self.now + timedelta(days=7))
            read.assert_called_once()
        self.assertEqual(expired["items"][0]["coverage"]["videos"], 0)
        with self.store.db:
            self.store.db.execute("UPDATE videos SET title='Revised'")
        with patch("tubescribe.rollups.catalogue", wraps=catalogue) as read:
            index(self.store, clock=self.now + timedelta(days=7))
            read.assert_called_once()

    def test_idle_scheduler_skips_catalogue_and_object_storage_checks(self):
        schedule(self.store)
        with (
            patch("tubescribe.rollups.catalogue", side_effect=AssertionError("full scan")),
            patch.object(self.store, "bundle_valid", side_effect=AssertionError("R2 scan")),
        ):
            self.assertIsNone(schedule(self.store))
        self.assertGreater(self.store._rollup_wake_at, time.time())

    def test_gateway_status_and_recovery_never_read_prompt_column(self):
        runs = Runs(self.root)
        self.addCleanup(runs.close)
        identifier = "a" * 64
        runs.create(identifier, {"input": "large transcript " * 10000})
        runs.update(identifier, state="active")

        def protect(action, table, column, *_):
            return (
                sqlite3.SQLITE_DENY
                if action == sqlite3.SQLITE_READ and table == "agent_runs" and column == "request"
                else sqlite3.SQLITE_OK
            )

        runs.db.set_authorizer(protect)
        self.assertEqual(runs.status(identifier)["state"], "active")
        runs.recover(identifier)
        self.assertEqual(runs.status(identifier)["state"], "uncertain")

    def test_gateway_waits_for_change_without_repeated_database_reads(self):
        token = "test-" + "a" * 40
        with patch.dict("os.environ", {"TUBESCRIBE_ACCESS_TOKEN": token}):
            server = make_gateway(self.root, "http://127.0.0.1:1")
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        identifier = "b" * 64
        with closing(Runs(self.root)) as runs:
            runs.create(identifier, {"input": "private transcript"})
        calls = []
        original = Runs.status

        def status(runs, key):
            calls.append(key)
            return original(runs, key)

        responses = []
        url = f"http://127.0.0.1:{server.server_port}/api/v1/durable/runs/{identifier}"
        with patch.object(Runs, "status", status):
            thread = threading.Thread(
                target=lambda: responses.append(
                    httpx.get(
                        url,
                        headers={"Authorization": "Bearer " + token, "Prefer": "wait=25"},
                        timeout=5,
                        trust_env=False,
                    )
                )
            )
            thread.start()
            deadline = time.monotonic() + 2
            while not calls and time.monotonic() < deadline:
                time.sleep(0.01)
            before = len(calls)
            time.sleep(0.2)
            self.assertTrue(thread.is_alive())
            self.assertEqual(len(calls), before)
            # Wake through the actual gateway update path (DELETE).
            httpx.delete(url, headers={"Authorization": "Bearer " + token}, trust_env=False)
            thread.join(timeout=3)
        self.assertFalse(thread.is_alive())
        self.assertEqual(responses[0].json()["state"], "cancelled")

    def test_abandoned_reservation_has_a_timer_even_without_a_lease(self):
        from tubescribe.submission import Dispatcher, JobRequest

        identifier = Dispatcher(self.store).submit(JobRequest("work", {"video": ["video"]}))
        self.store.update_submission(identifier, state="active", pid=999)
        self.assertAlmostEqual(queue_deadline(self.store), time.time() + 90, delta=2)
