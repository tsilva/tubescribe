"""Synthetic media provider: durable submission, timeout recovery and receipts."""

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import httpx

from tubescribe.agent_gateway import DurableRunPending
from tubescribe.apify_client import ACTOR, MAX_COST, perform, stored_file
from tubescribe.cloud_download import acquire, download_metrics
from tubescribe.pipeline_details import billing_totals
from tubescribe.storage import Store

RUN = "abcdefghijklmnopq"
KVS = "rstuvwxyzABCDEFGH"
VIDEO = "abcdefghijk"
FILE = f"https://api.apify.com/v2/key-value-stores/{KVS}/records/video.mp4"


def run(status="RUNNING", count=0, cost=0):
    return {"id": RUN, "actId": ACTOR, "status": status, "defaultKeyValueStoreId": KVS,
            "defaultDatasetId": RUN, "finishedAt": f"terminal-{count}",
            "stats": {"resurrectCount": count}, "usageTotalUsd": cost}


class DownloadRecovery(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.store = Store(Path(temporary.name))
        self.addCleanup(self.store.db.close)
        self.job = {"channel_id": "channel", "video_id": VIDEO, "profile": "profile"}
        self.key = f"transcripts/channel/{VIDEO}/profile/cloud-media/apify.json"

    def pending(self, send):
        with self.assertRaises(DurableRunPending):
            acquire(self.store, self.job, send=send)

    def test_timeout_resumes_same_run_then_reuses_saved_file_and_cumulative_cost(self):
        send = Mock(side_effect=[
            {"status": 200, "run": run()},
            {"status": 200, "run": run("TIMED-OUT")},
            {"status": 200, "file": None},
            {"status": 200, "run": run(count=1)},
            {"status": 200, "run": run("SUCCEEDED", 1, .372)},
            {"status": 200, "file": FILE},
        ])
        self.pending(send)
        first = download_metrics(json.loads(self.store.read_key(self.key)))
        self.pending(send)
        result = acquire(self.store, self.job, send=send)
        self.assertEqual(acquire(self.store, self.job, send=Mock(side_effect=AssertionError)), result)
        operations = [call.args[0]["operation"] for call in send.call_args_list]
        self.assertEqual(operations, ["start", "status", "output", "resurrect", "status", "output"])
        self.assertEqual(send.call_args_list[3].args[0]["run_id"], RUN)
        costs, _ = billing_totals([first, download_metrics(result)])
        self.assertEqual(costs, [.372])

    def test_lost_start_ack_reconciles_input_nonce_without_second_start(self):
        send = Mock(return_value={"status": 0})
        self.pending(send)
        intent = json.loads(self.store.read_key(self.key))
        send.side_effect = [{"status": 200, "run": run()}, {"status": 200, "run": run()}]
        self.pending(send)
        self.assertEqual(send.call_args_list[1].args[0]["operation"], "recover")
        self.assertEqual(send.call_args_list[1].args[0]["nonce"], intent["nonce"])
        self.assertEqual(sum(c.args[0]["operation"] == "start" for c in send.call_args_list), 1)

    def test_lost_resume_ack_waits_for_status_and_does_not_repeat_resume(self):
        send = Mock(side_effect=[{"status": 200, "run": run()},
                                 {"status": 200, "run": run("TIMED-OUT")},
                                 {"status": 200, "file": None}, {"status": 0},
                                 {"status": 200, "run": run("TIMED-OUT")},
                                 {"status": 200, "file": None}])
        self.pending(send)
        self.pending(send)
        self.pending(send)
        self.assertEqual(sum(c.args[0]["operation"] == "resurrect" for c in send.call_args_list), 1)

    def test_saved_output_after_timeout_skips_resume(self):
        send = Mock(side_effect=[{"status": 200, "run": run()},
                                 {"status": 200, "run": run("TIMED-OUT")},
                                 {"status": 200, "file": FILE}])
        self.pending(send)
        self.assertEqual(acquire(self.store, self.job, send=send)["file"], FILE)
        self.assertNotIn("resurrect", [c.args[0]["operation"] for c in send.call_args_list])

    def test_known_capacity_rejection_can_retry_but_ambiguous_start_cannot(self):
        send = Mock(side_effect=[{"status": 429}, {"status": 200, "run": run()}])
        self.pending(send)
        self.pending(send)
        self.assertEqual([c.args[0]["operation"] for c in send.call_args_list], ["start", "start"])

    def test_resume_budget_and_missing_output_fail_without_new_download(self):
        send = Mock(side_effect=[{"status": 200, "run": run()},
                                 {"status": 200, "run": run("TIMED-OUT", 2, .4)},
                                 {"status": 200, "file": None}])
        self.pending(send)
        with self.assertRaisesRegex(ValueError, "stopped"):
            acquire(self.store, self.job, send=send)
        self.assertEqual(len(send.call_args_list), 3)

    def test_fresh_manual_retry_replaces_only_confirmed_zero_cost_failed_run(self):
        import time

        from tubescribe.execution import CURRENT_SUBMISSION

        send = Mock(return_value={"status": 200, "run": run()})
        self.pending(send)
        old = json.loads(self.store.read_key(self.key))
        old["created"] -= 60
        self.store.blobs.put(self.key, json.dumps(old).encode())
        token = CURRENT_SUBMISSION.set("new-manual-selection")
        try:
            with patch.object(self.store, "submission", return_value={"created": time.time() - 30}):
                send.return_value = {"status": 200, "run": run("FAILED", cost=0)}
                self.pending(send)
                self.assertTrue(self.store.exists(self.key.removesuffix(".json") + "/" + old["nonce"] + ".json"))
                send.return_value = {"status": 200, "run": run()}
                self.pending(send)
                self.assertNotEqual(json.loads(self.store.read_key(self.key))["nonce"], old["nonce"])
                send.return_value = {"status": 200, "run": run("FAILED", cost=0)}
                with self.assertRaisesRegex(ValueError, "stopped"):
                    acquire(self.store, self.job, send=send)
        finally:
            CURRENT_SUBMISSION.reset(token)


class MediaClient(unittest.TestCase):
    def test_start_pins_actor_build_limits_and_disables_transcription(self):
        def reply(request):
            self.assertEqual(request.url.path, f"/v2/actors/{ACTOR}/runs")
            self.assertEqual(float(request.url.params["maxTotalChargeUsd"]), MAX_COST)
            self.assertEqual(request.url.params["build"], "0.0.119")
            self.assertEqual(request.url.params["forcePermissionLevel"], "LIMITED_PERMISSIONS")
            body = json.loads(request.content)
            self.assertEqual(body["transcriptionAndSubtitle"], "NONE")
            self.assertEqual(body["tubescribeRequestId"], "a" * 32)
            return httpx.Response(201, json={"data": run()})

        with httpx.Client(transport=httpx.MockTransport(reply)) as client:
            result = perform({"operation": "start", "video_id": VIDEO, "nonce": "a" * 32}, client)
        self.assertEqual(result["run"]["id"], RUN)

    def test_output_never_returns_raw_youtube_streams_or_wrong_video(self):
        def reply(request):
            if "/actor-runs/" in request.url.path:
                value = run("SUCCEEDED")
                value["defaultKeyValueStoreId"] = "separateINPUTstor"
                return httpx.Response(200, json={"data": value})
            return httpx.Response(200, json=[{"id": VIDEO, "input": "https://www.youtube.com/watch?v=" + VIDEO,
                "downloadedFileUrl": FILE, "videoOnlyUrl": "https://secret.googlevideo.com/signed"}])

        with httpx.Client(transport=httpx.MockTransport(reply)) as client:
            result = perform({"operation": "output", "run_id": RUN, "video_id": VIDEO}, client)
        self.assertEqual(result["file"], FILE)
        self.assertNotIn("googlevideo", json.dumps(result))
        for url in ["http://api.apify.com/v2/key-value-stores/" + KVS + "/records/file",
                    FILE + "?token=secret", FILE.replace(KVS, RUN),
                    "https://localhost/private", FILE.replace("api.apify.com", "api.apify.com.evil.test")]:
            with self.assertRaises(ValueError):
                stored_file(url, KVS)

    def test_changed_resume_marker_never_resurrects_another_finished_attempt(self):
        requests = []

        def reply(request):
            requests.append(request)
            return httpx.Response(200, json={"data": run("TIMED-OUT", 1)})

        with httpx.Client(transport=httpx.MockTransport(reply)) as client:
            perform({"operation": "resurrect", "run_id": RUN, "count": 0,
                     "finished_at": "terminal-0"}, client)
        self.assertEqual([r.method for r in requests], ["GET"])

    def test_apify_capability_is_isolated_from_model_and_database_access(self):
        from tubescribe.apify_client import PLACEHOLDER
        from tubescribe.cloud_auth import launch

        environment = {"TUBESCRIBE_VAULT_AUTH": "session", "TUBESCRIBE_VAULT_CA_FINGERPRINT": "ab" * 32,
                       "TUBESCRIBE_APIFY_VAULT_SESSION_TOKEN": "synthetic-media-session",
                       "INFISICAL_AGENT_VAULT_SESSION_TOKEN": "synthetic-model-session",
                       "TUBESCRIBE_DATABASE_URL": "synthetic-private-db"}

        def child(command, **kwargs):
            self.assertEqual(command[-1], "tubescribe.apify_client")
            self.assertEqual(kwargs["env"]["APIFY_TOKEN"], PLACEHOLDER)
            self.assertEqual(kwargs["env"]["INFISICAL_AGENT_VAULT_SESSION_TOKEN"], "synthetic-media-session")
            self.assertNotIn("OPENROUTER_API_KEY", kwargs["env"])
            self.assertNotIn("TUBESCRIBE_DATABASE_URL", kwargs["env"])
            return Mock(returncode=0, stdout=b'{"status":200}', stderr=b"")

        with patch("httpx.Client"):
            self.assertEqual(launch(b"{}", service="apify", env=environment, run=child), {"status": 200})
