"""Synthetic cloud boundaries: admission, chunk replay, suspended tools and discovery."""

import asyncio
import hashlib
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
import wave
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import AsyncMock, Mock, patch

import httpx

from tubescribe.agent_gateway import DurableRunPending
from tubescribe.cloud_pipeline import advance, asr_step, prefix
from tubescribe.cloud_sandbox import media_job
from tubescribe.config import OPENROUTER_PROFILE
from tubescribe.manual_queue import change
from tubescribe.openrouter_agent import OpenRouterAgentRunner
from tubescribe.storage import Store
from tubescribe.workflow_runtime import canonical, discover

CHANNEL = "UCabcdefghijklmnopqrstuv"
VIDEO = "abcdefghijk"


class CloudPipeline(unittest.TestCase):
    def setUp(self):
        settings = patch.dict(os.environ, {"TUBESCRIBE_MANUAL_QUEUE": "1"})
        settings.start()
        self.addCleanup(settings.stop)
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.store = Store(Path(temporary.name))
        self.addCleanup(self.store.db.close)
        self.store.channel({"id": CHANNEL, "title": "Example", "url": "https://example.test"})
        self.store.index(CHANNEL, "upload", [{"id": VIDEO, "title": "Example",
                                             "url": "https://youtube.com/watch?v=" + VIDEO}], None)

    def select(self):
        change(self.store.root, "queue", [VIDEO])
        identifier = self.store.db.execute("SELECT id FROM submissions").fetchone()[0]
        job = self.store.pipeline_jobs(ids=[VIDEO], profile=OPENROUTER_PROFILE)[0]
        return identifier, job

    def media(self, job, parts=2):
        media = {"duration": parts, "parts": [], "video_key": "prepared/video.webm"}
        for index in range(parts):
            pcm = b"\x00\x01" * 16000
            key = f"prepared/{index}.pcm"
            ref = self.store.blobs.put(key, pcm)
            media["parts"].append({"key": key, "sha256": ref.sha256, "seconds": 1})
        return media

    def response(self, audio):
        with wave.open(io.BytesIO(audio)) as source:
            duration = source.getnframes() / source.getframerate()
        words = [{"word": "Hello", "start": .05, "end": .85, "speaker": "voice"}]
        if duration > 1:
            words.append({"word": "again", "start": duration - .9, "end": duration - .1,
                          "speaker": "voice"})
        return {"status": 200, "generation_id": "generation-" + hashlib.sha256(audio).hexdigest(),
                "response": {"language": "en", "words": words, "usage": {"cost": .001}}}

    def test_discovery_never_creates_a_processing_job(self):
        xml = f'''<feed xmlns="http://www.w3.org/2005/Atom"
          xmlns:yt="http://www.youtube.com/xml/schemas/2015">
          <yt:channelId>{CHANNEL}</yt:channelId><entry>
          <yt:channelId>{CHANNEL}</yt:channelId><yt:videoId>lmnopqrstuv</yt:videoId>
          <title>New publication</title><published>2026-11-05T10:00:00Z</published>
          </entry></feed>'''
        response = httpx.Response(200, content=xml, request=httpx.Request("GET", "https://example.test"))
        for _ in range(2):
            discover(self.store, CHANNEL, request=lambda *a, **k: response)
        self.assertEqual(self.store.db.execute("SELECT COUNT(*) FROM videos").fetchone()[0], 2)
        self.assertEqual(self.store.db.execute("SELECT COUNT(*) FROM jobs").fetchone()[0], 0)
        self.assertEqual(self.store.db.execute("SELECT COUNT(*) FROM submissions").fetchone()[0], 0)

    def test_unselected_or_cancelled_video_cannot_start_media_or_inference(self):
        prepare, send = Mock(), Mock()
        self.assertTrue(advance(self.store, "missing", send=send, media_prepare=prepare)["done"])
        identifier, _ = self.select()
        change(self.store.root, "cancel", [VIDEO])
        advance(self.store, identifier, send=send, media_prepare=prepare)
        prepare.assert_not_called()
        send.assert_not_called()

    def test_each_step_transcribes_one_chunk_and_retains_speaker_references(self):
        identifier, job = self.select()
        media = self.media(job)
        send = Mock(side_effect=self.response)
        def prepare(*args):
            return media
        with patch("subprocess.run", side_effect=AssertionError("No local media subprocess")):
            self.assertFalse(advance(self.store, identifier, send=send, media_prepare=prepare)["done"])
            self.assertEqual(send.call_count, 1)
            state = json.loads(self.store.read_key(prefix(job) + "/workflow/asr.json"))
            self.assertEqual(state["part"], 1)
            self.assertTrue(state["bank"]["anchors"])
            advance(self.store, identifier, send=send, media_prepare=prepare)
        self.assertEqual(send.call_count, 2)
        transcript = self.store.transcript(job["id"])
        saved = json.loads(self.store.read_key(transcript["json_path"], transcript["json_sha256"]))
        self.assertEqual(saved["timing_resolution"], "word")
        self.assertEqual({cue["speaker_id"] for cue in saved["segments"]}, {"Speaker 1"})
        self.assertGreater(saved["speaker_continuity"]["matched_references"], 0)
        self.assertGreaterEqual(saved["segments"][-1]["start"], 1)

    def test_lost_chunk_state_commit_replays_saved_response_without_a_second_charge(self):
        _, job = self.select()
        media = self.media(job, parts=1)
        send = Mock(side_effect=self.response)
        original = self.store.blobs.put
        failed = False

        def put(key, content):
            nonlocal failed
            if key.endswith("/workflow/asr.json") and not failed:
                failed = True
                raise OSError("Synthetic failed checkpoint commit")
            return original(key, content)

        with patch.object(self.store.blobs, "put", side_effect=put), self.assertRaises(OSError):
            asr_step(self.store, job, media, send)
        self.assertTrue(asr_step(self.store, job, media, send))
        self.assertEqual(send.call_count, 1)

    def test_missing_word_timestamps_never_claim_word_precision(self):
        _, job = self.select()
        media = self.media(job, parts=1)
        result = {"status": 200, "response": {"text": "Synthetic speech", "usage": {"cost": .001}}}
        self.assertTrue(asr_step(self.store, job, media, lambda _: result))
        saved = self.store.transcript(job["id"])
        value = json.loads(self.store.read_key(saved["json_path"]))
        self.assertEqual(value["timing_resolution"], "audio_part")

    def test_confirmed_rate_limit_recovers_automatically_without_an_ambiguous_repeat(self):
        identifier, job = self.select()
        media = self.media(job, parts=1)
        calls = []

        def send(audio):
            calls.append(audio)
            return {"status": 429} if len(calls) == 1 else self.response(audio)

        first = advance(self.store, identifier, send=send, media_prepare=lambda *args: media)
        self.assertEqual(first, {"done": False, "delay": 60})
        self.assertEqual(self.store.submission(identifier)["state"], "active")
        advance(self.store, identifier, send=send, media_prepare=lambda *args: media)
        self.assertIsNotNone(self.store.transcript(job["id"]))
        self.assertEqual(len(calls), 2)

    def test_ambiguous_audio_timeout_is_visible_and_never_automatically_reissued(self):
        identifier, job = self.select()
        media = self.media(job, parts=1)
        send = Mock(side_effect=OSError("Synthetic lost acknowledgement"))
        result = advance(self.store, identifier, send=send, media_prepare=lambda *args: media)
        self.assertEqual(result, {"done": True, "state": "needs_review"})
        self.assertIn("needs review", self.store.submission(identifier)["error"])
        change(self.store.root, "retry", [VIDEO])
        retried = self.store.db.execute("SELECT id FROM submissions ORDER BY created DESC LIMIT 1").fetchone()[0]
        result = advance(self.store, retried, send=send, media_prepare=lambda *args: media)
        self.assertEqual(result["state"], "needs_review")
        self.assertEqual(send.call_count, 1)

    def test_media_wait_releases_the_step_and_a_saved_result_stops_its_box(self):
        start, stop = AsyncMock(), AsyncMock()
        with patch("tubescribe.cloud_sandbox.signed", return_value="synthetic-capability"), patch(
            "tubescribe.cloud_sandbox.start_box", start
        ), patch("tubescribe.cloud_sandbox.stop_box", stop):
            for _ in range(2):
                with self.assertRaises(DurableRunPending):
                    media_job(self.store, "cloud/test", {"operation": "prepare"})
            self.assertEqual(start.await_count, 1)
            control = json.loads(self.store.read_key("cloud/test/control.json"))
            self.store.blobs.put(control["result_key"], b'{"ok":true}')
            media_job(self.store, "cloud/test", {})
            media_job(self.store, "cloud/test", {})
            self.assertEqual(stop.await_count, 1)

    def test_native_dispatch_deduplicates_live_runs_and_keeps_a_failed_wake_queued(self):
        from tubescribe import workflow_dispatch as dispatch

        identifier, _ = self.select()

        @contextmanager
        def archive():
            yield self.store

        run = Mock(run_id="wrun_synthetic")
        with patch.object(dispatch, "enabled", return_value=True), patch.object(
            dispatch, "archive", archive
        ), patch.object(dispatch.workflow, "start", AsyncMock(return_value=run)) as start, patch.object(
            dispatch.workflow, "Run", return_value=Mock(status=AsyncMock(return_value="running"))
        ):
            asyncio.run(dispatch.maintain(clock=False))
            asyncio.run(dispatch.maintain(clock=False))
            self.assertEqual(start.await_count, 1)
        self.assertEqual(self.store.submission(identifier)["state"], "queued")

    def test_only_the_canonical_workflow_run_can_execute_a_selection(self):
        identifier, _ = self.select()
        self.assertIsNone(canonical(self.store, identifier, "run-first"))
        self.store.blobs.put("cloud/control/selections/" + identifier + ".json",
                             b'{"run_id":"run-first"}')
        self.assertTrue(canonical(self.store, identifier, "run-first"))
        self.assertFalse(canonical(self.store, identifier, "run-duplicate"))

    def test_failed_media_retries_have_separate_bounded_result_keys(self):
        start, stop = AsyncMock(), AsyncMock()
        with patch("tubescribe.cloud_sandbox.signed", return_value="synthetic-capability"), patch(
            "tubescribe.cloud_sandbox.start_box", start
        ), patch("tubescribe.cloud_sandbox.stop_box", stop):
            for attempt in range(1, 4):
                with self.assertRaises(DurableRunPending):
                    media_job(self.store, "cloud/retry", {})
                control = json.loads(self.store.read_key("cloud/retry/control.json"))
                self.assertEqual(control["attempt"], attempt)
                self.assertIn(f"attempt-{attempt}/", control["result_key"])
                self.store.blobs.put(control["result_key"], b'{"ok":false}')
            with self.assertRaisesRegex(ValueError, "three safe attempts"):
                media_job(self.store, "cloud/retry", {})
            self.assertEqual(start.await_count, 3)
            self.assertEqual(stop.await_count, 3)

    def test_selected_video_publishes_transcript_and_summary_without_local_media_or_bridge(self):
        identifier, job = self.select()
        media = self.media(job, parts=1)
        self.store.blobs.put(prefix(job) + "/cloud-media/manifest.json", json.dumps(media).encode())

        def speech(_):
            words = [{"word": word, "start": i * .25, "end": (i + 1) * .25,
                      "speaker": "voice"} for i, word in enumerate(["A", "real", "transcript."])]
            return {"status": 200, "response": {"words": words, "language": "en",
                                                "usage": {"cost": .001}}}

        requests = []

        def text(envelope):
            request = envelope["request"]
            requests.append(request)
            summary = "evidence" in request["response_format"]["json_schema"]["schema"]["properties"]
            output = {
                "markdown": "# Summary\n\nA real transcript.\n\n[Full transcript](transcript)",
                "used_image_ids": [], "evidence": [{"claim": "A real transcript.",
                                                      "source_quote": "**Speaker 1:** A real transcript."}],
            } if summary else {
                "markdown": "# Example\n\n## Opening\n\n**Speaker 1:** A real transcript.",
                "sections": [{"heading": "Opening", "first_cue": 1, "last_cue": 1}],
                "used_image_ids": [], "unresolved_references": [],
                "speaker_names": [{"speaker_id": "Speaker 1", "name": None,
                                   "confidence": "unknown", "evidence": []}],
            }
            return {"status": 200, "response": {"id": "text-" + str(len(requests)),
                    "usage": {"cost": .002, "total_tokens": 10}, "choices": [{
                        "finish_reason": "stop", "message": {"role": "assistant",
                                                              "content": json.dumps(output)}}]}}

        with patch("tubescribe.cloud_auth.workflow_send", side_effect=text), patch(
            "subprocess.run", side_effect=AssertionError("Cloud Functions must not run media commands")
        ):
            results = [advance(self.store, identifier, send=speech,
                               media_prepare=lambda *args: media) for _ in range(4)]
        self.assertEqual(results[-1], {"done": True, "state": "complete"})
        self.assertEqual(len(requests), 2)
        processed = self.store.stage_row("postprocessing", job["id"])
        summary = self.store.stage_row("artifact", job["id"], workflow="summary")
        self.assertEqual((processed["state"], summary["state"]), ("complete", "complete"))
        self.assertTrue(self.store.bundle_valid(processed["output_dir"], processed["manifest_sha256"]))
        self.assertTrue(self.store.bundle_valid(summary["output_dir"], summary["manifest_sha256"]))
        self.assertEqual(self.store.submission(identifier)["state"], "complete")


class BoundedAgent(unittest.TestCase):
    def test_occupied_capacity_suspends_without_holding_a_function_or_calling_a_model(self):
        from tubescribe.execution import ClaimBusy
        from tubescribe.inference import CapacityAgentRunner

        @contextmanager
        def busy(*args, **kwargs):
            self.assertFalse(kwargs["wait"])
            raise ClaimBusy("Synthetic occupied slot")
            yield

        underlying = Mock()
        runner = CapacityAgentRunner(underlying, Path("/tmp"), defer=True)
        with patch("tubescribe.execution.capacity", busy), self.assertRaises(DurableRunPending) as pending:
            runner.run({}, None)
        self.assertEqual(pending.exception.retry_after, 30)
        underlying.run.assert_not_called()

    def test_one_new_model_call_per_step_with_durable_tool_suspension(self):
        from tubescribe.inference import LocalTools, tool_handler

        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        store = Store(Path(temporary.name))
        self.addCleanup(store.db.close)
        ready = False

        def tool(_):
            if not ready:
                raise DurableRunPending("Synthetic media job running")
            return {"success": True, "contentItems": [{"type": "inputText", "text": "Ready"}]}

        tools = LocalTools(lambda token: tool_handler({"inspect": tool}, token))
        payload = {"instructions": "Inspect", "input": "Synthetic", "output_schema": {},
                   "tools": [{"type": "function", "function": {"name": "inspect", "parameters": {}}}],
                   "max_tool_calls": 2, "timeout_seconds": 30}
        sent = []

        def send(body):
            sent.append(body)
            message = {"role": "assistant", "tool_calls": [
                {"id": "tool1", "function": {"name": "inspect", "arguments": "{}"}}
            ]} if len(sent) == 1 else {"role": "assistant", "content": '{"text":"Complete"}'}
            return {"status": 200, "response": {"id": "generation-" + str(len(sent)),
                    "usage": {"cost": .001}, "choices": [{"finish_reason": "stop", "message": message}]}}

        for _ in range(2):
            with self.assertRaises(DurableRunPending):
                OpenRouterAgentRunner(store, "runs/test", send=send, max_new_calls=1).run(payload, tools)
        self.assertEqual(len(sent), 1)
        ready = True
        result = OpenRouterAgentRunner(store, "runs/test", send=send, max_new_calls=1).run(payload, tools)
        self.assertEqual(result["output"]["text"], "Complete")
        self.assertEqual(len(sent), 2)
        self.assertAlmostEqual(result["cost"], .002)


class CloudCredentials(unittest.TestCase):
    def test_clean_snapshot_source_has_an_explicit_python_and_no_credentials(self):
        from tubescribe.cloud_sandbox import start_box
        missing = RuntimeError("Synthetic absence")
        missing.status_code = 404
        box = Mock(create_process=AsyncMock(return_value=Mock(
            stdin=Mock(send=AsyncMock(), aclose=AsyncMock()))), stop=AsyncMock())
        with patch.dict(os.environ, {"TUBESCRIBE_MEDIA_SNAPSHOT": "snap_synthetic",
                                    "TUBESCRIBE_MEDIA_IMAGE": ""}), patch(
            "vercel.sandbox.get_sandbox", AsyncMock(side_effect=missing)
        ), patch("vercel.sandbox.create_sandbox", AsyncMock(return_value=box)) as create:
            asyncio.run(start_box("synthetic-media", {"operation": "prepare"}))
        self.assertEqual(create.call_args.kwargs["source"].snapshot_id, "snap_synthetic")
        self.assertNotIn("env", create.call_args.kwargs)
        self.assertFalse(create.call_args.kwargs["persistent"])
        self.assertEqual(box.create_process.call_args.args[0], "/app/.venv/bin/python")

    def test_explicit_session_mode_preserves_the_shared_capability_and_never_logs_in(self):
        from tubescribe.cloud_auth import launch
        client = Mock()
        environment = {"TUBESCRIBE_VAULT_AUTH": "session",
                       "INFISICAL_AGENT_VAULT_SESSION_TOKEN": "synthetic-dedicated-session",
                       "INFISICAL_AGENT_VAULT_PROXY_TOKEN": "synthetic-admin-capability",
                       "TUBESCRIBE_VAULT_CA_FINGERPRINT": "a" * 64}

        def run(command, **kwargs):
            self.assertIn("--keep-session", command)
            self.assertEqual(kwargs["env"]["INFISICAL_AGENT_VAULT_SESSION_TOKEN"],
                             "synthetic-dedicated-session")
            self.assertNotIn("INFISICAL_AGENT_VAULT_PROXY_TOKEN", kwargs["env"])
            return Mock(returncode=0, stdout=b'{"status":200}')

        self.assertEqual(launch(b"{}", env=environment, client=client, run=run), {"status": 200})
        client.post.assert_not_called()
        environment.pop("INFISICAL_AGENT_VAULT_SESSION_TOKEN")
        with self.assertRaisesRegex(ValueError, "dedicated TubeScribe session"):
            launch(b"{}", env=environment, client=client, run=run)
        client.post.assert_not_called()

    def test_sandbox_capabilities_stream_over_stdin_without_keys_or_environment_payloads(self):
        from tubescribe.cloud_sandbox import start_box

        missing = RuntimeError("Synthetic absence")
        missing.status_code = 404
        process = Mock(stdin=Mock(send=AsyncMock(), aclose=AsyncMock()))
        box = Mock(create_process=AsyncMock(return_value=process), stop=AsyncMock())
        request = {"parts": ["synthetic-object-capability" * 50 for _ in range(240)]}
        self.assertGreater(len(json.dumps(request)), 131072)
        with patch.dict(os.environ, {"TUBESCRIBE_MEDIA_IMAGE": "registry.example.test/media@sha256:" + "a" * 64}), patch(
            "vercel.sandbox.get_sandbox", AsyncMock(side_effect=missing)
        ), patch("vercel.sandbox.create_sandbox", AsyncMock(return_value=box)):
            asyncio.run(start_box("synthetic-media", request))
        self.assertNotIn("env", box.create_process.call_args.kwargs)
        self.assertEqual(json.loads(process.stdin.send.call_args.args[0]), request)
        process.stdin.aclose.assert_awaited_once()

    def test_native_client_sees_only_placeholder_and_ephemeral_proxy_capability(self):
        from tubescribe.cloud_auth import BUNDLE, INSTANCE, launch
        from tubescribe.openrouter import PLACEHOLDER

        client = Mock()

        def post(url, **kwargs):
            data = {"accessToken": "synthetic-machine-token"}
            if url.endswith("/agent-vault/sessions"):
                self.assertEqual(kwargs["json"]["accessBundle"], BUNDLE)
                self.assertEqual(kwargs["json"]["ttl"], "15m")
                data = {"session": {"id": "synthetic-session", "token": "synthetic-session-token"}}
            return httpx.Response(200, json=data, request=httpx.Request("POST", url))

        client.post.side_effect = post
        environment = {"PATH": "/usr/bin", "HOME": "/tmp", "VERCEL_OIDC_TOKEN": "synthetic-jwt",
                       "TUBESCRIBE_INFISICAL_IDENTITY_ID": "00000000-0000-4000-8000-000000000001",
                       "TUBESCRIBE_VAULT_CA_FINGERPRINT": "a" * 64,
                       "INFISICAL_AGENT_VAULT_PROXY_TOKEN": "synthetic-private-proxy",
                       "AWS_SECRET_ACCESS_KEY": "synthetic-storage-key"}

        def run(command, **kwargs):
            child = kwargs["env"]
            self.assertEqual(child["OPENROUTER_API_KEY"], PLACEHOLDER)
            self.assertEqual(child["INFISICAL_AGENT_VAULT_SESSION_TOKEN"], "synthetic-session-token")
            for forbidden in ("VERCEL_OIDC_TOKEN", "AWS_SECRET_ACCESS_KEY",
                              "INFISICAL_AGENT_VAULT_PROXY_TOKEN", "TUBESCRIBE_INFISICAL_IDENTITY_ID"):
                self.assertNotIn(forbidden, child)
            self.assertNotIn("synthetic-session-token", " ".join(command))
            self.assertIn("--domain=" + INSTANCE, command)
            return Mock(returncode=0, stdout=b'{"status":200}')

        self.assertEqual(launch(b"{}", env=environment, client=client, run=run), {"status": 200})
        self.assertTrue(client.post.call_args_list[-1].args[0].endswith("/synthetic-session/revoke"))


class DiscoveryBatch(unittest.TestCase):
    def test_feed_poll_batches_channels_with_bounded_parallelism(self):
        from tubescribe import workflows

        active = peak = 0
        checked = []

        async def execute(operation, *arguments):
            nonlocal active, peak
            if operation == "discovery_owner":
                return "current"
            if operation == "channels":
                return ["channel-" + str(i) for i in range(19)]
            if operation == "discover":
                active += 1
                peak = max(active, peak)
                await asyncio.sleep(.001)
                checked.append(arguments[0])
                active -= 1
                return {"discovered": 1}

        with patch.object(workflows, "execute", execute), patch.object(
            workflows, "reconcile", AsyncMock()
        ), patch.object(workflows.workflow, "get_workflow_metadata", return_value=Mock(run_id="synthetic")):
            result = asyncio.run(workflows.discover_batch.func())
        self.assertEqual((len(checked), result["discovered"]), (19, 19))
        self.assertLessEqual(peak, 4)


class NativeWorkflow(unittest.TestCase):
    def test_registered_workflow_runs_through_the_native_sdk_and_releases_between_steps(self):
        # A fresh interpreter isolates the SDK's process-wide Local World from
        # other tests. No service credentials, media VM or model call is used.
        with tempfile.TemporaryDirectory() as directory:
            environment = {key: value for key, value in os.environ.items()
                           if not key.startswith(("TUBESCRIBE_", "VERCEL_", "WORKFLOW_"))}
            environment.update(WORKFLOW_TARGET_WORLD="local", WORKFLOW_LOCAL_DATA_DIR=directory)
            result = subprocess.run([sys.executable, "-c", '''
import asyncio
from unittest.mock import patch
from vercel import workflow
from vercel.workflow._internal import world
from tubescribe.workflows import process_selection
async def main():
    try:
        states = [{'done':False,'delay':0}, {'done':True,'state':'superseded'}]
        with patch('tubescribe.workflow_runtime.with_store', side_effect=states) as execute:
            run = await workflow.start(process_selection, 'synthetic-selected-job')
            result = await asyncio.wait_for(run.return_value(), 45)
            assert result == {'done':True,'state':'superseded'}, result
            assert execute.call_count == 2, execute.call_count
            print('native-sdk-complete')
    finally:
        await world.get_world().aclose()
asyncio.run(main())
'''], env=environment, capture_output=True, timeout=60, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("native-sdk-complete", result.stdout)
