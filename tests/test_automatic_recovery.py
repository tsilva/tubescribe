"""A selected job reconnects after transport loss without another paid model run."""

import json
import os
import time
import unittest
from unittest.mock import patch

import httpx
import test_postprocessing_pipeline as pipeline
from test_pipeline import RESULT, VID

from tubescribe.agent_gateway import DurableAgentRunner, DurableRunPending
from tubescribe.changes import queue_deadline
from tubescribe.inference import AgentRunner
from tubescribe.manual_queue import PROFILE, change, request_for, status
from tubescribe.storage import Store
from tubescribe.submission import Dispatcher, Inline, JobRequest
from tubescribe.worker import work


class AutomaticRecovery(unittest.TestCase):
    def setUp(self):
        self.fixture = pipeline.FinalStage()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        settings = patch.dict(os.environ, {"TUBESCRIBE_MANUAL_QUEUE": "1"})
        settings.start()
        self.addCleanup(settings.stop)
        self.store = Store(self.fixture.store.root)
        self.addCleanup(self.store.db.close)
        change(self.store.root, "queue", [VID])
        self.submission = self.store.queued_submissions(manual=True)[0]["id"]
        self.job = self.store.pipeline_jobs(ids=[VID], profile=PROFILE)[0]
        self.store.save(self.job, {**RESULT, "segments": [
            {**RESULT["segments"][0], "speaker_id": "Speaker 1"},
        ]}, hosted_speakers=1)

    def execute(self, store):
        return Dispatcher(store).execute(request_for(VID), Inline(lambda: work(
            store, None, ids=[VID], profile=PROFILE, max_attempts=1,
            bridge=self.fixture.bridge, postprocessing="text",
        )), identifier=self.submission)

    def test_timeout_requeues_then_fresh_worker_publishes_saved_summary_once(self):
        accepted, calls = {}, []
        deliver = False

        def transport(request):
            calls.append((request.method, str(request.url)))
            if request.method == "POST":
                raw = json.loads(request.content)
                if str(request.url) in accepted:
                    self.assertEqual(accepted[str(request.url)], raw)
                else:
                    accepted[str(request.url)] = raw
            if deliver:
                return httpx.Response(200, json={"state": "complete", "result": {
                    "output": {"markdown": "# Summary\n\nA real transcript.\n\n[Full transcript](transcript)",
                               "used_image_ids": [], "evidence": [{"claim": "A real transcript.",
                                "source_quote": "**Speaker 1:** A real transcript."}]},
                    "usage": {"total": {"totalTokens": 10}}, "elapsed_seconds": 0.01,
                }})
            if request.method == "POST":
                return httpx.Response(202, json={"state": "active"})
            raise httpx.ReadTimeout("Response lost after the model completed")

        def runner(store, endpoint, key, *, profile=None):
            if key.startswith("artifacts/"):
                return DurableAgentRunner("http://127.0.0.1:1", store=store, run_key=key,
                                          transport=httpx.MockTransport(transport))
            return AgentRunner(endpoint)

        with patch("tubescribe.inference.configured_runner", side_effect=runner), patch(
            "tubescribe.openrouter.OpenRouter.transcribe", side_effect=AssertionError("ASR repeated")
        ), patch("tubescribe.agent_gateway.time.sleep"):
            with self.assertRaises(DurableRunPending):
                self.execute(self.store)
            row = self.store.submission(self.submission)
            self.assertEqual(row["state"], "queued")
            self.assertIsNone(row["pid"])
            self.assertIsNone(row["worker_id"])
            retry_at = json.loads(row["result"])["retry_at"]
            self.assertGreater(retry_at, time.time())
            self.assertEqual(self.store.queued_submissions(manual=True), [])
            self.assertAlmostEqual(queue_deadline(self.store), retry_at + 0.01)
            self.assertEqual(status(self.store.root)[0]["retry_at"], retry_at)
            summary = self.store.stage_row("artifact", self.job["id"], workflow="summary")
            self.assertEqual((summary["state"], summary["attempts"]), ("pending", 0))
            self.assertTrue(self.store.exists(summary["output_dir"] + "/agent-run.json"))
            self.assertEqual(self.fixture.requests, 1, "cleanup completed once")
            with self.store.db:
                self.store.db.execute("UPDATE claims SET expires=0 WHERE resource=?",
                                      ("submission-retry:" + self.submission,))
            replacement = Store(self.store.root)
            self.addCleanup(replacement.db.close)
            self.assertEqual(replacement.queued_submissions(manual=True)[0]["id"], self.submission)
            deliver = True
            self.execute(replacement)
            self.assertEqual(replacement.submission(self.submission)["state"], "complete")
            self.assertEqual(replacement.stage_row("artifact", self.job["id"], workflow="summary")["state"], "complete")
            self.assertEqual(len(accepted), 1, "one model run, one stable request body")
            self.assertEqual([method for method, _ in calls], ["POST", "GET", "GET", "GET", "POST"])
            self.assertEqual(self.fixture.requests, 1)
            self.assertIsNone(queue_deadline(replacement))
            self.assertEqual(replacement.pipeline_jobs(profile="turbo-v1")[0]["asr_state"], "pending")

    def test_backoff_is_persisted_capped_and_cancellable(self):
        for _ in range(10):
            self.store.update_submission(self.submission, state="active")
            self.store.defer_submission(self.submission)
        result = json.loads(self.store.submission(self.submission)["result"])
        self.assertAlmostEqual(result["retry_at"], time.time() + 300, delta=2)
        self.assertEqual(change(self.store.root, "cancel", [VID]), {"changed": 1})
        self.assertEqual(self.store.queued_submissions(manual=True), [])
        self.assertIsNone(queue_deadline(self.store))
        legacy = Dispatcher(self.store).submit(JobRequest("work", {"video": [VID]}))
        self.store.update_submission(legacy, state="active")
        with self.assertRaisesRegex(ValueError, "manually selected"):
            self.store.defer_submission(legacy)

    def test_cleanup_timeout_keeps_its_journal_and_model_budget(self):
        def runner(store, endpoint, key, *, profile=None):
            replies = iter([httpx.Response(202, json={"state": "active"}),
                            httpx.ReadTimeout("lost"), httpx.ReadTimeout("lost"),
                            httpx.ReadTimeout("lost")])

            def transport(_request):
                response = next(replies)
                if isinstance(response, Exception):
                    raise response
                return response

            return DurableAgentRunner("http://127.0.0.1:1", store=store, run_key=key,
                                      transport=httpx.MockTransport(transport))

        with patch("tubescribe.inference.configured_runner", side_effect=runner), patch(
            "tubescribe.agent_gateway.time.sleep"
        ), self.assertRaises(DurableRunPending):
            self.execute(self.store)
        row = self.store.stage_row("postprocessing", self.job["id"])
        self.assertEqual((row["state"], row["attempts"]), ("pending", 0))
        self.assertTrue(self.store.exists(row["output_dir"] + "/agent-run.json"))
        self.assertEqual(self.store.submission(self.submission)["state"], "queued")
