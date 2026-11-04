import unittest

from score import cpwer, der, distance


class ScoreTests(unittest.TestCase):
    def test_word_edits(self):
        self.assertEqual(distance("a b c".split(), "a x c d".split()), 2)
        self.assertEqual(distance([], ["extra"]), 1)

    def test_cpwer_permutation_and_extra_speaker(self):
        ref = [{"text": "one two", "speaker": "A"}, {"text": "three", "speaker": "B"}]
        hyp = [{"text": "one two", "speaker": "2"}, {"text": "three", "speaker": "1"}]
        self.assertEqual(cpwer(ref, hyp)["errors"], 0)
        hyp.append({"text": "extra", "speaker": "3"})
        self.assertEqual(cpwer(ref, hyp)["errors"], 1)

    def test_der_permutation(self):
        ref = [{"start": 0, "end": 1, "speaker": "A"}, {"start": 1, "end": 2, "speaker": "B"}]
        hyp = [{"start": 0, "end": 1, "speaker": "2"}, {"start": 1, "end": 2, "speaker": "1"}]
        self.assertEqual(der(ref, hyp, [[0, 2]], collar=0)["rate"], 0)

    def test_der_missed_false_and_overlap(self):
        ref = [{"start": 0, "end": 2, "speaker": "A"}, {"start": 1, "end": 2, "speaker": "B"}]
        hyp = [{"start": 0, "end": 3, "speaker": "1"}]
        result = der(ref, hyp, [[0, 3]], collar=0)
        self.assertEqual(result["missed_seconds"], 1)
        self.assertEqual(result["false_alarm_seconds"], 1)
        self.assertAlmostEqual(result["rate"], 2 / 3)
        self.assertEqual(der(ref, hyp, [[0, 2]], collar=0, exclude_overlap=True)["rate"], 0)

    def test_collar_masks_reference_boundaries(self):
        ref = [{"start": 1, "end": 3, "speaker": "A"}]
        hyp = [{"start": 1.2, "end": 2.8, "speaker": "1"}]
        self.assertAlmostEqual(der(ref, hyp, [[0, 4]], collar=0)["rate"], 0.2)
        self.assertEqual(der(ref, hyp, [[0, 4]], collar=0.25)["rate"], 0)


if __name__ == "__main__":
    unittest.main()
