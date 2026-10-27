"""Scheduler bounds, draining and scope without paid model requests."""

import os
import threading
import time
import unittest
from contextlib import nullcontext
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from tubescribe.execution import CURRENT_SUBMISSION
from tubescribe.parallel import work_parallel


class Parallel(unittest.TestCase):
    def test_rate_limit_cools_all_downloaders_but_direct_403_does_not(self):
        from tubescribe.youtube import YouTubeError, download, download_video

        for operation in (download, download_video):
            for message, expected in [
                ("HTTP Error 429: Too Many Requests", 1),
                ("HTTP Error 403: Forbidden", 0),
            ]:
                with (
                    patch("tubescribe.youtube.run", side_effect=YouTubeError(message)),
                    patch("tubescribe.execution.defer_downloads") as cooldown,
                ):
                    with self.assertRaises(YouTubeError):
                        operation(Path("/scratch"), "abcdefghijk")
                    self.assertEqual(cooldown.call_count, expected)

    def test_queued_downloader_rechecks_shared_cooldown_after_acquiring_slot(self):
        from contextlib import contextmanager

        from tubescribe.execution import capacity

        events = []

        @contextmanager
        def slot(*_):
            events.append("acquire")
            try:
                yield
            finally:
                events.append("release")

        claims = Mock()
        claims.download_delay.side_effect = [0, 10, 10, 0, 0]
        config = SimpleNamespace(network=True, downloads=1, gpu_slots=1, agent_slots=8)
        with (
            patch("tubescribe.config.RuntimeConfig.load", return_value=config),
            patch("tubescribe.execution.Claims", return_value=claims),
            patch("tubescribe.execution.lease", side_effect=slot),
            patch("tubescribe.execution.time.sleep") as sleep,
        ):
            with capacity(Path("/scratch"), "download"):
                events.append("download")
        self.assertEqual(events, ["acquire", "release", "acquire", "download", "release"])
        sleep.assert_any_call(10)
        claims.close.assert_called_once()

    def test_readonly_sessions_use_direct_database_without_affecting_pooled_writers(self):
        from tubescribe.database import connect

        with (
            patch.dict(
                os.environ,
                {
                    "TUBESCRIBE_DATABASE_URL": "postgresql://pool/db",
                    "TUBESCRIBE_DATABASE_UNPOOLED_URL": "postgresql://direct/db",
                },
            ),
            patch("tubescribe.database.PostgreSQLConnection") as connection,
        ):
            connect(Path("/scratch"), readonly=True)
            connection.assert_called_with("postgresql://direct/db", readonly=True, root=Path("/scratch"))
            connect(Path("/scratch"))
            connection.assert_called_with("postgresql://pool/db", readonly=False, root=Path("/scratch"))

    def run_jobs(self, *, limit=None, pause=False):
        videos = [f"video-{i}" for i in range(6)]
        finished = set()
        lock = threading.Lock()
        barrier = threading.Barrier(3)
        active = peak = 0
        seen = []
        store = Mock(root=Path("/scratch"), config=SimpleNamespace(agent_concurrency=3))
        store.completed.return_value = 2
        store.jobs.side_effect = lambda **_: [
            {"video_id": video} for video in videos if video not in finished
        ]
        store.next_due.return_value = None

        def one(root, endpoint, options):
            nonlocal active, peak
            self.assertEqual(CURRENT_SUBMISSION.get(), "submission")
            self.assertFalse(options["wait"])
            self.assertEqual(options["limit"], 1)
            video = options["ids"][0]
            with lock:
                active += 1
                peak = max(peak, active)
                seen.append(video)
            if len(seen) <= 3:
                barrier.wait(timeout=3)
            time.sleep(0.02)
            with lock:
                active -= 1
                finished.add(video)
            return {"completed": 1, "failed": 0, "paused": "service unavailable" if pause else None}

        token = CURRENT_SUBMISSION.set("submission")
        try:
            with (
                patch("tubescribe.parallel.pipeline_lock", side_effect=lambda _: nullcontext()),
                patch("tubescribe.parallel.work_one", side_effect=one),
            ):
                result = work_parallel(
                    store,
                    "http://127.0.0.1:1",
                    ids=videos,
                    channel=None,
                    profile="test",
                    limit=limit,
                    max_attempts=3,
                    wait=False,
                    postprocessing="video",
                    summary=True,
                )
        finally:
            CURRENT_SUBMISSION.reset(token)
        self.assertEqual(active, 0)
        self.assertEqual(peak, 3)
        self.assertEqual(len(seen), len(set(seen)))
        store.recover.assert_called_once()
        return result, seen

    def test_overlaps_independent_videos_and_drains_before_return(self):
        result, seen = self.run_jobs()
        self.assertEqual(len(seen), 6)
        self.assertEqual(result, {"completed": 6, "failed": 0, "skipped": 2})

    def test_limit_is_shared_across_parallel_executions(self):
        result, seen = self.run_jobs(limit=3)
        self.assertEqual(len(seen), 3)
        self.assertEqual(result["completed"], 3)

    def test_pause_stops_scheduling_and_drains_existing_work(self):
        result, seen = self.run_jobs(pause=True)
        self.assertEqual(len(seen), 3)
        self.assertEqual(result["paused"], "service unavailable")
