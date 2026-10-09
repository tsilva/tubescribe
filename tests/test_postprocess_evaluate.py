"""Offline evaluations must catch tampering and report failed cases without hiding them."""

import importlib.util
import json
import tempfile
import unittest
import zipfile
from pathlib import Path

spec = importlib.util.spec_from_file_location(
    "postprocess_evaluate", Path(__file__).parents[1] / "scripts" / "postprocess-evaluate.py"
)
evaluation = importlib.util.module_from_spec(spec)
spec.loader.exec_module(evaluation)


class EvaluationTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.source = self.root / "transcript.srt"
        self.source.write_text(
            "1\n00:00:00,000 --> 00:00:02,000\nSpeaker 1: Hello there.\n\n"
            "2\n00:00:02,000 --> 00:00:04,000\nSpeaker 2: Good morning.\n"
        )
        self.run = self.root / "run"
        self.run.mkdir()
        self.response = {
            "elapsed_seconds": 3,
            "output": {
                "markdown": "## Conversation\n**Speaker 1:** Hello there.\n**Speaker 2:** Good morning.",
                "sections": [{"heading": "Conversation", "first_cue": 1, "last_cue": 2}],
                "used_image_ids": [],
                "unresolved_references": [],
                "speaker_names": [
                    {
                        "speaker_id": f"Speaker {i}",
                        "confidence": "unknown",
                        "name": None,
                        "evidence": [],
                    }
                    for i in (1, 2)
                ],
            },
        }
        self.write("response.json", self.response)
        self.write("resources.json", {})
        self.write("speaker_names.json", self.response["output"]["speaker_names"])
        (self.run / "processed.md").write_text(self.response["output"]["markdown"] + "\n")
        (self.run / "preview.html").write_text("<p>Hello there.</p>")
        self.report = {
            "source": str(self.source),
            "source_sha256": evaluation.sha256(self.source),
            "srt_sha256": evaluation.sha256(self.source),
            "duration": 4,
            "elapsed_seconds": 5,
            "tool_calls": 0,
            "figures": 0,
            "usage": {
                "total": {
                    "inputTokens": 300,
                    "cachedInputTokens": 100,
                    "totalTokens": 400,
                    "outputTokens": 100,
                }
            },
        }
        self.write("report.json", self.report)
        self.package()

    def write(self, name, data):
        (self.run / name).write_text(json.dumps(data))

    def package(self):
        with zipfile.ZipFile(self.run / "processed-with-figures.zip", "w") as archive:
            for name in (
                "processed.md",
                "preview.html",
                "report.json",
                "resources.json",
                "speaker_names.json",
            ):
                archive.write(self.run / name, name)

    def test_valid_audit_preserves_metrics_and_distinguishes_queue_time(self):
        result = evaluation.audit(self.run)
        self.assertEqual(result["status"], "pass")
        self.assertEqual(result["speaker_turns"], 2)
        self.assertEqual(result["uncached_input_tokens"], 200)
        self.assertEqual(result["queue_and_transport_seconds"], 2)

    def test_source_modified_after_run_fails(self):
        self.source.write_text(self.source.read_text().replace("Hello", "Goodbye"))
        with self.assertRaisesRegex(ValueError, "hash changed"):
            evaluation.audit(self.run)

    def test_markdown_or_download_tampering_fails(self):
        (self.run / "processed.md").write_text("A shortened summary.")
        with self.assertRaisesRegex(ValueError, "Markdown differs"):
            evaluation.audit(self.run)
        (self.run / "processed.md").write_text(self.response["output"]["markdown"] + "\n")
        (self.run / "preview.html").write_text("Modified after packaging")
        with self.assertRaisesRegex(ValueError, "package differs"):
            evaluation.audit(self.run)

    def test_claimed_complete_coverage_cannot_hide_deleted_voice(self):
        self.response["output"]["markdown"] = "## Conversation\n**Speaker 1:** Hello."
        self.write("response.json", self.response)
        (self.run / "processed.md").write_text(self.response["output"]["markdown"] + "\n")
        self.package()
        with self.assertRaisesRegex(ValueError, "missing a diarized speaker"):
            evaluation.audit(self.run)

    def test_unissued_identity_frame_and_path_escape_fail(self):
        self.response["output"]["speaker_names"][0]["evidence"] = [
            {"source": "video_frame", "image_id": "guessed", "text": "A name card."}
        ]
        self.write("response.json", self.response)
        with self.assertRaisesRegex(ValueError, "evidence IDs"):
            evaluation.audit(self.run)
        with self.assertRaisesRegex(ValueError, "escapes"):
            evaluation.resource_path(self.run, "../transcript.srt")

    def test_failed_runs_remain_in_published_report_and_inputs_are_deduplicated(self):
        summary = evaluation.publish(
            [self.run, self.run, self.root / "missing"], [], self.root / "report"
        )
        self.assertEqual((summary["passed"], summary["failed"]), (2, 1))
        self.assertEqual(summary["unique_input_transcripts"], 1)
        self.assertIn("FAIL", (self.root / "report" / "index.html").read_text())
        self.assertEqual(len(summary["runs"]), 3)

    def test_comparison_rejects_different_source_and_reports_regression(self):
        other = self.root / "other"
        other.mkdir()
        report = dict(self.report, elapsed_seconds=6)
        report["usage"] = {"total": {"inputTokens": 400, "totalTokens": 500, "outputTokens": 100}}
        (other / "report.json").write_text(json.dumps(report))
        (other / "response.json").write_text(json.dumps(dict(self.response, elapsed_seconds=4)))
        pair = evaluation.compare(self.run, other)
        self.assertEqual(pair["reduction_percent"]["total_tokens"], -25)
        report["srt_sha256"] = "different"
        (other / "report.json").write_text(json.dumps(report))
        with self.assertRaisesRegex(ValueError, "same original SRT"):
            evaluation.compare(self.run, other)


if __name__ == "__main__":
    unittest.main()
