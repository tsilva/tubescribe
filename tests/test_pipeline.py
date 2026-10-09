import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from tubescribe.asr import Client, validate
from tubescribe.storage import Store, worker_lock
from tubescribe.worker import work
from tubescribe.youtube import YouTubeError, discover

CID = "UCabcdefghijklmnopqrstuv"
VID = "abcdefghijk"
RESULT = {
    "language": "en",
    "model": "large-v3-turbo",
    "revision": "hash",
    "backend": "faster-whisper",
    "backend_version": "1.2.1",
    "started_at": "2026-10-09T09:00:00Z",
    "finished_at": "2026-10-09T09:00:01Z",
    "processing_seconds": 1.0,
    "duration": 4.0,
    "segments": [{"start": 0.0, "end": 3.0, "text": "A real transcript."}],
}


class Pipeline(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.temp.name))
        self.store.channel(
            {"id": CID, "url": "https://www.youtube.com/channel/" + CID, "title": "Test"}
        )
        self.video = {"id": VID, "title": "Test", "url": "https://www.youtube.com/watch?v=" + VID}
        self.store.index(CID, "upload", [self.video], "turbo-v1")

    def tearDown(self):
        self.store.db.close()
        self.temp.cleanup()

    def job(self):
        return dict(self.store.db.execute("SELECT * FROM jobs").fetchone())

    def downloaded(self, *args):
        path = self.store.root / "audio" / f"{VID}.webm"
        path.parent.mkdir(exist_ok=True)
        path.write_bytes(b"fake audio")
        return path, {"title": "Test", "channel_id": CID, "duration": 4, "timestamp": 1791536000}

    def run_work(self):
        return work(self.store, "http://127.0.0.1:18766", ids=[VID])

    def test_duplicate_discovery_and_completed_skip(self):
        self.store.index(CID, "short", [self.video, self.video], "turbo-v1")
        self.assertEqual(self.store.db.execute("SELECT count(*) FROM jobs").fetchone()[0], 1)
        with (
            patch("tubescribe.worker.Client.health", return_value={**RESULT, "ready": True}),
            patch("tubescribe.worker.download", side_effect=self.downloaded) as dl,
            patch("tubescribe.worker.Client.transcribe", return_value=RESULT) as asr,
        ):
            self.assertEqual(self.run_work()["completed"], 1)
            self.assertEqual(self.run_work()["completed"], 0)
            self.assertEqual(dl.call_count, 1)
            self.assertEqual(asr.call_count, 1)
        self.assertEqual(self.job()["state"], "complete")
        self.assertFalse((self.store.root / self.job()["audio_path"]).exists())
        transcript = self.store.db.execute("SELECT * FROM transcripts").fetchone()
        self.assertEqual(
            json.loads((self.store.root / transcript["json_path"]).read_text())["video_id"], VID
        )
        self.assertIn("00:00:03.000", (self.store.root / transcript["text_path"]).read_text())

    def test_asr_failure_retains_audio_and_retry_does_not_download(self):
        with (
            patch("tubescribe.worker.Client.health", return_value={**RESULT, "ready": True}),
            patch("tubescribe.worker.download", side_effect=self.downloaded) as dl,
            patch(
                "tubescribe.worker.Client.transcribe", side_effect=RuntimeError("GPU unavailable")
            ),
        ):
            self.assertEqual(self.run_work()["failed"], 1)
        self.assertEqual(self.job()["download_state"], "complete")
        self.assertEqual(self.job()["asr_state"], "failed")
        self.assertTrue((self.store.root / self.job()["audio_path"]).exists())
        self.store.retry([VID])
        with (
            patch("tubescribe.worker.Client.health", return_value={**RESULT, "ready": True}),
            patch("tubescribe.worker.download") as dl,
            patch("tubescribe.worker.Client.transcribe", return_value=RESULT),
        ):
            self.assertEqual(self.run_work()["completed"], 1)
            dl.assert_not_called()

    def test_save_failure_retains_source(self):
        with (
            patch("tubescribe.worker.Client.health", return_value={**RESULT, "ready": True}),
            patch("tubescribe.worker.download", side_effect=self.downloaded),
            patch("tubescribe.worker.Client.transcribe", return_value=RESULT),
            patch("tubescribe.storage.atomic_write", side_effect=OSError("disk full")),
        ):
            self.assertEqual(self.run_work()["failed"], 1)
        self.assertTrue((self.store.root / self.job()["audio_path"]).exists())
        self.assertEqual(self.store.db.execute("SELECT count(*) FROM transcripts").fetchone()[0], 0)
        self.assertEqual(self.job()["failed_stage"], "save")

    def test_interruption_recovery_and_exclusive_worker(self):
        self.store.update(self.job()["id"], state="active", download_state="active")
        self.store.recover()
        self.assertEqual(self.job()["state"], "pending")
        self.assertEqual(self.job()["download_state"], "pending")
        with worker_lock(self.store.root):
            with self.assertRaises(RuntimeError):
                with worker_lock(self.store.root):
                    pass

    def test_missing_transcript_reopens_job(self):
        with (
            patch("tubescribe.worker.Client.health", return_value={**RESULT, "ready": True}),
            patch("tubescribe.worker.download", side_effect=self.downloaded),
            patch("tubescribe.worker.Client.transcribe", return_value=RESULT),
        ):
            self.run_work()
        t = self.store.db.execute("SELECT json_path FROM transcripts").fetchone()[0]
        (self.store.root / t).unlink()
        self.store.recover()
        self.assertEqual(self.job()["state"], "pending")

    def test_bounded_attempts_and_backoff(self):
        self.store.update(self.job()["id"], state="failed", asr_attempts=3)
        self.assertEqual(self.store.jobs(), [])
        self.store.retry()
        self.store.update(self.job()["id"], next_retry=9999999999)
        self.assertEqual(self.store.jobs(), [])

    def test_absent_tabs_are_empty_but_bot_checks_are_errors(self):
        with patch(
            "tubescribe.youtube.run",
            side_effect=YouTubeError("This channel does not have a shorts tab"),
        ):
            self.assertEqual(discover(CID, "short"), [])
        with patch(
            "tubescribe.youtube.run",
            side_effect=YouTubeError("Sign in to confirm you are not a bot"),
        ):
            with self.assertRaises(YouTubeError):
                discover(CID, "short")

    def test_invalid_or_empty_transcript_is_rejected(self):
        for result in (
            {**RESULT, "segments": []},
            {**RESULT, "segments": [{"start": 3, "end": 2, "text": "bad"}]},
            {**RESULT, "duration": float("nan")},
        ):
            with self.assertRaises(ValueError):
                validate(result)
        validate(RESULT)

    def test_health_requires_rtx4090_cuda(self):
        with patch("tubescribe.asr.httpx.get") as request:
            for device, gpu in [("cpu", "NVIDIA GeForce RTX 4090"), ("cuda", "NVIDIA RTX 3090")]:
                request.return_value.json.return_value = {
                    "service": "tubescribe-asr",
                    "device": device,
                    "gpu_name": gpu,
                }
                with self.assertRaises(RuntimeError):
                    Client("http://127.0.0.1:18766").health()
            request.return_value.json.return_value = {
                "service": "tubescribe-asr",
                "device": "cuda",
                "gpu_name": "NVIDIA GeForce RTX 4090",
            }
            self.assertEqual(Client("http://127.0.0.1:18766").health()["device"], "cuda")

    def test_profile_change_requires_explicit_upgrade(self):
        self.store.bind_profile("turbo-v1", RESULT)
        with self.assertRaises(RuntimeError):
            self.store.bind_profile("turbo-v1", {**RESULT, "revision": "new-revision"})
        self.store.bind_profile("turbo-v2", {**RESULT, "revision": "new-revision"})

    def test_model_change_during_request_retains_audio(self):
        with (
            patch("tubescribe.worker.Client.health", return_value={**RESULT, "ready": True}),
            patch("tubescribe.worker.download", side_effect=self.downloaded),
            patch(
                "tubescribe.worker.Client.transcribe",
                return_value={**RESULT, "revision": "changed"},
            ),
        ):
            self.assertEqual(self.run_work()["failed"], 1)
        self.assertTrue((self.store.root / self.job()["audio_path"]).exists())
        self.assertEqual(self.job()["state"], "failed")

    def test_keep_audio_and_unknown_ids(self):
        with (
            patch("tubescribe.worker.Client.health", return_value={**RESULT, "ready": True}),
            patch("tubescribe.worker.download", side_effect=self.downloaded),
            patch("tubescribe.worker.Client.transcribe", return_value=RESULT),
        ):
            work(self.store, "http://127.0.0.1:18766", ids=[VID], keep_audio=True)
        self.assertTrue((self.store.root / self.job()["audio_path"]).exists())
        with self.assertRaises(ValueError):
            work(self.store, "http://127.0.0.1:18766", ids=["nonexistent"])

    def test_explicit_new_profile_preserves_prior_job(self):
        self.store.index(CID, "upload", [self.video], "upgrade-v2")
        self.assertEqual(self.store.db.execute("SELECT count(*) FROM jobs").fetchone()[0], 2)


if __name__ == "__main__":
    unittest.main()
