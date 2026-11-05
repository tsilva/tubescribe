import json
import tempfile
import unittest
from pathlib import Path

from tubescribe.config import OPENROUTER_PROFILE
from tubescribe.pipeline_details import details
from tubescribe.storage import Store


class PipelineDetails(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.store = Store(self.root)
        self.addCleanup(self.store.db.close)
        self.store.channel({"id": "channel", "title": "Test", "url": "https://example.test"})
        self.store.index(
            "channel",
            "upload",
            [
                {
                    "id": "video",
                    "title": "Test",
                    "url": "https://example.test/video",
                    "duration": 120,
                }
            ],
            None,
        )

    def job(self, profile=OPENROUTER_PROFILE):
        with self.store.db:
            self.store.db.execute(
                "INSERT INTO jobs(video_id,profile,updated_at) VALUES('video',?,'now')", (profile,)
            )
        return self.store.db.execute("SELECT id FROM jobs WHERE profile=?", (profile,)).fetchone()[
            0
        ]

    def attempt(self, job_id, stage, usage, state="complete", workflow=None):
        identifier = self.store.begin_attempt(job_id, stage, workflow)
        self.store.finish_attempt(identifier, state, metrics=usage)
        with self.store.db:
            self.store.db.execute("UPDATE attempts SET elapsed=2 WHERE id=?", (identifier,))

    def test_discovered_and_missing_videos_and_profiles(self):
        result = details(self.root, "video")
        self.assertIsNone(result["cost_usd"])
        self.assertIsNone(result["processing_seconds"])
        self.assertFalse(result["complete"])
        self.assertEqual([s["state"] for s in result["steps"]], ["pending"] * 5)
        self.assertEqual(result["duration_seconds"], 120)
        with self.assertRaises(FileNotFoundError):
            details(self.root, "missing")
        with self.assertRaises(FileNotFoundError):
            details(self.root, "video", "missing")

    def test_hosted_cost_includes_attempts_without_billing_saved_publication_twice(self):
        job_id = self.job()
        self.attempt(job_id, "asr", {"cost": 0.02})
        self.attempt(job_id, "asr", {"cost": 0.03})
        report = {
            "publication_key": "postprocessing/run",
            "usage": {"total": {"cost": 0.04, "totalTokens": 150}},
        }
        self.attempt(job_id, "process_one", report)
        self.attempt(job_id, "process_one", report)
        self.attempt(
            job_id, "build_one", {"usage": {"total": {"totalTokens": 50}}}, workflow="summary"
        )
        self.attempt(job_id, "build_one", {"cost": 99}, workflow="other")
        other = self.job("other-profile")
        self.attempt(other, "asr", {"cost": 99})
        result = details(self.root, "video", OPENROUTER_PROFILE)
        self.assertAlmostEqual(result["cost_usd"], 0.09)
        self.assertFalse(result["cost_complete"])
        self.assertEqual(result["total_tokens"], 200)
        self.assertEqual(result["processing_seconds"], 10)
        self.assertEqual(result["steps"][2]["state"], "included")
        self.assertEqual(result["steps"][3]["attempts"], 2)
        self.assertEqual(result["steps"][3]["total_tokens"], 150)
        self.assertNotIn("publication_key", json.dumps(result))

    def test_invalid_and_unknown_costs_do_not_become_zero_or_estimates(self):
        job_id = self.job()
        for value in (None, -1, float("nan"), float("inf"), True, "0.1"):
            self.attempt(job_id, "asr", {"cost": value})
        result = details(self.root, "video")
        self.assertIsNone(result["cost_usd"])
        self.assertFalse(result["steps"][1]["cost_complete"])
        self.attempt(job_id, "asr", {"cost": 0})
        result = details(self.root, "video")
        self.assertEqual(result["cost_usd"], 0)
        self.assertFalse(result["steps"][1]["cost_complete"])

    def test_cached_provider_generations_are_counted_once(self):
        job_id = self.job()
        self.attempt(job_id, "asr", {"cost": 0.03, "generation_ids": ["one", "two"]})
        self.attempt(job_id, "asr", {"cost": 0.03, "generation_ids": ["two", "one"]})
        self.assertEqual(details(self.root, "video")["cost_usd"], 0.03)
        self.assertEqual(details(self.root, "video")["steps"][1]["attempts"], 2)

    def test_hosted_media_cost_tracks_cumulative_run_usage_once(self):
        from tubescribe.cloud_download import download_metrics

        job = self.job()
        state = {"nonce": "selected-request", "run": {"id": "media-run", "usageTotalUsd": 0}}
        self.attempt(job, "download", download_metrics(state), "pending")
        state["run"]["usageTotalUsd"] = .372
        self.attempt(job, "download", download_metrics(state))
        self.attempt(job, "download", download_metrics(state))
        result = details(self.root, "video")
        self.assertEqual(result["steps"][0]["cost_usd"], .372)
        self.assertEqual(result["steps"][0]["cost_source"], "provider_reported")
        self.assertEqual(result["cost_usd"], .372)

    def test_failures_interruptions_and_retry_are_presented_without_raw_errors(self):
        job_id = self.job("turbo-v1")
        with self.store.db:
            self.store.db.execute(
                "UPDATE jobs SET download_state='failed',state='failed',failed_stage='download',"
                "error='private provider output',next_retry=123 WHERE id=?",
                (job_id,),
            )
        result = details(self.root, "video")
        self.assertEqual(result["steps"][0]["state"], "failed")
        self.assertEqual(result["steps"][0]["retry_at"], 123)
        self.assertNotIn("private provider output", json.dumps(result))
        self.attempt(job_id, "asr", {}, "interrupted")
        with self.store.db:
            self.store.db.execute(
                "UPDATE jobs SET state='active',asr_state='active',download_state='complete' WHERE id=?",
                (job_id,),
            )
        self.assertEqual(details(self.root, "video")["steps"][1]["state"], "interrupted")

    def test_complete_local_pipeline_has_stats_and_unknown_billing(self):
        job_id = self.job("turbo-v1")
        job = dict(
            self.store.db.execute(
                "SELECT j.*,v.channel_id,v.title,v.url,v.kind "
                "FROM jobs j JOIN videos v ON v.id=j.video_id WHERE j.id=?",
                (job_id,),
            ).fetchone()
        )
        self.store.save(
            job,
            {
                "language": "en",
                "model": "test-model",
                "revision": "test",
                "duration": 120,
                "processing_seconds": 12,
                "segments": [{"start": 0, "end": 120, "text": "Test transcript."}],
            },
        )
        with self.store.db:
            self.store.db.execute("UPDATE jobs SET download_state='complete' WHERE id=?", (job_id,))
            self.store.db.execute(
                "INSERT INTO diarizations(job_id,state,updated_at,speaker_count,processing_seconds) "
                "VALUES(?,'complete','now',2,3)",
                (job_id,),
            )
            self.store.db.execute(
                "INSERT INTO postprocessings(job_id,state,updated_at,figures,total_tokens,agent_seconds) "
                "VALUES(?,'complete','now',4,100,5)",
                (job_id,),
            )
            self.store.db.execute(
                "INSERT INTO artifacts(job_id,workflow,state,updated_at) "
                "VALUES(?,'summary','complete','now')",
                (job_id,),
            )
        result = details(self.root, "video")
        self.assertTrue(result["complete"])
        self.assertEqual([s["state"] for s in result["steps"]], ["complete"] * 5)
        self.assertEqual(
            (result["model"], result["language"], result["speakers"], result["figures"]),
            ("test-model", "en", 2, 4),
        )
        self.assertEqual(result["processing_seconds"], 20)
        self.assertFalse(result["cost_complete"])
        self.assertIsNone(result["cost_usd"])

    def test_later_receipt_resolves_unknown_cached_generation_cost(self):
        job = self.job()
        self.attempt(job, "asr", {"cost": None, "generation_ids": ["known"]})
        self.attempt(
            job,
            "asr",
            {
                "cost": 0.02,
                "generation_ids": ["known"],
                "charges": [
                    {
                        "request_key": "same-request",
                        "key": "openrouter:known",
                        "cost_usd": 0.02,
                        "total_tokens": 10,
                    },
                ],
            },
        )
        result = details(self.root, "video")
        self.assertAlmostEqual(result["cost_usd"], 0.02)
        self.assertTrue(result["steps"][1]["cost_complete"])

    def test_legacy_aggregate_does_not_double_bill_replayed_partial_receipts(self):
        job = self.job()
        self.attempt(job, "asr", {"cost": 0.03, "generation_ids": ["one", "two"]})
        self.attempt(
            job,
            "asr",
            {
                "charges": [
                    {
                        "request_key": "cached-one",
                        "key": "openrouter:one",
                        "generation_id": "one",
                        "cost_usd": 0.01,
                        "total_tokens": 10,
                    }
                ]
            },
            state="failed",
        )
        result = details(self.root, "video")
        self.assertAlmostEqual(result["cost_usd"], 0.03)
        self.assertTrue(result["steps"][1]["cost_complete"])

    def test_malformed_charge_metadata_remains_unknown_without_breaking_reader(self):
        job = self.job()
        self.attempt(
            job,
            "asr",
            {
                "charges": [
                    {
                        "request_key": "saved-request",
                        "key": {},
                        "generation_id": {},
                        "cost_usd": True,
                        "total_tokens": "10",
                    }
                ]
            },
        )
        result = details(self.root, "video")
        self.assertIsNone(result["cost_usd"])
        self.assertFalse(result["steps"][1]["cost_complete"])
