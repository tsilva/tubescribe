"""Rolling-window scope, durable dispatch, and honest failure reporting."""

import contextlib
import io
import json
import tempfile
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import patch

from tubescribe.cli import main
from tubescribe.recent import ensure_recent
from tubescribe.storage import Store
from tubescribe.youtube import YouTubeError

CID = "UCabcdefghijklmnopqrstuv"
OTHER = "UCzyxwvutsrqponmlkjihgfe"
VID = "abcdefghijk"
SECOND = "lmnopqrstuv"
OLD = "01234567890"
NOW = datetime(2026, 10, 15, 12, tzinfo=UTC)


def video(identifier, date=None):
    return {
        "id": identifier,
        "title": identifier,
        "url": "https://www.youtube.com/watch?v=" + identifier,
        "published_at": date,
    }


class Recent(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.store = Store(self.root)
        self.addCleanup(self.store.db.close)
        self.store.channel({"id": CID, "title": "Channel", "url": "https://youtube.com/" + CID})

    def run_recent(self, **options):
        return ensure_recent(self.store, "http://127.0.0.1:18766", clock=NOW, **options)

    def details(self, identifier):
        return {"channel_id": CID, "timestamp": NOW.timestamp(), "duration": 60}

    def test_all_tabs_all_channels_without_count_cap_and_duplicate_dispatch(self):
        self.store.channel({"id": OTHER, "title": "Other", "url": "https://youtube.com/" + OTHER})
        with (
            patch(
                "tubescribe.recent.youtube.discover",
                side_effect=lambda channel, kind: [video(VID if channel == CID else SECOND)],
            ) as discover,
            patch(
                "tubescribe.recent.youtube.details",
                side_effect=lambda identifier: {
                    **self.details(identifier),
                    "channel_id": CID if identifier == VID else OTHER,
                },
            ) as details,
        ):
            first = self.run_recent(enqueue_only=True)
            second = self.run_recent(enqueue_only=True)
        self.assertEqual(first["videos"], [VID, SECOND])
        self.assertEqual(first["submission"], second["submission"])
        self.assertEqual(discover.call_count, 12)
        self.assertEqual(details.call_count, 4)
        request = json.loads(self.store.queued_submissions()[0]["request"])
        self.assertEqual(request["command"], "work")
        self.assertEqual(request["options"]["video"], [VID, SECOND])
        self.assertTrue(request["options"]["wait"])
        self.assertNotIn("no_summary", request["options"])
        self.assertNotIn("postprocessing", request["options"])

    def test_boundary_future_live_missing_date_and_channel_identity(self):
        ids = [VID, SECOND, OLD, "11111111111", "22222222222", "33333333333"]
        metadata = {
            VID: {"timestamp": (NOW - timedelta(days=7)).timestamp()},
            SECOND: {"timestamp": (NOW - timedelta(days=7, seconds=1)).timestamp()},
            OLD: {"timestamp": (NOW + timedelta(seconds=1)).timestamp()},
            ids[3]: {"timestamp": NOW.timestamp(), "live_status": "post_live"},
            ids[4]: {},
            ids[5]: {"timestamp": NOW.timestamp(), "channel_id": OTHER},
        }
        with (
            patch(
                "tubescribe.recent.youtube.discover",
                side_effect=lambda channel, kind: (
                    [video(identifier) for identifier in ids] if kind == "upload" else []
                ),
            ),
            patch(
                "tubescribe.recent.youtube.details",
                side_effect=lambda identifier: {"channel_id": CID, **metadata[identifier]},
            ),
        ):
            result = self.run_recent(enqueue_only=True)
        self.assertEqual(result["videos"], [VID])
        self.assertEqual(len(result["discovery_errors"]), 2)
        self.assertEqual(self.store.db.execute("SELECT count(*) FROM jobs").fetchone()[0], 1)

    def test_old_pinned_entry_does_not_truncate_tab_and_upload_date_fallback(self):
        with (
            patch(
                "tubescribe.recent.youtube.discover",
                side_effect=lambda channel, kind: (
                    [video(OLD, "20200101"), video(VID)] if kind == "upload" else []
                ),
            ),
            patch(
                "tubescribe.recent.youtube.details",
                return_value={
                    "channel_id": CID,
                    "upload_date": "20261008",
                },
            ) as details,
        ):
            result = self.run_recent(enqueue_only=True)
        self.assertEqual(result["videos"], [VID])
        details.assert_called_once_with(VID)
        self.assertEqual(result["date_only_videos"], [VID])

    def test_empty_window_never_submits_unscoped_work(self):
        with patch("tubescribe.recent.youtube.discover", return_value=[]):
            result = self.run_recent(enqueue_only=True)
        self.assertEqual(result["videos"], [])
        self.assertEqual(self.store.queued_submissions(), [])

    def test_tab_failure_visible_but_other_tabs_still_submitted(self):
        def discover(channel, kind):
            if kind == "short":
                raise RuntimeError("Discovery failed")
            return [video(VID)]

        with (
            patch("tubescribe.recent.youtube.discover", side_effect=discover),
            patch("tubescribe.recent.youtube.details", side_effect=self.details),
        ):
            result = self.run_recent(enqueue_only=True)
        self.assertEqual(result["videos"], [VID])
        self.assertEqual(result["discovery_errors"][0]["kind"], "short")
        self.assertEqual(
            self.store.db.execute("SELECT state FROM discovery WHERE kind='short'").fetchone()[0],
            "failed",
        )

    def test_rate_limit_stops_discovery_and_defers_all_downloads(self):
        with (
            patch(
                "tubescribe.recent.youtube.discover", return_value=[video(VID), video(SECOND)]
            ) as discover,
            patch(
                "tubescribe.recent.youtube.details",
                side_effect=[
                    self.details(VID),
                    YouTubeError("429 Too many requests"),
                ],
            ),
            patch("tubescribe.recent.work") as work,
        ):
            result = self.run_recent()
        self.assertTrue(result["rate_limited"])
        self.assertEqual(result["videos"], [VID])
        self.assertEqual(discover.call_count, 1)
        self.assertEqual(self.store.queued_submissions(), [])
        work.assert_not_called()

    def test_exhausted_job_reported_and_attempt_budgets_preserved(self):
        self.store.index(CID, "upload", [video(VID)], "turbo-v1")
        with self.store.db:
            self.store.db.execute("UPDATE jobs SET state='failed',asr_attempts=3")
        with (
            patch("tubescribe.recent.youtube.discover", return_value=[video(VID)]),
            patch("tubescribe.recent.youtube.details", side_effect=self.details),
            patch("tubescribe.recent.work", return_value={"failed": 0}) as work,
        ):
            result = self.run_recent()
        self.assertEqual(result["incomplete"], [{"video_id": VID, "stage": "asr"}])
        self.assertEqual(work.call_args.kwargs["ids"], [VID])
        self.assertTrue(work.call_args.kwargs["wait"])
        self.assertEqual(work.call_args.args[1], "http://127.0.0.1:18766")
        self.assertEqual(self.store.db.execute("SELECT asr_attempts FROM jobs").fetchone()[0], 3)

    def test_queued_selection_survives_aging_out(self):
        with (
            patch("tubescribe.recent.youtube.discover", return_value=[video(VID)]),
            patch("tubescribe.recent.youtube.details", side_effect=self.details),
        ):
            result = self.run_recent(enqueue_only=True)
        with patch("tubescribe.recent.youtube.discover", return_value=[]):
            self.run_recent(enqueue_only=True)
        row = self.store.queued_submissions()[0]
        self.assertEqual(row["id"], result["submission"])
        self.assertEqual(json.loads(row["request"])["options"]["video"], [VID])

    def test_cli_nonzero_for_incomplete_and_positive_days_validation(self):
        with (
            patch("sys.argv", ["tubescribe", "--data", str(self.root), "ensure-recent"]),
            patch(
                "tubescribe.recent.ensure_recent",
                return_value={
                    "discovery_errors": [],
                    "incomplete": [{"video_id": VID, "stage": "summary"}],
                },
            ),
            contextlib.redirect_stdout(io.StringIO()),
        ):
            self.assertEqual(main(), 1)
        with (
            patch("sys.argv", ["tubescribe", "ensure-recent", "--days", "0"]),
            contextlib.redirect_stderr(io.StringIO()),
            self.assertRaises(SystemExit) as error,
        ):
            main()
        self.assertEqual(error.exception.code, 2)
