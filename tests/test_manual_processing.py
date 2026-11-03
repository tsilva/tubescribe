import io
import json
import os
import tempfile
import unittest
import wave
from contextlib import redirect_stdout
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import patch

import httpx

from tubescribe.cli import main
from tubescribe.feed_watch import poll_feeds
from tubescribe.manual_queue import PROFILE, change, status
from tubescribe.openrouter import PLACEHOLDER, cached_response, cues, proxy_request
from tubescribe.postprocessing_worker import initialize
from tubescribe.rollups import video_work_pending
from tubescribe.storage import Store
from tubescribe.submission import Dispatcher, JobRequest

VIDEO = "abcdefghijk"
OTHER = "lmnopqrstuv"
CHANNEL = "UCabcdefghijklmnopqrstuv"


class ManualProcessing(unittest.TestCase):
    def setUp(self):
        settings = patch.dict(os.environ, {"TUBESCRIBE_MANUAL_QUEUE": "1"})
        settings.start()
        self.addCleanup(settings.stop)
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.store = Store(Path(folder.name))
        self.addCleanup(self.store.db.close)
        self.store.channel({"id": CHANNEL, "title": "Channel", "url": "https://example.test"})

    def discover(self):
        now = datetime.now(UTC)
        videos = [
            {
                "id": vid,
                "title": vid,
                "url": f"https://www.youtube.com/watch?v={vid}",
                "published_at": str(now.timestamp()),
            }
            for vid in (VIDEO, OTHER)
        ]
        with (
            patch("tubescribe.feed_watch.fetch_feed", return_value=videos),
            patch(
                "tubescribe.feed_watch.youtube.details",
                side_effect=lambda vid: {
                    "id": vid,
                    "channel_id": CHANNEL,
                    "timestamp": now.timestamp(),
                    "duration": 1,
                },
            ),
        ):
            result = poll_feeds(self.store, None, clock=now)
        self.assertEqual(result["videos"], [VIDEO, OTHER])
        self.assertEqual(result["errors"], [])
        self.assertEqual(self.store.db.execute("SELECT COUNT(*) FROM jobs").fetchone()[0], 0)
        self.assertEqual(self.store.submission_records(), [])

    def test_only_selected_video_runs_through_cli_without_provider_speakers(self):
        self.discover()
        legacy = Dispatcher(self.store).submit(JobRequest("work", {"video": [OTHER]}))
        self.assertFalse(video_work_pending(self.store))
        with self.assertRaisesRegex(ValueError, "no longer in the catalogue"):
            change(self.store.root, "queue", [VIDEO, "zzzzzzzzzzz"])
        self.assertEqual(self.store.queued_submissions(manual=True), [])
        self.assertEqual(change(self.store.root, "queue", [VIDEO])["changed"], 1)
        self.assertTrue(video_work_pending(self.store))
        self.assertEqual(change(self.store.root, "queue", [VIDEO])["changed"], 0)
        [selected] = self.store.queued_submissions(manual=True)
        self.assertEqual([row["id"] for row in self.store.queued_submissions()], [legacy])
        self.assertEqual(self.store.pipeline_jobs(profile=PROFILE)[0]["video_id"], VIDEO)
        self.assertEqual(change(self.store.root, "cancel", [VIDEO])["changed"], 1)
        self.assertEqual(self.store.queued_submissions(manual=True), [])
        self.assertEqual(change(self.store.root, "queue", [VIDEO])["changed"], 1)
        [selected] = self.store.queued_submissions(manual=True)
        if self.store.config.network:
            self.assertEqual(
                self.store.claim_submission("fixture", manual=True)["id"], selected["id"]
            )
            self.assertEqual(change(self.store.root, "cancel", [VIDEO])["changed"], 0)
        root = self.store.root
        audio = root / "audio" / f"{VIDEO}.wav"
        audio.parent.mkdir()
        with wave.open(str(audio), "wb") as wav:
            wav.setparams((1, 2, 16000, 16000, "NONE", ""))
            wav.writeframes(b"\0" * 32000)
        reply = {
            "status": 200,
            "generation_id": "fixture",
            "response": {
                "text": "hello",
                "language": "en",
                "usage": {"cost": 0.0000333},
                "words": [{"word": "hello", "start": 0.1, "end": 0.6}],
            },
        }

        def finish(store, job, **_options):
            initialize(store, job, "text")
            store.stage_update("postprocessing", job["id"], state="complete")
            store.ensure_stage("artifact", job["id"], workflow="summary")
            store.stage_update("artifact", job["id"], workflow="summary", state="complete")
            return []

        argv = [
            "tubescribe",
            "--data",
            str(root),
            "--submission",
            selected["id"],
            "work",
            "--profile",
            PROFILE,
            "--max-attempts",
            "1",
            "--video",
            VIDEO,
        ]
        with (
            patch("sys.argv", argv),
            patch("tubescribe.openrouter.launcher", return_value=Path("/fixture")),
            patch("tubescribe.openrouter.call", return_value=reply) as hosted,
            patch(
                "tubescribe.worker.download",
                return_value=(audio, {"channel_id": CHANNEL, "duration": 1}),
            ),
            patch("tubescribe.worker.ensure_ready", side_effect=AssertionError("ASR tunnel used")),
            patch(
                "tubescribe.worker.ready_diarization",
                side_effect=AssertionError("Diarization tunnel used"),
            ),
            patch("tubescribe.pipeline.finish", side_effect=finish),
            redirect_stdout(io.StringIO()) as output,
        ):
            self.assertEqual(main(), 0, output.getvalue())
        hosted.assert_called_once()
        [record] = status(root)
        self.assertEqual(record, {"video_id": VIDEO, "state": "complete", "cost": 0.0000333})
        [job] = self.store.pipeline_jobs(profile=PROFILE)
        self.assertIsNone(job["diarization_state"])
        result = json.loads(self.store.read_key(self.store.transcript(job["id"])["json_path"]))
        self.assertIsNone(result["segments"][0]["speaker_id"])
        self.assertEqual(result["segments"][0]["start"], 0.1)

    def test_paid_checkpoint_reuses_response_and_blocks_ambiguous_replay(self):
        with patch(
            "tubescribe.openrouter.call", return_value={"status": 200, "response": {}}
        ) as call:
            self.assertEqual(cached_response(self.store, "request", None, b"audio")["status"], 200)
            self.assertEqual(cached_response(self.store, "request", None, b"audio")["status"], 200)
            with self.assertRaisesRegex(ValueError, "Decoded audio changed"):
                cached_response(self.store, "request", None, b"other audio")
            call.assert_called_once()
        with patch("tubescribe.openrouter.call", side_effect=ValueError("lost response")) as call:
            with self.assertRaisesRegex(ValueError, "outcome unknown"):
                cached_response(self.store, "unknown", None, b"audio")
            with self.assertRaisesRegex(ValueError, "outcome unknown"):
                cached_response(self.store, "unknown", None, b"audio")
            call.assert_called_once()

    def test_proxy_client_requires_placeholder_and_loopback(self):
        with (
            patch.dict(os.environ, {"OPENROUTER_API_KEY": "unbrokered-fixture"}, clear=True),
            patch("tubescribe.openrouter.httpx.Client") as client,
            patch("tubescribe.openrouter.ssl.create_default_context") as tls,
        ):
            with self.assertRaisesRegex(ValueError, "placeholder"):
                proxy_request(b"{}")
            os.environ.update(
                OPENROUTER_API_KEY=PLACEHOLDER, HTTPS_PROXY="https://external.test:1234"
            )
            with self.assertRaisesRegex(ValueError, "loopback proxy"):
                proxy_request(b"{}")
            client.assert_not_called()
            os.environ.update(HTTPS_PROXY="http://127.0.0.1:1234", SSL_CERT_FILE="fixture-ca.pem")
            transport = client.return_value.__enter__.return_value
            transport.post.return_value = httpx.Response(200, json={"text": "fixture"})
            self.assertEqual(
                proxy_request(b"{}"),
                {"status": 200, "generation_id": None, "response": {"text": "fixture"}},
            )
            tls.assert_called_once_with(cafile="fixture-ca.pem")
            self.assertFalse(client.call_args.kwargs["trust_env"])
            transport.post.assert_called_once_with(
                "https://openrouter.ai/api/v1/audio/transcriptions",
                content=b"{}",
                headers={
                    "Authorization": "Bearer " + PLACEHOLDER,
                    "Content-Type": "application/json",
                },
            )

    def test_speaker_labels_are_scoped_to_each_part(self):
        response = {"words": [{"word": "test", "start": 1, "end": 2, "speaker": 0}]}
        [first] = cues(response, 0, 10)
        [second] = cues(response, 10, 10, first_speaker=2)
        self.assertNotEqual(first["speaker_id"], second["speaker_id"])
        self.assertEqual(second["start"], 11)
