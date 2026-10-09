import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import httpx

from tubescribe.diarization import MODEL, REVISION, assign_speakers, validate
from tubescribe.diarization_worker import enrich_one, initialize, run
from tubescribe.storage import Store, atomic_write
from tubescribe.youtube import YouTubeError

PREDICTION = {
    "model": MODEL,
    "revision": REVISION,
    "backend": "transformers",
    "backend_version": "5.18.0",
    "torch_version": "2.8.0+cu128",
    "compute_type": "float32",
    "options": {"threshold": 0.5},
    "exclusive_policy": "highest",
    "device": "cuda:0",
    "gpu": "NVIDIA RTX 4090",
    "duration": 4.0,
    "processing_seconds": 0.5,
    "speaker_count": 2,
    "exclusive_turns": [
        {"start": 0, "end": 1, "speaker": "Speaker 1"},
        {"start": 1, "end": 3, "speaker": "Speaker 2"},
    ],
}
ASR = {
    "language": "en",
    "model": "large-v3-turbo",
    "revision": "asrhash",
    "backend": "faster-whisper",
    "backend_version": "1.2.1",
    "started_at": "start",
    "finished_at": "finish",
    "duration": 4.0,
    "processing_seconds": 1.0,
    "segments": [
        {
            "start": 0,
            "end": 4,
            "text": "Question? Answer. Unmatched.",
            "words": [
                {"start": 0, "end": 1, "word": "Question?"},
                {"start": 1.1, "end": 2, "word": " Answer."},
                {"start": 3.5, "end": 4, "word": " Unmatched."},
            ],
        }
    ],
}


class SpeakerPipeline(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.temp.name))
        self.store.channel(
            {"id": "channel", "title": "Channel", "url": "https://youtube.com/channel"}
        )
        self.store.index(
            "channel",
            "upload",
            [
                {
                    "id": "abcdefghijk",
                    "title": "Test",
                    "url": "https://youtube.com/watch?v=abcdefghijk",
                }
            ],
            "turbo-v1",
        )
        self.job = dict(
            self.store.db.execute(
                "SELECT j.*,v.channel_id,v.title,v.url,v.kind FROM jobs j JOIN videos v ON v.id=j.video_id"
            ).fetchone()
        )
        self.audio = self.store.root / "audio/test.mp4"
        self.audio.parent.mkdir()
        self.audio.write_bytes(b"audio")
        self.job["audio_path"] = "audio/test.mp4"
        self.store.save(self.job, ASR, keep_audio=True)
        self.client = Mock()
        self.client.health.return_value = {
            **PREDICTION,
            "ready": True,
            "service": "tubescribe-diarization",
        }
        self.client.diarize.return_value = PREDICTION

    def tearDown(self):
        self.store.db.close()
        self.temp.cleanup()

    def row(self):
        return dict(self.store.db.execute("SELECT * FROM diarizations").fetchone())

    def test_speaker_turns_preserve_words_and_unknown_gaps(self):
        result = assign_speakers(ASR, PREDICTION)
        self.assertEqual(
            [s["speaker_id"] for s in result["segments"]], ["Speaker 1", "Speaker 2", "UNKNOWN"]
        )
        self.assertEqual(result["asr_segments"], ASR["segments"])
        self.assertEqual(
            [w["word"] for s in result["segments"] for w in s["words"]],
            [w["word"] for w in ASR["segments"][0]["words"]],
        )

    def test_enrichment_commits_formats_preserves_asr_and_skips_rerun(self):
        self.assertTrue(enrich_one(self.store, self.job, self.client))
        row = self.row()
        self.assertEqual(row["state"], "complete")
        backup = self.store.root / row["asr_path"]
        self.assertEqual(hashlib.sha256(backup.read_bytes()).hexdigest(), row["asr_sha256"])
        self.assertEqual(json.loads(backup.read_text())["segments"], ASR["segments"])
        self.assertFalse(self.audio.exists())
        transcript = self.store.db.execute("SELECT * FROM transcripts").fetchone()
        path = self.store.root / transcript["json_path"]
        self.assertIn("Speaker 1: Question?", path.with_suffix(".srt").read_text())
        self.assertIn("**Speaker 2:** Answer.", path.with_suffix(".md").read_text())
        self.assertFalse(enrich_one(self.store, self.job, self.client))
        self.assertEqual(self.client.diarize.call_count, 1)

    def test_untranscribed_speaker_is_visible_in_all_published_formats(self):
        # The real video has a tablet voice during a gap in Whisper's words.
        # Segment-level overlap alone must not hide that untranscribed voice.
        asr = {
            **ASR,
            "segments": [
                {
                    **ASR["segments"][0],
                    "words": [
                        ASR["segments"][0]["words"][0],
                        ASR["segments"][0]["words"][2],
                    ],
                    "text": "Question? Unmatched.",
                }
            ],
        }
        self.store.save(self.job, asr, keep_audio=True)
        enrich_one(self.store, self.job, self.client)
        transcript = self.store.db.execute("SELECT * FROM transcripts").fetchone()
        path = self.store.root / transcript["json_path"]
        saved = json.loads(path.read_text())
        annotations = [
            s for s in saved["segments"] if s.get("annotation") == "untranscribed_speech"
        ]
        self.assertEqual(len(annotations), 1)
        self.assertEqual(annotations[0]["speaker_id"], "Speaker 2")
        self.assertEqual((annotations[0]["start"], annotations[0]["end"]), (1, 3))
        self.assertEqual(annotations[0]["words"], [])
        self.assertEqual(saved["asr_segments"], asr["segments"])
        self.assertEqual(
            [w for s in saved["segments"] for w in s["words"]], asr["segments"][0]["words"]
        )
        for suffix in (".md", ".srt", ".txt"):
            text = path.with_suffix(suffix).read_text()
            self.assertIn("Speaker 2", text)
            self.assertIn("Untranscribed speech", text)
            self.assertIn("00:00:01.000", text if suffix != ".srt" else text.replace(",", "."))

    def test_inference_failure_retains_audio_and_asr(self):
        self.client.diarize.side_effect = RuntimeError("GPU busy")
        with self.assertRaisesRegex(RuntimeError, "GPU busy"):
            enrich_one(self.store, self.job, self.client)
        self.assertTrue(self.audio.exists())
        row = self.row()
        self.assertEqual(row["state"], "failed")
        self.assertEqual(row["attempts"], 1)
        self.assertTrue((self.store.root / row["asr_path"]).exists())

    def test_interrupted_last_diarization_attempt_retains_its_retry_budget(self):
        initialize(self.store, self.job)
        self.store.db.execute("UPDATE diarizations SET attempts=2")
        self.store.db.commit()
        self.client.diarize.side_effect = KeyboardInterrupt
        with self.assertRaises(KeyboardInterrupt):
            enrich_one(self.store, self.job, self.client)
        self.assertEqual(self.row()["attempts"], 2)
        self.assertEqual(self.row()["state"], "pending")
        self.assertTrue(self.audio.exists())
        self.client.diarize.side_effect = None
        self.assertTrue(enrich_one(self.store, self.job, self.client))

    def test_save_failure_reuses_durable_prediction_without_gpu_or_redownload(self):
        def write(path, content):
            if path.suffix == ".md":
                raise OSError("disk full")
            atomic_write(path, content)

        with patch("tubescribe.storage.atomic_write", side_effect=write):
            with self.assertRaises(OSError):
                enrich_one(self.store, self.job, self.client)
        row = self.row()
        self.assertEqual(row["stage"], "save")
        self.assertEqual(row["save_attempts"], 1)
        self.assertTrue(self.audio.exists())
        self.assertTrue(row["prediction_path"])
        with patch("tubescribe.diarization_worker.download") as download:
            self.assertTrue(enrich_one(self.store, self.job, self.client))
            download.assert_not_called()
        self.assertEqual(self.client.diarize.call_count, 1)
        self.assertFalse(self.audio.exists())

    def test_bad_device_empty_turns_and_overlapping_turns_rejected(self):
        for changes in (
            {"device": "cpu"},
            {"exclusive_turns": []},
            {
                "exclusive_turns": [
                    {"start": 0, "end": 2, "speaker": "A"},
                    {"start": 1, "end": 3, "speaker": "B"},
                ]
            },
        ):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                validate({**PREDICTION, **changes})

    def test_corrupt_asr_checkpoint_fails_without_inference(self):
        row = initialize(self.store, self.job)
        (self.store.root / row["asr_path"]).write_text("corrupt")
        with self.assertRaisesRegex(ValueError, "Corrupt asr"):
            enrich_one(self.store, self.job, self.client)
        self.client.diarize.assert_not_called()
        self.assertEqual(self.row()["state"], "failed")
        self.assertEqual(self.row()["save_attempts"], 1)

    def test_connection_loss_pauses_batch_without_consuming_other_jobs(self):
        self.store.index(
            "channel",
            "upload",
            [
                {
                    "id": "lmnopqrstuv",
                    "title": "Second",
                    "url": "https://youtube.com/watch?v=lmnopqrstuv",
                }
            ],
            "turbo-v1",
        )
        second = dict(
            self.store.db.execute(
                "SELECT j.*,v.channel_id,v.title,v.url,v.kind FROM jobs j JOIN videos v ON v.id=j.video_id WHERE j.video_id='lmnopqrstuv'"
            ).fetchone()
        )
        self.store.save(second, ASR)
        self.store.update(self.job["id"], audio_path="audio/test.mp4")
        self.client.diarize.side_effect = httpx.ConnectError("Tunnel interrupted")
        with (
            patch(
                "tubescribe.diarization.Client.health", return_value=self.client.health.return_value
            ),
            patch("tubescribe.diarization_tunnel.ready", return_value=self.client),
        ):
            result = run(self.store, "http://127.0.0.1:18768", wait=True)
        self.assertEqual(result["completed"], 0)
        self.assertIn("paused", result)
        rows = self.store.db.execute("SELECT * FROM diarizations ORDER BY job_id").fetchall()
        self.assertEqual([r["state"] for r in rows], ["pending", "pending"])
        self.assertEqual(rows[0]["attempts"], 0)
        self.assertEqual(rows[1]["download_attempts"], 0)
        self.assertEqual(rows[1]["attempts"], 0)
        self.assertTrue(self.audio.exists())

    def test_connection_lost_during_download_reconnects_before_inference(self):
        self.client.endpoint = "http://127.0.0.1:18768"
        health = self.client.health.return_value
        self.client.health.side_effect = [health, httpx.ConnectError("Disconnected"), health]
        with patch("tubescribe.diarization_tunnel.ready", return_value=self.client) as reconnect:
            self.assertTrue(enrich_one(self.store, self.job, self.client))
        reconnect.assert_called_once_with(self.store.root)
        self.client.diarize.assert_called_once()
        self.assertEqual(self.row()["state"], "complete")

    def test_unrecoverable_transcript_rebuild_cannot_inherit_old_diarization(self):
        enrich_one(self.store, self.job, self.client, keep_audio=True)
        row = self.row()
        (self.store.root / row["asr_path"]).write_text("corrupt checkpoint")
        saved = self.store.db.execute("SELECT json_path FROM transcripts").fetchone()[0]
        (self.store.root / saved).unlink()
        self.store.update(self.job["id"], asr_attempts=3)
        self.store.recover()
        job = self.store.db.execute("SELECT * FROM jobs").fetchone()
        self.assertEqual(job["asr_state"], "pending")
        self.assertEqual(job["asr_attempts"], 0)
        self.assertEqual(self.row()["state"], "pending")
        self.assertIsNone(self.row()["asr_path"])
        self.assertIsNone(self.row()["prediction_path"])
        self.store.save(self.job, ASR, keep_audio=True)
        self.assertTrue(enrich_one(self.store, self.job, self.client))
        self.assertEqual(self.client.diarize.call_count, 2)

    def test_rate_limit_waits_before_downloading_any_more_audio(self):
        with (
            patch(
                "tubescribe.diarization.Client.health", return_value=self.client.health.return_value
            ),
            patch("tubescribe.diarization_tunnel.ready", return_value=self.client),
            patch(
                "tubescribe.diarization_worker.download", side_effect=YouTubeError("HTTP 429")
            ) as download,
            patch(
                "tubescribe.diarization_worker.time.sleep", side_effect=KeyboardInterrupt
            ) as sleep,
        ):
            with self.assertRaises(KeyboardInterrupt):
                run(self.store, "http://127.0.0.1:18768", wait=True)
        sleep.assert_called_once_with(30)
        download.assert_called_once()
        self.assertEqual(self.row()["state"], "failed")
        self.assertEqual(self.row()["stage"], "download")
