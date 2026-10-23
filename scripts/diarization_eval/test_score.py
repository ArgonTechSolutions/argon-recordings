import unittest

from score import Turn, score


class DiarizationScoreTests(unittest.TestCase):
    def run_score(self, ref, hyp, duration=4, **kwargs):
        options = {"collar": 0}
        options.update(kwargs)
        return score(ref, hyp, duration=duration, step=0.01, **options)

    def test_all_silent(self):
        result = self.run_score([], [])
        self.assertEqual(result["der"], 0)
        self.assertEqual(result["scored_speech_seconds"], 0)

    def test_miss_and_false_alarm_have_correct_directions(self):
        miss = self.run_score([Turn("A", 0, 2)], [])
        self.assertAlmostEqual(miss["miss_seconds"], 2)
        self.assertEqual(miss["false_alarm_seconds"], 0)
        false_alarm = self.run_score([], [Turn("X", 0, 2)])
        self.assertEqual(false_alarm["miss_seconds"], 0)
        self.assertAlmostEqual(false_alarm["false_alarm_seconds"], 2)

    def test_overlapping_reference_one_of_two_missing_counts_as_miss(self):
        result = self.run_score(
            [Turn("A", 0, 2), Turn("B", 1, 3)], [Turn("X", 0, 3)], ignore_overlap=False
        )
        self.assertAlmostEqual(result["miss_seconds"], 1)
        # The same single hypothesis identity cannot map to both reference IDs;
        # the non-overlapping B-only tail therefore contributes confusion too.
        self.assertAlmostEqual(result["confusion_seconds"], 1)

    def test_global_label_permutation(self):
        result = self.run_score(
            [Turn("A", 0, 2), Turn("B", 2, 4)],
            [Turn("Y", 0, 2), Turn("X", 2, 4)],
        )
        self.assertEqual(result["der"], 0)

    def test_undercount_is_confusion_when_speech_present(self):
        result = self.run_score(
            [Turn("A", 0, 2), Turn("B", 2, 4)], [Turn("X", 0, 4)]
        )
        self.assertAlmostEqual(result["confusion_seconds"], 2)
        self.assertEqual(result["speaker_count_error"], -1)

    def test_250ms_collar_masks_125ms_on_each_side_of_reference_boundaries(self):
        result = self.run_score(
            [Turn("A", 0, 1)], [Turn("X", 0, 0.5)], duration=1, collar=0.25,
            ignore_overlap=False,
        )
        self.assertAlmostEqual(result["miss_seconds"], 0.37, places=2)
        self.assertAlmostEqual(result["scored_speech_seconds"], 0.75, places=2)

    def test_speaker_counts_use_full_clipped_turns_not_collar_frames(self):
        result = self.run_score(
            [Turn("A", 0, 1), Turn("B", 0.05, 0.1)],
            [Turn("X", 0, 1)], duration=1, collar=0.25, ignore_overlap=False,
        )
        self.assertEqual(result["reference_speakers"], 2)
        self.assertEqual(result["hypothesis_speakers"], 1)
        self.assertEqual(result["speaker_count_error"], -1)

    def test_overlap_inclusive_and_excluded_are_both_available(self):
        ref = [Turn("A", 0, 3), Turn("B", 1, 2)]
        hyp = [Turn("X", 0, 3)]
        included = self.run_score(ref, hyp, duration=3, collar=0, ignore_overlap=False)
        excluded = self.run_score(ref, hyp, duration=3, collar=0, ignore_overlap=True)
        self.assertGreater(included["miss_seconds"], excluded["miss_seconds"])


if __name__ == "__main__":
    unittest.main()
