"""Rolling digests respect publication windows, source versions and the video queue."""

import json
import tempfile
import threading
import unittest
from contextlib import closing
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import Mock, patch

import httpx

from tubescribe.cloud_pipeline import advance
from tubescribe.dashboard import make_server
from tubescribe.execution import lease
from tubescribe.rollups import (
    TOOL,
    catalogue,
    index,
    records,
    run,
    schedule,
    timestamp,
    update,
    validate,
)
from tubescribe.storage import Store
from tubescribe.submission import Dispatcher, JobRequest

NOW = datetime(2026, 10, 15, 12, tzinfo=UTC)
QUOTE = "Changing liquidity conditions can weaken historical market signals."


class Runner:
    def __init__(self):
        self.calls = []
        self.during = None

    def run(self, payload, tools):
        self.calls.append(payload)
        if self.during:
            self.during()
        sources = json.loads(payload["input"])["sources"]
        source = sources[0]
        claim = "The speakers emphasize uncertainty."
        quote = QUOTE if QUOTE in source["markdown"] else claim + " See supporting sources."
        return {
            "output": {
                "markdown": f"# Overview\n\n{claim} See supporting sources.\n\n[Source]({source['url']})",
                "evidence": [{"claim": claim, "source_id": source["id"], "source_quote": quote}],
            }
        }


class RollingSummaries(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = Store(Path(self.temp.name))
        self.addCleanup(self.store.db.close)
        self.store.channel(
            {"id": "one", "title": "First channel", "url": "https://youtube.com/one"}
        )
        self.store.channel(
            {"id": "two", "title": "Second channel", "url": "https://youtube.com/two"}
        )
        self.runner = Runner()
        self.video("recent", NOW - timedelta(days=1), summarized=True)
        self.video("missing", NOW - timedelta(days=2))
        self.video("expired", NOW - timedelta(days=8), summarized=True)
        self.video("future", NOW + timedelta(hours=1), summarized=True)
        self.video("unknown", None, summarized=True)

    def video(self, identifier, published, *, summarized=False, channel="one", profile="test"):
        stamp = str(published.timestamp()) if published else None
        self.store.index(
            channel,
            "videos",
            [
                {
                    "id": identifier,
                    "title": identifier,
                    "url": "https://youtube.com/watch?v=" + identifier,
                    "published_at": stamp,
                }
            ],
            profile,
        )
        job_id = self.store.db.execute(
            "SELECT id FROM jobs WHERE video_id=? AND profile=?", (identifier, profile)
        ).fetchone()[0]
        if summarized:
            directory = self.store.root / "artifacts" / identifier / profile
            directory.mkdir(parents=True, exist_ok=True)
            (directory / "read.md").write_text("# Source\n\n" + QUOTE)
            published_dir, manifest = self.store.publish(directory)
            self.store.ensure_stage("artifact", job_id, workflow="summary")
            self.store.stage_update(
                "artifact",
                job_id,
                workflow="summary",
                state="complete",
                output_dir=published_dir.relative_to(self.store.root).as_posix(),
                manifest_sha256=manifest.sha256,
            )
        return job_id

    def build(self, **kwargs):
        return run(self.store, clock=NOW, agent_runner=self.runner, **kwargs)

    def test_window_deduplicates_profiles_and_reports_missing_and_undated(self):
        self.video("recent", NOW - timedelta(days=1), summarized=True, profile="other")
        channels, start, end = catalogue(self.store, clock=NOW)
        self.assertEqual(set(channels["one"]["videos"]), {"recent", "missing"})
        self.assertEqual(len(channels["one"]["sources"]), 1)
        self.assertEqual(channels["one"]["undated"], 1)
        self.assertEqual(end - start, timedelta(days=7))
        self.assertEqual(timestamp("20261015"), datetime(2026, 10, 15, tzinfo=UTC).timestamp())
        self.assertEqual(timestamp("2026-10-15T12:00:00Z"), NOW.timestamp())
        self.assertIsNone(timestamp("nan"))

    def test_hierarchy_cached_rebuild_and_expiry(self):
        self.assertEqual(self.build()["completed"], 3)
        self.assertEqual(len(self.runner.calls), 2)  # empty channel needs no model
        global_input = json.loads(self.runner.calls[-1]["input"])
        self.assertEqual(global_input["sources"][0]["id"], "one")
        self.assertEqual(self.build()["skipped"], 3)
        self.assertEqual(len(self.runner.calls), 2)
        self.video("new", NOW - timedelta(hours=1), summarized=True)
        self.assertEqual(self.build()["completed"], 2)
        self.assertEqual(len(self.runner.calls), 4)
        expired = index(self.store, clock=NOW + timedelta(days=8))
        self.assertTrue(
            all(e["state"] == "pending" for e in expired["items"] if e["scope"] != "channel:two")
        )
        result = run(self.store, clock=NOW + timedelta(days=8), agent_runner=self.runner)
        self.assertEqual(result["failed"], 0)
        self.assertEqual(len(self.runner.calls), 4)  # all sources aged out; no inference

    def test_queue_drain_batches_multiple_video_completions(self):
        dispatcher = Dispatcher(self.store)
        submission = dispatcher.submit(JobRequest("work", {"video": ["recent", "missing"]}))
        with patch("tubescribe.submission.Dispatcher.submit") as submit:
            self.assertIsNone(schedule(self.store))
            submit.assert_not_called()
        self.assertTrue(self.build()["deferred"])
        self.assertFalse(self.runner.calls)
        self.video("missing", NOW - timedelta(days=2), summarized=True)
        self.assertTrue(self.build()["deferred"])
        self.store.update_submission(submission, state="complete")
        self.assertEqual(self.build()["completed"], 3)
        self.assertEqual(len(self.runner.calls), 2)
        self.assertEqual(len(json.loads(self.runner.calls[0]["input"])["sources"]), 2)
        self.assertEqual(self.build()["skipped"], 3)

    def test_work_arriving_during_inference_defers_publication_and_reuses_response(self):
        submissions = []

        def enqueue():
            submissions.append(
                Dispatcher(self.store).submit(JobRequest("work", {"video": ["recent"]}))
            )

        self.runner.during = enqueue
        result = self.build()
        self.assertTrue(result["deferred"])
        self.assertNotEqual(records(self.store)["channel:one"]["state"], "complete")
        self.assertEqual(len(self.runner.calls), 1)
        self.store.update_submission(submissions[0], state="complete")
        self.runner.during = None
        self.assertEqual(self.build()["failed"], 0)
        self.assertEqual(
            len(self.runner.calls), 2
        )  # channel response reused, only global model call

    def test_corrupt_source_and_failed_inference_are_bounded(self):
        self.runner.run = lambda *_: (_ for _ in ()).throw(RuntimeError("bridge down"))
        self.assertGreater(self.build()["failed"], 0)
        row = records(self.store)["channel:one"]
        self.assertEqual(row["attempts"], 1)
        self.assertEqual(row["state"], "failed")
        self.build()
        self.assertEqual(records(self.store)["channel:one"]["attempts"], 1)

    def test_live_manual_video_lease_blocks_aggregation(self):
        with lease(self.store.root, "video:recent"):
            self.assertTrue(self.build()["deferred"])
        self.assertEqual(self.build()["failed"], 0)

    def test_scheduler_coalesces_until_queue_drained_and_uses_native_tool_schema(self):
        with patch("tubescribe.rollups.datetime") as clock:
            clock.now.return_value = NOW
            clock.fromisoformat = datetime.fromisoformat
            clock.strptime = datetime.strptime
            first = schedule(self.store, background=False)
            self.assertEqual(first, schedule(self.store, background=False))
            self.assertEqual(len(self.store.queued_submissions()), 1)
        self.assertEqual(TOOL["type"], "function")
        self.assertIn("parameters", TOOL["function"])
        self.store.update_submission(first, state="complete")

    def test_cloud_digest_skips_exhausted_channel_and_does_not_requeue_it(self):
        exhausted = index(self.store, clock=NOW)["items"][0]
        update(
            self.store, exhausted["scope"], state="failed", attempts=3,
            input_hash=exhausted["input_hash"],
        )
        with patch("tubescribe.rollups.datetime", wraps=datetime) as clock:
            clock.now.return_value = NOW
            identifier = schedule(self.store, background=False)
            self.assertIsNotNone(identifier)
            with self.store.db:
                self.store.db.execute(
                    "UPDATE submissions SET created=? WHERE id=?", (NOW.timestamp(), identifier)
                )
            self.assertFalse(advance(self.store, identifier)["done"])
            self.assertEqual(advance(self.store, identifier), {"done": True, "state": "complete"})
            self.assertEqual(records(self.store)["channel:two"]["state"], "complete")
            self.assertNotIn("all", records(self.store))
            self.assertEqual(records(self.store)[exhausted["scope"]]["attempts"], 3)

            clock.now.return_value = NOW + timedelta(minutes=15)
            next_tick = Store(self.store.root)
            with closing(next_tick.db):
                self.assertIsNone(schedule(next_tick, background=False))
                self.assertEqual(
                    next_tick.db.execute("SELECT COUNT(*) FROM submissions").fetchone()[0], 1
                )

                self.video("changed", NOW - timedelta(hours=1), summarized=True)
                refreshed = schedule(next_tick, background=False)
                self.assertIsNotNone(refreshed)
                with next_tick.db:
                    next_tick.db.execute(
                        "UPDATE submissions SET created=? WHERE id=?",
                        ((NOW + timedelta(minutes=15)).timestamp(), refreshed),
                    )
                with patch("tubescribe.cloud_pipeline.agent_runner", return_value=self.runner):
                    self.assertFalse(advance(next_tick, refreshed)["done"])
                recovered = records(next_tick)[exhausted["scope"]]
                self.assertEqual(recovered["state"], "complete")
                self.assertEqual(recovered["attempts"], 1)

    def test_completed_empty_rollup_does_not_start_inference(self):
        with self.store.db:
            self.store.db.execute("UPDATE videos SET published_at=NULL")
        self.assertEqual(self.build()["completed"], 3)
        self.assertFalse(self.runner.calls)

    def test_retry_save_checkpoint_reuses_model_response(self):
        original = self.store.publish
        calls = []

        def unavailable(directory):
            calls.append(directory)
            if len(calls) == 1:
                raise OSError("objects unavailable")
            return original(directory)

        with patch.object(self.store, "publish", side_effect=unavailable):
            self.build()
        with self.store.db:
            self.store.db.execute("UPDATE rolling_summaries SET next_retry=0")
        self.assertEqual(self.build()["failed"], 0)
        self.assertEqual(len(self.runner.calls), 2)

    def test_queued_submission_can_be_started_when_local_scheduler_returns(self):
        request = JobRequest("rollups", {})
        identifier = Dispatcher(self.store).submit(request)
        process = Mock()
        process.start.return_value = 1234
        self.assertEqual(
            Dispatcher(self.store, process=process).submit(request, background=True), identifier
        )
        Dispatcher(self.store, process=process).submit(request, background=True)
        self.assertEqual(process.start.call_count, 1)

    def test_exported_transcript_links_become_canonical_model_sources(self):
        source = catalogue(self.store, clock=NOW)[0]["one"]["sources"][0]
        directory = self.store.root / source["key"]
        (directory / "read.md").write_text(
            QUOTE
            + "\n\n[Full transcript](../../../../../../transcripts/channel/video/preview.html)"
        )
        published, manifest = self.store.publish(directory)
        with self.store.db:
            self.store.db.execute(
                "UPDATE artifacts SET output_dir=?,manifest_sha256=? WHERE job_id=(SELECT id FROM jobs WHERE video_id='recent')",
                (published.relative_to(self.store.root).as_posix(), manifest.sha256),
            )
        self.assertEqual(self.build()["failed"], 0)
        text = json.loads(self.runner.calls[0]["input"])["sources"][0]["markdown"]
        self.assertNotIn("../", text)
        self.assertIn("/videos/recent/transcripts/test/", text)

    def test_separate_scratch_and_memory_objects_support_rollups(self):
        from tubescribe.blobs import MemoryArtifacts

        memory = MemoryArtifacts()
        for key in self.store.blobs.keys():
            if key.startswith("artifacts/"):
                memory.put(key, self.store.blobs.get(key))
        other = Store(self.store.root / "other", database=self.store.database, blobs=memory)
        try:
            result = run(other, clock=NOW, agent_runner=self.runner)
            self.assertEqual(result["failed"], 0)
            for row in records(other).values():
                self.assertTrue(other.bundle_valid(row["output_dir"], row["manifest_sha256"]))
        finally:
            other.db.close()

    def test_durable_gateway_executes_rollup_without_figure_state(self):
        from contextlib import closing

        from tubescribe.agent_gateway import Runs, execute

        payload = {"input": "summaries", "timeout_seconds": 30, "tool_context": {"kind": "rollup"}}
        with closing(Runs(self.store.root)) as runs:
            runs.create("a" * 64, payload)
        runner = Mock()
        runner.run.return_value = {"output": {"markdown": "A digest"}}
        execute(self.store.root, "a" * 64, "http://127.0.0.1:1", runner_factory=lambda _: runner)
        with closing(Runs(self.store.root)) as runs:
            row = runs.get("a" * 64)
            self.assertEqual(row["state"], "complete", row["error"])
            self.assertEqual(
                json.loads(self.store.read_key(row["response_key"]))["output"]["markdown"],
                "A digest",
            )

    def test_validation_rejects_invented_evidence_and_links(self):
        sources = [{"id": "a", "url": "/source/", "markdown": QUOTE}]
        base = {
            "markdown": "The argument. [Source](/source/)",
            "evidence": [{"claim": "The argument.", "source_id": "a", "source_quote": QUOTE}],
        }
        self.assertEqual(validate(base, sources), base["markdown"])
        for field, value in [
            ("markdown", "The argument. [Source](https://evil.example)"),
            (
                "evidence",
                [
                    {
                        "claim": "The argument.",
                        "source_id": "a",
                        "source_quote": "This is an entirely invented source quote.",
                    }
                ],
            ),
        ]:
            with self.assertRaises(ValueError):
                validate({**base, field: value}, sources)

    def test_routes_fullpage_markdown_metadata_and_no_stale_content(self):
        # Routes use live clock; pin the catalogue clock for deterministic boundary assertions.
        self.build()
        with patch("tubescribe.rollups.datetime") as clock:
            clock.now.return_value = NOW
            clock.fromisoformat = datetime.fromisoformat
            clock.strptime = datetime.strptime
            server = make_server(self.store.root)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                with httpx.Client(
                    base_url=f"http://127.0.0.1:{server.server_port}", trust_env=False
                ) as client:
                    self.assertEqual(client.get("/api/summaries").json()["days"], 7)
                    self.assertEqual(client.get("/summaries/").status_code, 200)
                    self.assertIn('href="/channels/one/summary/"', client.get("/summaries/").text)
                    self.assertIn("# Overview", client.get("/channels/one/summary/summary.md").text)
                    self.assertEqual(client.get("/channels/nope/summary/").status_code, 404)
                    self.assertEqual(
                        client.get(
                            "/channels/one/summary/", headers={"Accept": "application/json"}
                        ).json()["coverage"]["summarized"],
                        1,
                    )
                    clock.now.return_value = NOW + timedelta(days=8)
                    self.assertEqual(client.get("/summaries/").status_code, 503)
            finally:
                server.shutdown()
                server.server_close()
                thread.join()
