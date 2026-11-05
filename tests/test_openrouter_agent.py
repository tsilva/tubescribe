import json
import tempfile
import unittest
from pathlib import Path

from tubescribe.billing import tracking
from tubescribe.inference import (
    LocalTools,
    PostprocessingUnavailable,
    configured_runner,
    tool_handler,
)
from tubescribe.openrouter_agent import MODEL, OpenRouterAgentRunner
from tubescribe.pipeline_details import details
from tubescribe.storage import Store


class OpenRouterAgents(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.store = Store(Path(temporary.name))
        self.addCleanup(self.store.db.close)
        self.store.channel({"id": "channel", "title": "Example", "url": "https://example.test"})
        self.store.index(
            "channel",
            "upload",
            [{"id": "video", "title": "Example", "url": "https://example.test/video"}],
            "mai-openrouter-v1",
        )
        self.job = self.store.db.execute("SELECT id FROM jobs").fetchone()[0]
        self.payload = {
            "instructions": "Use only the supplied inspection tool. Return JSON.",
            "input": "Synthetic transcript",
            "output_schema": {
                "type": "object",
                "properties": {"text": {"type": "string"}},
                "required": ["text"],
                "additionalProperties": False,
            },
            "tools": [
                {
                    "type": "function",
                    "function": {
                        "name": "inspect",
                        "parameters": {
                            "type": "object",
                            "properties": {},
                            "additionalProperties": False,
                        },
                    },
                }
            ],
            "max_tool_calls": 2,
            "timeout_seconds": 30,
        }

    def tools(self):
        return LocalTools(
            lambda token: tool_handler(
                {
                    "inspect": lambda args: {
                        "success": True,
                        "contentItems": [
                            {"type": "inputText", "text": '{"image_id":"frame-1"}'},
                            {
                                "type": "inputImage",
                                "imageUrl": "data:image/png;base64,c3ludGhldGlj",
                            },
                        ],
                    }
                },
                token,
            )
        )

    def response(self, identifier, cost, message, tokens=10):
        return {
            "status": 200,
            "response": {
                "id": identifier,
                "model": MODEL,
                "usage": {
                    "cost": cost,
                    "total_tokens": tokens,
                    "prompt_tokens": tokens - 2,
                    "completion_tokens": 2,
                },
                "choices": [{"finish_reason": "stop", "message": message}],
            },
        }

    def test_hosted_profiles_choose_openrouter_without_a_bridge_or_codex_fallback(self):
        runner = configured_runner(self.store, None, "runs/test", profile="mai-openrouter-v1")
        self.assertIsInstance(runner.runner, OpenRouterAgentRunner)
        self.assertEqual(runner.model, MODEL)

    def test_tools_images_billing_and_cached_recovery(self):
        sent = []

        def send(body):
            sent.append(json.loads(json.dumps(body)))
            if len(sent) == 1:
                return self.response(
                    "generation-1",
                    0.01,
                    {
                        "role": "assistant",
                        "content": None,
                        "reasoning_details": [{"type": "reasoning.text", "text": "Inspect"}],
                        "tool_calls": [
                            {
                                "id": "tool-1",
                                "type": "function",
                                "function": {"name": "inspect", "arguments": "{}"},
                            }
                        ],
                    },
                )
            return self.response(
                "generation-2", 0.02, {"role": "assistant", "content": '{"text":"Complete"}'}, 20
            )

        runner = OpenRouterAgentRunner(self.store, "runs/test", send=send)
        for _ in range(2):
            attempt = self.store.begin_attempt(self.job, "process_one")
            with tracking(self.store, attempt):
                result = runner.run(self.payload, self.tools())
            self.store.finish_attempt(attempt, "complete", metrics={"cost": result["cost"]})
        self.assertEqual(len(sent), 2)
        self.assertAlmostEqual(result["cost"], 0.03)
        self.assertTrue(result["cost_complete"])
        self.assertEqual(result["usage"]["total"]["totalTokens"], 30)
        self.assertEqual(result["tool_calls"][0]["image_count"], 1)
        self.assertEqual(sent[1]["messages"][2]["reasoning_details"][0]["text"], "Inspect")
        self.assertEqual(sent[1]["messages"][-1]["content"][-1]["type"], "image_url")
        self.assertTrue(sent[0]["response_format"]["json_schema"]["strict"])
        self.assertAlmostEqual(details(self.store.root, "video")["cost_usd"], 0.03)
        self.assertEqual(details(self.store.root, "video")["steps"][3]["total_tokens"], 30)

    def test_charge_survives_a_rejected_final_result(self):
        runner = OpenRouterAgentRunner(
            self.store,
            "runs/bad",
            send=lambda body: self.response(
                "billed-invalid", 0.04, {"role": "assistant", "content": "not JSON"}
            ),
        )
        attempt = self.store.begin_attempt(self.job, "process_one")
        with tracking(self.store, attempt), self.assertRaises(ValueError):
            runner.run(self.payload, self.tools())
        self.store.finish_attempt(attempt, "failed")
        self.assertAlmostEqual(details(self.store.root, "video")["cost_usd"], 0.04)

    def test_ambiguous_request_is_not_reissued(self):
        sent = []

        def send(body):
            sent.append(body)
            raise OSError("Synthetic timeout")

        runner = OpenRouterAgentRunner(self.store, "runs/pending", send=send)
        for _ in range(2):
            attempt = self.store.begin_attempt(self.job, "process_one")
            with tracking(self.store, attempt), self.assertRaises(PostprocessingUnavailable):
                runner.run(self.payload, self.tools())
            self.store.finish_attempt(attempt, "failed")
        self.assertEqual(len(sent), 1)
        result = details(self.store.root, "video")
        self.assertIsNone(result["cost_usd"])
        self.assertFalse(result["steps"][3]["cost_complete"])

    def test_tool_budget_and_allowlist_are_enforced_before_execution(self):
        runner = OpenRouterAgentRunner(
            self.store,
            "runs/invalid-tool",
            send=lambda body: self.response(
                "billed-tool",
                0.01,
                {
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [{"id": "bad", "function": {"name": "shell", "arguments": "{}"}}],
                },
            ),
        )
        with self.assertRaisesRegex(ValueError, "undeclared tool"):
            runner.run(self.payload, self.tools())

    def test_rate_limited_requests_can_retry_without_losing_the_later_charge(self):
        attempts = []

        def send(body):
            attempts.append(body)
            if len(attempts) == 1:
                return {"status": 429}
            return self.response(
                "retried-generation", 0.05, {"role": "assistant", "content": '{"text":"Complete"}'}
            )

        runner = OpenRouterAgentRunner(self.store, "runs/limited", send=send)
        attempt = self.store.begin_attempt(self.job, "process_one")
        with tracking(self.store, attempt), self.assertRaises(PostprocessingUnavailable):
            runner.run(self.payload, self.tools())
        self.store.finish_attempt(attempt, "failed")
        attempt = self.store.begin_attempt(self.job, "process_one")
        with tracking(self.store, attempt):
            runner.run(self.payload, self.tools())
        self.store.finish_attempt(attempt, "complete")
        result = details(self.store.root, "video")
        self.assertAlmostEqual(result["cost_usd"], 0.05)
        self.assertTrue(result["steps"][3]["cost_complete"])
