"""Incremental discovery, scope validation and unchanged retry budgets."""

import json
import tempfile
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import patch

from tubescribe.feed_watch import fetch_feed, poll_feeds
from tubescribe.storage import Store
from tubescribe.youtube import YouTubeError

CID = "UCabcdefghijklmnopqrstuv"
VID = "abcdefghijk"
SECOND = "lmnopqrstuv"
NOW = datetime(2026, 10, 16, 8, tzinfo=UTC)


def entry(identifier=VID, stamp=None):
    return {
        "id": identifier,
        "title": identifier,
        "url": "https://www.youtube.com/watch?v=" + identifier,
        "published_at": str((stamp or NOW).timestamp()),
    }


class Feeds(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = Store(Path(self.temp.name))
        self.addCleanup(self.store.db.close)
        self.store.channel(
            {"id": CID, "title": "Channel", "url": "https://www.youtube.com/channel/" + CID}
        )

    def poll(self):
        return poll_feeds(self.store, "http://127.0.0.1:18766", profile="test", clock=NOW)

    def details(self, identifier):
        return {"id": identifier, "channel_id": CID, "timestamp": NOW.timestamp(), "duration": 30}

    def test_bounded_publication_feed_and_owner_validation(self):
        data = {
            "id": "UU" + CID[2:],
            "channel_id": CID,
            "entries": [{"id": VID, "timestamp": NOW.timestamp()}],
        }
        with patch("tubescribe.feed_watch.youtube.run", return_value=data) as run:
            self.assertEqual(fetch_feed(CID)[0]["id"], VID)
            self.assertIn("--playlist-end", run.call_args.args[0])
            self.assertIn("15", run.call_args.args[0])
            data["channel_id"] = "UCwrong"
            with self.assertRaisesRegex(ValueError, "identity"):
                fetch_feed(CID)

    def test_new_video_full_pipeline_and_repoll_avoids_metadata_and_duplicate(self):
        with (
            patch("tubescribe.feed_watch.fetch_feed", return_value=[entry()]),
            patch("tubescribe.feed_watch.youtube.details", side_effect=self.details) as details,
        ):
            first = self.poll()
            second = self.poll()
        self.assertEqual(first["videos"], [VID])
        self.assertEqual(second["videos"], [])
        self.assertEqual(details.call_count, 1)
        request = json.loads(self.store.queued_submissions()[0]["request"])
        self.assertEqual(request["command"], "work")
        self.assertEqual(request["options"]["video"], [VID])
        self.assertTrue(request["options"]["wait"])
        self.assertNotIn("no_summary", request["options"])
        self.assertNotIn("postprocessing", request["options"])

    def test_completed_and_exhausted_are_untouched(self):
        self.store.index(CID, "upload", [entry(), entry(SECOND)], "test")
        rows = self.store.pipeline_jobs(profile="test")
        first = next(r for r in rows if r["video_id"] == VID)
        second = next(r for r in rows if r["video_id"] == SECOND)
        self.store.update(first["id"], state="complete", asr_state="complete")
        for stage in ("diarization", "postprocessing", "artifact"):
            opts = {"workflow": "summary"} if stage == "artifact" else {}
            self.store.ensure_stage(stage, first["id"], **opts)
            self.store.stage_update(stage, first["id"], state="complete", **opts)
        self.store.update(
            second["id"],
            state="failed",
            asr_state="failed",
            asr_attempts=3,
            error="No detected speech",
        )
        with (
            patch("tubescribe.feed_watch.fetch_feed", return_value=[entry(), entry(SECOND)]),
            patch("tubescribe.feed_watch.youtube.details") as details,
        ):
            result = self.poll()
        details.assert_not_called()
        self.assertEqual(result["videos"], [])
        self.assertEqual(
            self.store.pipeline_jobs(profile="test", ids=[SECOND])[0]["asr_attempts"], 3
        )

    def test_bad_entry_does_not_block_other_videos(self):
        def resolve(identifier):
            if identifier == VID:
                raise YouTubeError("Video unavailable")
            return self.details(identifier)

        with (
            patch("tubescribe.feed_watch.fetch_feed", return_value=[entry(), entry(SECOND)]),
            patch("tubescribe.feed_watch.youtube.details", side_effect=resolve),
        ):
            result = self.poll()
        self.assertEqual(result["videos"], [SECOND])
        self.assertEqual(len(result["errors"]), 1)

    def test_live_future_and_old_are_not_queued(self):
        ids = [VID, SECOND, "01234567890"]

        def resolve(identifier):
            info = self.details(identifier)
            if identifier == VID:
                info["live_status"] = "is_live"
            elif identifier == SECOND:
                info["timestamp"] = (NOW + timedelta(hours=1)).timestamp()
            else:
                info["timestamp"] = (NOW - timedelta(days=8)).timestamp()
            return info

        with (
            patch("tubescribe.feed_watch.fetch_feed", return_value=[entry(i) for i in ids]),
            patch("tubescribe.feed_watch.youtube.details", side_effect=resolve),
        ):
            result = self.poll()
        self.assertFalse(result["errors"])
        self.assertFalse(result["videos"])
        self.assertFalse(self.store.queued_submissions())

    def test_rate_limit_stops_poll_and_defers_downloads(self):
        with (
            patch(
                "tubescribe.feed_watch.fetch_feed",
                side_effect=YouTubeError("429 too many requests"),
            ),
            patch("tubescribe.feed_watch.defer_downloads") as defer,
        ):
            result = self.poll()
        defer.assert_called_once()
        self.assertEqual(len(result["errors"]), 1)
        self.assertFalse(result["videos"])

    def test_full_unanchored_recent_feed_reconciles_without_truncation(self):
        feed = [entry(f"{i:011d}") for i in range(15)]
        with (
            patch("tubescribe.feed_watch.fetch_feed", return_value=feed),
            patch(
                "tubescribe.feed_watch.ensure_recent",
                return_value={"discovery_errors": [], "rate_limited": False},
            ) as reconcile,
        ):
            result = self.poll()
        self.assertEqual(result["reconciled"], [CID])
        self.assertEqual(reconcile.call_args.kwargs["channel"], CID)
        self.assertTrue(reconcile.call_args.kwargs["enqueue_only"])

    def test_missing_watch_date_is_an_error_not_a_fabricated_publication(self):
        with (
            patch("tubescribe.feed_watch.fetch_feed", return_value=[entry()]),
            patch(
                "tubescribe.feed_watch.youtube.details", return_value={"id": VID, "channel_id": CID}
            ),
        ):
            result = self.poll()
        self.assertFalse(result["videos"])
        self.assertEqual(len(result["errors"]), 1)

    def test_unknown_feed_dates_use_watch_page_to_detect_a_truncated_window(self):
        feed = [{**entry(f"{i:011d}"), "published_at": None} for i in range(15)]
        with (
            patch("tubescribe.feed_watch.fetch_feed", return_value=feed),
            patch("tubescribe.feed_watch.youtube.details", side_effect=self.details) as details,
            patch(
                "tubescribe.feed_watch.ensure_recent",
                return_value={"discovery_errors": [], "rate_limited": False},
            ) as reconcile,
        ):
            result = self.poll()
        self.assertEqual(result["reconciled"], [CID])
        details.assert_called_once_with(feed[-1]["id"])
        reconcile.assert_called_once()

    def test_clearly_old_feed_entries_need_no_watch_page_requests(self):
        feed = [entry(f"{i:011d}", NOW - timedelta(days=9)) for i in range(15)]
        with (
            patch("tubescribe.feed_watch.fetch_feed", return_value=feed),
            patch("tubescribe.feed_watch.youtube.details") as details,
            patch("tubescribe.feed_watch.ensure_recent") as reconcile,
        ):
            result = self.poll()
        details.assert_not_called()
        reconcile.assert_not_called()
        self.assertFalse(result["videos"])

    def test_interrupted_catchup_remains_required_after_partial_indexing(self):
        feed = [entry(f"{i:011d}") for i in range(15)]

        def interrupted(*args, **kwargs):
            self.store.index(CID, "upload", [feed[0]], "test")
            raise RuntimeError("Discovery interrupted")

        with (
            patch("tubescribe.feed_watch.fetch_feed", return_value=feed),
            patch("tubescribe.feed_watch.ensure_recent", side_effect=interrupted),
        ):
            self.assertEqual(len(self.poll()["errors"]), 1)
        with (
            patch("tubescribe.feed_watch.fetch_feed") as fetch,
            patch(
                "tubescribe.feed_watch.ensure_recent",
                return_value={"discovery_errors": [], "rate_limited": False},
            ) as reconcile,
        ):
            self.assertEqual(self.poll()["reconciled"], [CID])
        fetch.assert_not_called()
        reconcile.assert_called_once()
