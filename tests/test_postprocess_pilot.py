"""Post-processing must resolve only captured images and preserve source coverage."""

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from tubescribe import postprocessing as pilot


class PostprocessPilotTests(unittest.TestCase):
    def output(self):
        return {
            "output": {
                "markdown": "## Opening\nSpeaker 1: Discussion.\n"
                "![Chart](image:frame_0001000)\n"
                "## Interruption\nSpeaker 2: [Untranscribed speech detected.]",
                "used_image_ids": ["frame_0001000"],
                "unresolved_references": [],
                "sections": [
                    {"heading": "Opening", "first_cue": 1, "last_cue": 2},
                    {"heading": "Interruption", "first_cue": 3, "last_cue": 3},
                ],
            }
        }

    def test_stitch_only_issued_images(self):
        result = self.output()
        assets = {"frame_0001000": {"path": "figures/frame_0001000.png"}}
        markdown, report = pilot.stitch(
            result, assets, 3, annotations=["[Untranscribed speech detected.]"]
        )
        self.assertIn("](" + assets["frame_0001000"]["path"] + ")", markdown)
        self.assertEqual(report["figures"], 1)
        with self.assertRaisesRegex(ValueError, "unissued"):
            pilot.stitch(result, {}, 3)

    def test_missing_cue_or_annotation_is_rejected(self):
        assets = {"frame_0001000": {"path": "figures/frame_0001000.png"}}
        result = self.output()
        result["output"]["sections"][1]["first_cue"] = 4
        with self.assertRaisesRegex(ValueError, "gap"):
            pilot.stitch(result, assets, 3, annotations=["[Untranscribed speech detected.]"])
        result = self.output()
        result["output"]["markdown"] = result["output"]["markdown"].replace(
            "[Untranscribed speech detected.]", ""
        )
        with self.assertRaisesRegex(ValueError, "annotation"):
            pilot.stitch(result, assets, 3, annotations=["[Untranscribed speech detected.]"])

    def test_out_of_range_timestamp_rejected_before_extraction(self):
        screenshots = pilot.Screenshots.__new__(pilot.Screenshots)
        screenshots.duration = 20
        for timestamp in (-1, 20, float("nan"), True):
            with self.assertRaisesRegex(ValueError, "Timestamp"):
                screenshots.execute({"timestamps": [timestamp], "resolution": "full", "crop": None})
        with self.assertRaisesRegex(ValueError, "outside"):
            screenshots.execute({"timestamps": [1], "resolution": "full", "crop": [0.9, 0, 0.2, 1]})


class ScreenshotEfficiencyTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.output = Path(self.directory.name)
        with patch.object(
            pilot.subprocess,
            "check_output",
            return_value=b'{"streams":[{"width":1920,"height":1030}]}',
        ):
            self.screenshots = pilot.Screenshots(
                self.output / "source.mp4",
                self.output,
                20,
                {"frames": 48, "details": 18, "search_samples": 126},
            )
        patch.object(
            pilot.Screenshots,
            "fingerprint",
            side_effect=lambda path, crop=None: (
                bytes([int(path.stem.split("_")[1]) // 1000 * 10 + (100 if crop else 0)])
                * (96 * 54)
            ),
        ).start()
        self.run = patch.object(pilot.subprocess, "run", side_effect=self.extract).start()
        self.addCleanup(patch.stopall)
        self.encode = patch.object(
            pilot.subprocess, "check_output", return_value=b"encoded image"
        ).start()

    def extract(self, command, **_kwargs):
        Path(command[-1]).write_bytes(b"original frame")

    def request(self, timestamps, resolution="preview", crop=None):
        return self.screenshots.execute(
            {"timestamps": timestamps, "resolution": resolution, "crop": crop}
        )

    def images(self, result):
        return [item for item in result["contentItems"] if item["type"] == "inputImage"]

    def test_batch_is_one_image_and_repeat_does_not_resend(self):
        self.assertEqual(len(self.images(self.request([1, 2, 3]))), 1)
        self.assertEqual(len(self.images(self.request([1, 2, 3]))), 0)
        self.assertEqual(len(self.images(self.request([2, 3, 4]))), 1)
        self.assertEqual(self.run.call_count, 4)
        self.assertEqual(self.screenshots.calls[-1]["deduplicated_views"], 2)

    def test_failed_encoding_does_not_issue_captured_frame_ids(self):
        self.encode.side_effect = RuntimeError("Encoder failed")
        with self.assertRaisesRegex(RuntimeError, "Encoder failed"):
            self.request([1])
        self.assertEqual(len(self.screenshots.assets), 1)
        self.assertEqual(self.screenshots.issued_assets, {})
        self.encode.side_effect = None
        self.request([1])
        self.assertEqual(set(self.screenshots.issued_assets), {"frame_0001000"})

    def test_retry_after_encoding_failure_still_delivers_the_image(self):
        self.encode.side_effect = RuntimeError("Encoder failed")
        with self.assertRaises(RuntimeError):
            self.request([1])
        self.encode.side_effect = None
        self.assertEqual(len(self.images(self.request([1]))), 1)
        self.assertEqual(self.run.call_count, 1)
        self.assertEqual(self.screenshots.calls[-1]["deduplicated_views"], 0)

    def test_detail_upgrade_and_crop_keep_canonical_full_frame(self):
        self.request([1])
        detail = self.request([1], "full", [0, 0, 0.5, 1])
        self.assertEqual(len(self.images(detail)), 1)
        self.assertEqual(list(self.screenshots.assets), ["frame_0001000"])
        self.assertIsNone(self.screenshots.assets["frame_0001000"]["crop"])
        self.assertEqual(
            (self.output / "figures/frame_0001000.png").read_bytes(), b"original frame"
        )
        self.assertEqual(self.screenshots.full_views, 1)
        self.assertEqual(len(self.images(self.request([1], "full", [0, 0, 0.5, 1]))), 0)
        self.assertEqual(len(self.images(self.request([1], "full"))), 1)

    def test_detail_budget_rejects_before_extraction(self):
        self.screenshots.full_views = 17
        with self.assertRaisesRegex(ValueError, "18-detail"):
            self.request([1, 2], "full")
        self.run.assert_not_called()
        self.assertFalse(self.screenshots.assets)

    def test_broad_preview_crop_reuses_whole_frame(self):
        self.request([1])
        self.assertEqual(len(self.images(self.request([1], crop=[0, 0.08, 1, 0.85]))), 0)
        self.assertEqual(self.screenshots.calls[-1]["deduplicated_views"], 1)
        self.assertEqual(len(self.images(self.request([1], crop=[0, 0, 0.3, 0.5]))), 1)

    def test_similar_preview_can_upgrade_to_own_detail(self):
        with patch.object(pilot.Screenshots, "fingerprint", return_value=bytes(96 * 54)):
            self.request([1])
            result = self.request([2])
        self.assertEqual(len(self.images(result)), 0)
        self.assertIn('"similar_preview_to": "frame_0001000"', result["contentItems"][0]["text"])
        self.assertEqual(len(self.images(self.request([2], "full"))), 1)
        self.assertIn("frame_0002000", self.screenshots.assets)

    def test_numeric_equivalent_detail_crops_do_not_resend(self):
        self.request([1], "full", [0, 0, 0.5, 1])
        self.assertEqual(len(self.images(self.request([1], "full", [0.0, 0.0, 0.5, 1.0]))), 0)

    def test_search_returns_distinct_candidates_with_bounded_sampling(self):
        raw = b"".join(bytes([v]) * (96 * 54) for v in [0, 0, 80, 80, 180])
        self.encode.return_value = raw
        result = self.screenshots.search({"timestamps": [10], "radius": 5})
        self.assertEqual(len(self.images(result)), 1)
        self.assertEqual(len(self.screenshots.assets), 3)
        self.assertEqual(self.screenshots.search_samples, 5)
        self.assertTrue(all(5 <= a["timestamp"] < 15 for a in self.screenshots.assets.values()))
        self.screenshots.search_samples = 126
        with self.assertRaisesRegex(ValueError, "sample budget"):
            self.screenshots.search({"timestamps": [10], "radius": 5})


class TranscriptAndSpeakerTests(unittest.TestCase):
    def srt(self):
        return "1\n00:00:00,000 --> 00:00:01,000\nSpeaker 1: My name is Alice.\n\n2\n00:00:02,000 --> 00:00:03,000\nSpeaker 2: Look at this chart.\n"

    def test_full_transcript_delivered_once_without_loss(self):
        transcript = pilot.Transcript(self.srt())
        self.assertEqual(transcript.execute({})["contentItems"][0]["text"], self.srt())
        self.assertNotEqual(transcript.execute({})["contentItems"][0]["text"], self.srt())
        self.assertEqual(transcript.reads, 1)
        self.assertEqual(len(pilot.srt_cues(self.srt())), 2)
        self.assertIn("Alice", str(pilot.visual_index(self.srt())))
        with self.assertRaises(ValueError):
            pilot.srt_cues(self.srt().replace("2\n00", "3\n00"))

    def mapping(self):
        return [
            {
                "speaker_id": "Speaker 1",
                "name": "Alice",
                "confidence": "confirmed",
                "evidence": [
                    {
                        "source": "transcript",
                        "cue": 1,
                        "image_id": None,
                        "text": "Self introduction as Alice",
                    }
                ],
            },
            {
                "speaker_id": "Speaker 2",
                "name": "Bob",
                "confidence": "probable",
                "evidence": [
                    {
                        "source": "video_frame",
                        "cue": None,
                        "image_id": "frame_0002000",
                        "text": "Name card; speaker connection uncertain",
                    }
                ],
            },
        ]

    def test_only_confirmed_speaker_names_fill_labels(self):
        md = pilot.fill_speaker_names(
            "**Speaker 1:** Hello. **Speaker 2:** Hi.",
            self.mapping(),
            {"frame_0002000": {}},
            2,
            ["Speaker 1", "Speaker 2"],
        )
        self.assertIn("**Alice (Speaker 1):**", md)
        self.assertIn("**Speaker 2:**", md)
        self.assertNotIn("Bob", md)

    def test_identity_needs_evidence_and_valid_references(self):
        mapping = self.mapping()
        mapping[0]["evidence"] = []
        with self.assertRaisesRegex(ValueError, "evidence"):
            pilot.fill_speaker_names(
                "", mapping, {"frame_0002000": {}}, 2, ["Speaker 1", "Speaker 2"]
            )
        with self.assertRaisesRegex(ValueError, "unissued"):
            pilot.fill_speaker_names("", self.mapping(), {}, 2, ["Speaker 1", "Speaker 2"])
        with self.assertRaisesRegex(ValueError, "exactly"):
            pilot.fill_speaker_names("", self.mapping(), {}, 2, ["Speaker 1"])

    def test_fingerprint_preserves_all_corners_and_detects_slide_changes(self):
        a = bytearray(96 * 54)
        b = bytearray(a)
        b[0 : 10 * 96] = bytes([255]) * (10 * 96)
        self.assertFalse(
            pilot.Screenshots.similar(
                pilot.Screenshots.mask_fingerprint(a), pilot.Screenshots.mask_fingerprint(b)
            )
        )
        b[20 * 96 : 40 * 96] = bytes([100]) * (20 * 96)
        self.assertFalse(
            pilot.Screenshots.similar(
                pilot.Screenshots.mask_fingerprint(a), pilot.Screenshots.mask_fingerprint(b)
            )
        )

    def test_full_srt_is_default_and_delayed_loading_is_explicit(self):
        self.assertIn(self.srt(), pilot.transcript_input(self.srt(), False))
        self.assertNotIn(self.srt(), pilot.transcript_input(self.srt(), True))
        self.assertNotIn("get_full_transcript", pilot.run_instructions(False))
        self.assertIn("get_full_transcript", pilot.run_instructions(True))


class GeneralizationTests(unittest.TestCase):
    def srt(self, labels):
        return (
            "\n\n".join(
                f"{i}\n00:00:0{i},000 --> 00:00:0{i + 1},000\n{label}: Words here."
                for i, label in enumerate(labels, 1)
            )
            + "\n"
        )

    def test_numeric_ids_preserved_and_names_have_provenance(self):
        text, labels, names = pilot.normalize_speakers(
            self.srt(["Speaker 7", "Alice", "Alice", "UNKNOWN"]), known_labels=["Alice"]
        )
        self.assertEqual(labels, ["Speaker 1", "Speaker 7", "UNKNOWN"])
        self.assertEqual(names, {"Speaker 1": "Alice"})
        self.assertIn("Speaker 7: Words here.", text)

    def test_unlabelled_colon_is_not_assumed_a_speaker(self):
        text, labels, names = pilot.normalize_speakers(self.srt(["The key point"]))
        self.assertEqual(labels, ["UNKNOWN"])
        self.assertEqual(names, {})
        self.assertIn("UNKNOWN: The key point: Words here.", text)

    def test_standalone_srt_and_optional_metadata(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "sample.srt"
            original = self.srt(["Speaker 2"])
            path.write_text(original)
            source = pilot.load_source(path, title="Sample")
            self.assertEqual(source["duration"], 2)
            self.assertEqual(source["speaker_ids"], ["Speaker 2"])
            self.assertIsNone(source["source_url"])
            self.assertEqual(path.read_text(), original)
            with self.assertRaisesRegex(ValueError, "HTTP"):
                pilot.load_source(path, source_url="file:///tmp/foo")

    def test_speech_only_accepts_no_figures_and_no_missing_speech(self):
        result = {
            "output": {
                "markdown": "## One\n**Speaker 1:** Words here.",
                "used_image_ids": [],
                "unresolved_references": [],
                "sections": [{"heading": "One", "first_cue": 1, "last_cue": 1}],
                "speaker_names": [
                    {
                        "speaker_id": "Speaker 1",
                        "name": None,
                        "confidence": "unknown",
                        "evidence": [],
                    }
                ],
            }
        }
        markdown, report = pilot.stitch(result, {}, 1, ["Speaker 1"])
        self.assertEqual(report["figures"], 0)
        self.assertEqual(report["preserved_annotations"], 0)
        result["output"]["markdown"] += " [Untranscribed speech detected.]"
        with self.assertRaisesRegex(ValueError, "invented"):
            pilot.stitch(result, {}, 1, ["Speaker 1"])

    def test_heading_order_checked(self):
        result = PostprocessPilotTests().output()
        result["output"]["markdown"] = (
            "## Interruption\n## Opening\n![Chart](image:frame_0001000)\n[Untranscribed speech detected.]"
        )
        with self.assertRaisesRegex(ValueError, "out of order"):
            pilot.stitch(
                result,
                {"frame_0001000": {"path": "figures/frame_0001000.png"}},
                3,
                annotations=["[Untranscribed speech detected.]"],
            )

    def test_unknown_is_not_one_identifiable_voice(self):
        with self.assertRaisesRegex(ValueError, "Unassigned"):
            pilot.fill_speaker_names(
                "",
                [
                    {
                        "speaker_id": "UNKNOWN",
                        "name": "Alice",
                        "confidence": "confirmed",
                        "evidence": [
                            {"source": "transcript", "cue": 1, "image_id": None, "text": "Name"}
                        ],
                    }
                ],
                {},
                1,
                ["UNKNOWN"],
            )

    def test_scaled_budgets_and_no_video_instructions(self):
        short, long = pilot.budgets_for(300), pilot.budgets_for(3600)
        self.assertLess(short["frames"], long["frames"])
        self.assertLess(short["figure_target"], long["figure_target"])
        self.assertEqual(pilot.budgets_for(3600, False)["frames"], 0)
        self.assertIn("No video is available", pilot.run_instructions(False, False))
        self.assertNotIn("video_screenshots", pilot.run_instructions(False, False))

    def test_large_deferred_transcript_is_chunked_losslessly(self):
        srt = "x" * 220000
        items = pilot.Transcript(srt).execute({})["contentItems"]
        self.assertEqual("".join(i["text"] for i in items), srt)
        self.assertTrue(all(len(i["text"]) <= 90000 for i in items))

    def test_no_video_stream_fails_clearly(self):
        with (
            tempfile.TemporaryDirectory() as d,
            patch.object(pilot.subprocess, "check_output", return_value=b'{"streams":[]}'),
        ):
            with self.assertRaisesRegex(ValueError, "video stream"):
                pilot.Screenshots(Path(d) / "audio.mp4", Path(d), 10)

    def test_missing_speech_annotation_keeps_its_speaker(self):
        result = PostprocessPilotTests().output()
        result["output"]["markdown"] = (
            result["output"]["markdown"]
            .replace("Speaker 1:", "**Speaker 1:**")
            .replace("Speaker 2:", "**Speaker 1:**")
        )
        with self.assertRaisesRegex(ValueError, "wrong speaker"):
            pilot.stitch(
                result,
                {"frame_0001000": {"path": "figures/frame_0001000.png"}},
                3,
                annotations=["[Untranscribed speech detected.]"],
                annotation_labels=[("[Untranscribed speech detected.]", "Speaker 2")],
            )

    def test_video_shorter_than_source_rejected(self):
        with (
            tempfile.TemporaryDirectory() as d,
            patch.object(
                pilot.subprocess,
                "check_output",
                return_value=b'{"streams":[{"width":1280,"height":720}],"format":{"duration":"5"}}',
            ),
        ):
            with self.assertRaisesRegex(ValueError, "shorter"):
                pilot.Screenshots(Path(d) / "source.mp4", Path(d), 20)

    def test_existing_names_cannot_be_reassigned(self):
        result = {
            "output": {
                "markdown": "## One\n**Speaker 1:** Words.",
                "used_image_ids": [],
                "unresolved_references": [],
                "sections": [{"heading": "One", "first_cue": 1, "last_cue": 1}],
                "speaker_names": [
                    {
                        "speaker_id": "Speaker 1",
                        "name": None,
                        "confidence": "unknown",
                        "evidence": [],
                    }
                ],
            }
        }
        with self.assertRaisesRegex(ValueError, "Existing named"):
            pilot.stitch(result, {}, 1, ["Speaker 1"], provided_names={"Speaker 1": "Alice"})

    def test_incomplete_video_rejected_before_model_call(self):
        with patch.object(pilot.subprocess, "check_output", return_value=b""):
            with self.assertRaisesRegex(ValueError, "finish copying"):
                pilot.validate_cached_video(Path("incomplete.mp4"))
        with patch.object(pilot.subprocess, "check_output", return_value=bytes(256)):
            pilot.validate_cached_video(Path("complete.mp4"))


class ExpandedRegressionTests(unittest.TestCase):
    def test_numeric_body_line_is_not_a_cue_number(self):
        text = "1\n00:00:00,000 --> 00:00:02,000\nSpeaker 1: The number is\n100\n"
        self.assertEqual(len(pilot.srt_cues(text)), 1)

    def test_bom_and_crlf_srt_is_accepted(self):
        text = "\ufeff1\r\n00:00:00,000 --> 00:00:02,000\r\nSpeaker 1: Hello.\r\n"
        self.assertEqual(len(pilot.srt_cues(text)), 1)

    def test_reversed_cue_timing_rejected(self):
        text = "1\n00:00:03,000 --> 00:00:02,000\nSpeaker 1: Hello.\n"
        with self.assertRaisesRegex(ValueError, "timing"):
            pilot.srt_cues(text)

    def test_invalid_time_fields_rejected(self):
        text = "1\n00:99:03,000 --> 00:99:04,000\nSpeaker 1: Hello.\n"
        with self.assertRaisesRegex(ValueError, "timing"):
            pilot.srt_cues(text)

    def test_compact_format_round_trips_text_speaker_and_times(self):
        import json
        import random

        rng = random.Random(42)
        for _ in range(30):
            blocks = []
            for i in range(1, 11):
                label = rng.choice(["Speaker 1", "Speaker 7", "UNKNOWN"])
                body = rng.choice(
                    ["Price: 100 | 200.", 'A "quote".\n100', "[Inaudible speech.]", "Olá mundo."]
                )
                blocks.append(f"{i}\n00:00:{i:02},123 --> 00:00:{i + 1:02},456\n{label}: {body}")
            srt = "\n\n".join(blocks) + "\n"
            expected = [
                (
                    c["cue"],
                    c["timestamp"],
                    c["end_timestamp"],
                    c["body"].partition(": ")[0],
                    c["body"].partition(": ")[2],
                )
                for c in pilot.srt_cues(srt)
            ]
            label = None
            actual = []
            for line in pilot.compact_transcript(srt).splitlines():
                if line.startswith("@"):
                    label = line[1:]
                else:
                    cue, start, end, text = json.loads(line)
                    actual.append((cue, start, end, label, text))
            self.assertEqual(actual, expected)

    def test_ten_minute_roundoff_does_not_inflate_budgets(self):
        self.assertEqual(pilot.budgets_for(600), pilot.budgets_for(600.003625))

    def test_remote_source_quotes_paths_and_transfers_only_frame_bytes(self):
        source = pilot.MediaSource("ssh://beast-3.local/home/test/video%20%27%24.mp4")
        with (
            patch.object(pilot.subprocess, "check_output", return_value=b"png") as run,
            tempfile.TemporaryDirectory() as d,
        ):
            path = Path(d) / "frame.png"
            source.extract(["ffmpeg", "-i", str(source), "-frames:v", "1", str(path)], timeout=40)
            self.assertEqual(path.read_bytes(), b"png")
            command = run.call_args.args[0]
            self.assertEqual(command[:4], ["ssh", "-o", "BatchMode=yes", "beast-3.local"])
            import shlex

            args = shlex.split(command[-1])
            self.assertEqual(args[2], "/home/test/video '$.mp4")
            self.assertEqual(args[-1], "pipe:1")
            self.assertNotIn(str(path), args)

    def test_remote_source_rejects_credential_and_option_injection(self):
        for value in [
            "ssh://user:secret@host/a.mp4",
            "ssh://host:22/a.mp4",
            "ssh://-bad/a.mp4",
            "ssh://host/a.mp4?x=1",
            "ssh://host/a%00.mp4",
        ]:
            with self.assertRaises(ValueError):
                pilot.MediaSource(value)

    def test_search_respects_remaining_capture_budget(self):
        raw = b"".join(bytes([v]) * (96 * 54) for v in [0, 60, 120, 180, 240])
        fixture = ScreenshotEfficiencyTests(
            "test_search_returns_distinct_candidates_with_bounded_sampling"
        )
        fixture.setUp()
        try:
            fixture.screenshots.budgets["frames"] = 2
            fixture.encode.return_value = raw
            result = fixture.screenshots.search({"timestamps": [10], "radius": 5})
            self.assertTrue(result["success"])
            self.assertLessEqual(len(fixture.screenshots.assets), 2)
        finally:
            fixture.doCleanups()

    def run_fake_summary(self, source, markdown, expected_error):
        import sys

        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            srt = root / "transcript.srt"
            srt.write_text(source)
            count = len(pilot.srt_cues(source))
            ids = pilot.normalize_speakers(source)[1]
            result = {
                "output": {
                    "markdown": markdown,
                    "used_image_ids": [],
                    "unresolved_references": [],
                    "sections": [{"heading": "One", "first_cue": 1, "last_cue": count}],
                    "speaker_names": [
                        {"speaker_id": label, "name": None, "confidence": "unknown", "evidence": []}
                        for label in ids
                    ],
                },
                "usage": {},
                "tool_calls": [],
                "elapsed_seconds": 0.01,
            }
            with (
                patch.object(
                    sys,
                    "argv",
                    [
                        "postprocess",
                        "--bridge",
                        "http://127.0.0.1:1234",
                        "--transcript",
                        str(srt),
                        "--output",
                        str(root / "out"),
                    ],
                ),
                patch.object(pilot.httpx, "Client") as client,
                patch("builtins.print"),
            ):
                client.return_value.__enter__.return_value.post.return_value.json.return_value = (
                    result
                )
                with self.assertRaisesRegex(ValueError, expected_error):
                    pilot.main()
            self.assertFalse((root / "out" / "processed.md").exists())

    def test_declared_coverage_does_not_allow_a_short_summary(self):
        source = (
            "1\n00:00:00,000 --> 00:00:02,000\nSpeaker 1: "
            + " ".join("word" + str(i) for i in range(500))
            + "\n"
        )
        self.run_fake_summary(source, "## One\n**Speaker 1:** A short summary.", "short")

    def test_whole_pipeline_preserves_speaker_turn_sequence(self):
        word = " ".join("word" + str(i) for i in range(200))
        source = (
            "\n\n".join(
                f"{i}\n00:00:0{i},000 --> 00:00:0{i + 1},000\nSpeaker {label}: {word}"
                for i, label in enumerate([1, 2, 1], 1)
            )
            + "\n"
        )
        self.run_fake_summary(
            source, f"## One\n**Speaker 1:** {word} {word}\n**Speaker 2:** {word}", "speaker turn"
        )

    def test_repeated_prose_prefix_is_not_a_person_name(self):
        source = (
            "\n\n".join(
                f"{i}\n00:00:0{i},000 --> 00:00:0{i + 1},000\nThe key point: Words." for i in [1, 2]
            )
            + "\n"
        )
        _, ids, names = pilot.normalize_speakers(source)
        self.assertEqual(ids, ["UNKNOWN"])
        self.assertEqual(names, {})

    def test_repeated_capitalized_heading_is_not_a_confirmed_person(self):
        source = (
            "1\n00:00:00,000 --> 00:00:02,000\nKey Point: Keep the wording.\n\n"
            "2\n00:00:02,000 --> 00:00:04,000\nKey Point: Keep this too.\n"
        )
        normalized, ids, names = pilot.normalize_speakers(source)
        self.assertEqual(ids, ["UNKNOWN"])
        self.assertEqual(names, {})
        self.assertIn("UNKNOWN: Key Point: Keep the wording.", normalized)

    def test_metadata_identifies_a_named_label_even_in_one_cue(self):
        import json

        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "transcript.srt"
            path.write_text("1\n00:00:00,000 --> 00:00:02,000\nAlice: Hello.\n")
            path.with_suffix(".json").write_text(
                json.dumps({"segments": [{"speaker_id": "a", "speaker_name": "Alice"}]})
            )
            source = pilot.load_source(path)
            self.assertEqual(source["speaker_ids"], ["Speaker 1"])
            self.assertEqual(source["provided_speaker_labels"], {"Speaker 1": "Alice"})

    def test_caller_can_declare_standalone_named_srt_prefixes(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "transcript.srt"
            path.write_text("1\n00:00:00,000 --> 00:00:02,000\nAlice: Hello.\n")
            source = pilot.load_source(path, speaker_labels=["Alice"])
            self.assertEqual(source["provided_speaker_labels"], {"Speaker 1": "Alice"})
            with self.assertRaisesRegex(ValueError, "speaker labels"):
                pilot.load_source(path, speaker_labels=["Alice:\nBob"])

    def test_nonissued_external_figure_is_rejected(self):
        result = {
            "output": {
                "markdown": "## One\n![made up](https://example.org/plot.png)",
                "used_image_ids": [],
                "unresolved_references": [],
                "sections": [{"heading": "One", "first_cue": 1, "last_cue": 1}],
            }
        }
        with self.assertRaisesRegex(ValueError, "unissued"):
            pilot.stitch(result, {}, 1)
