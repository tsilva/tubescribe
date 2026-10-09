import unittest

from experiments.diarization.review import evaluate, identify, label_words


class DiarizationReview(unittest.TestCase):
    def test_word_overlap_and_unknown_gap(self):
        turns = [
            {"start": 0, "end": 1, "speaker": "A"},
            {"start": 1, "end": 2, "speaker": "B"},
        ]
        self.assertEqual(identify(turns, 0.9, 1.4), "B")
        self.assertEqual(identify(turns, 3, 4), "UNKNOWN")

    def test_cues_split_at_speaker_change_without_dropping_unmatched_words(self):
        asr = {
            "segments": [
                {
                    "words": [
                        {"start": 0, "end": 1, "word": "Question?"},
                        {"start": 1.1, "end": 2, "word": "Answer."},
                        {"start": 3, "end": 4, "word": "Unmatched."},
                    ]
                }
            ]
        }
        turns = [
            {"start": 0, "end": 1, "speaker": "A"},
            {"start": 1, "end": 2, "speaker": "B"},
        ]
        cues = label_words(asr, turns, {"A": "Host", "B": "Guest"})
        self.assertEqual([cue["speaker_id"] for cue in cues], ["A", "B", "UNKNOWN"])
        self.assertEqual([cue["text"] for cue in cues], ["Question?", "Answer.", "Unmatched."])

    def test_name_mapping_uses_anchors_and_does_not_hide_later_errors(self):
        reference = {
            "duration": 30,
            "turns": [
                {"start": 0, "speaker": "Jordi"},
                {"start": 10, "speaker": "Marty"},
                {"start": 20, "speaker": "Jordi"},
            ],
        }
        run = {
            "exclusive_turns": [
                {"start": 0, "end": 10, "speaker": "A"},
                {"start": 10, "end": 30, "speaker": "B"},
            ]
        }
        result = evaluate(run, reference)
        self.assertEqual(result["mapping"], {"A": "Jordi", "B": "Marty"})
        self.assertEqual(result["label_agreement_on_covered_speech"], 0)
        self.assertEqual(result["reference_interval_coverage"], 1)
