import io
import tempfile
import unittest
import wave
from pathlib import Path
from unittest.mock import patch

from tubescribe.config import LEGACY_OPENROUTER_PROFILE, OPENROUTER_PROFILE
from tubescribe.manual_queue import PREFIX, authorize_execution, change, request_for
from tubescribe.openrouter import OpenRouter
from tubescribe.pipeline import Plan
from tubescribe.speaker_continuity import RATE, REFERENCE_BUDGET, SpeakerContinuity
from tubescribe.storage import Store


def word(text, start, end, voice):
    return {"word": text, "start": start, "end": end, "speaker": voice}


class Continuity(unittest.TestCase):
    def test_returning_voice_after_gap_matches_audio_not_local_number(self):
        bank = SpeakerContinuity()
        pcm = b"\x01\0" * RATE * 6
        first, _ = bank.apply({"words": [word("Alice", 0, 3, 0), word("Bob", 3, 6, 1)]},
                              pcm, 0, [])
        self.assertEqual([w["speaker"] for w in first["words"]], ["Speaker 1", "Speaker 2"])
        request, prefix, refs = bank.request(pcm)
        with wave.open(io.BytesIO(request)) as audio:
            self.assertAlmostEqual(audio.getnframes() / RATE, prefix + 6)
        # Native IDs have swapped. Alice's voice is absent from this part's new audio.
        second, _ = bank.apply({"words": [word("reference A", refs[0][1], refs[0][2], 9),
            word("reference B", refs[1][1], refs[1][2], 2), word("only Bob", prefix, prefix+6, 2)]},
            pcm, prefix, refs)
        self.assertEqual([w["speaker"] for w in second["words"]], ["Speaker 2"])
        _, prefix, refs = bank.request(pcm)
        third, _ = bank.apply({"words": [word("reference A", refs[0][1], refs[0][2], 5),
            word("reference B", refs[1][1], refs[1][2], 0), word("Alice returns", prefix, prefix+6, 5)]},
            pcm, prefix, refs)
        self.assertEqual(third["text"], "Alice returns")
        self.assertEqual(third["words"][0]["speaker"], "Speaker 1")
        self.assertEqual(third["words"][0]["start"], 0)
        self.assertEqual(bank.next_id, 3)

    def test_conflated_references_do_not_merge_different_known_voices(self):
        bank = SpeakerContinuity()
        pcm = b"\0\0" * RATE * 6
        bank.apply({"words": [word("A", 0, 3, 0), word("B", 3, 6, 1)]}, pcm, 0, [])
        _, prefix, refs = bank.request(pcm)
        result, _ = bank.apply({"words": [word("A", refs[0][1], refs[0][2], 0),
            word("B", refs[1][1], refs[1][2], 0), word("uncertain", prefix, prefix+2, 0)]},
            pcm, prefix, refs)
        self.assertIsNone(result["words"][0]["speaker"])
        self.assertEqual(bank.unresolved, 2)

    def test_reference_budget_and_no_reference_text_on_silent_body(self):
        bank = SpeakerContinuity()
        pcm = b"\0\0" * RATE * 3
        for index in range(32):
            bank.anchors[f"Speaker {index+1}"] = pcm
        request, prefix, refs = bank.request(pcm)
        self.assertLessEqual(prefix, REFERENCE_BUDGET)
        result, _ = bank.apply({"words": [word("reference only", refs[0][1], refs[0][2], 0)]},
                              pcm, prefix, refs)
        self.assertEqual(result["text"], "")
        self.assertEqual(result["words"], [])

    def test_resume_rebuilds_identical_reference_audio_and_reuses_paid_results(self):
        with tempfile.TemporaryDirectory() as directory:
            store = Store(Path(directory))
            self.addCleanup(store.db.close)
            source = Path(directory) / "source.wav"
            with wave.open(str(source), "wb") as audio:
                audio.setparams((1, 2, RATE, 0, "NONE", ""))
                audio.writeframes(b"\x01\0" * RATE * 4)
            job = {"channel_id": "channel", "video_id": "abcdefghijk",
                   "profile": OPENROUTER_PROFILE, "duration": 4}
            first = {"status": 200, "response": {"language": "en", "usage": {"cost": .01},
                     "words": [word("first", .1, 1.8, 0)]}}
            # A definitive rejection can safely be retried; successful part zero must not repeat.
            rejected = {"status": 429}
            second = {"status": 200, "response": {"language": "en", "usage": {"cost": .02},
                      "words": [word("reference", 0, 1.7, 7), word("second", 1.85, 3.85, 7)]}}
            with (patch("tubescribe.openrouter.CHUNK_SECONDS", 2),
                  patch("tubescribe.openrouter.launcher", return_value=Path("/fixture")),
                  patch("tubescribe.openrouter.call", side_effect=[first, rejected, second]) as call):
                with self.assertRaisesRegex(ValueError, "429"):
                    OpenRouter().transcribe(store, job, source)
                result = OpenRouter().transcribe(store, job, source)
                self.assertEqual(call.call_count, 3)
            self.assertEqual({s["speaker_id"] for s in result["segments"]}, {"Speaker 1"})
            self.assertEqual(" ".join(s["text"] for s in result["segments"]), "first second")
            self.assertEqual(result["cost"], .03)
            self.assertGreaterEqual(result["segments"][-1]["start"], 2)

    def test_legacy_retry_keeps_model_profile_and_skips_separate_diarization(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict(
            "os.environ", {"TUBESCRIBE_MANUAL_QUEUE": "1"}
        ):
            store = Store(Path(directory))
            self.addCleanup(store.db.close)
            store.channel({"id": "channel", "title": "Channel", "url": "https://example.test"})
            store.index("channel", "videos", [{"id": "abcdefghijk", "title": "Old video",
                        "url": "https://example.test/watch", "duration": 1}], None)
            old = request_for("abcdefghijk", LEGACY_OPENROUTER_PROFILE)
            identifier, _ = store.enqueue(old.encode(), PREFIX + "abcdefghijk")
            store.update_submission(identifier, state="failed")
            self.assertEqual(change(store.root, "retry", ["abcdefghijk"])["changed"], 1)
            selected = store.queued_submissions(manual=True)[0]
            authorize_execution(store, old, selected["id"])
            self.assertIn(LEGACY_OPENROUTER_PROFILE, selected["request"])
            self.assertEqual(OpenRouter(LEGACY_OPENROUTER_PROFILE).health()["decoding"],
                             {"chunk_seconds": 480, "timestamp_granularities": ["word"]})
            self.assertIsNone(Plan(True, False, False).next_stage(
                {"profile": LEGACY_OPENROUTER_PROFILE, "state": "complete"}))
