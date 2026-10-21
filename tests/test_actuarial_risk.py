import unittest

from risk import apply_customer_credibility


class CustomerCredibilityTests(unittest.TestCase):
    # Verify no completed history preserves rule score.
    def test_no_completed_history_preserves_rule_score(self):
        score, weight = apply_customer_credibility(0.4)
        self.assertEqual(score, 0.4)
        self.assertEqual(weight, 0)

    # Verify deliveries lower estimate toward observed failure rate.
    def test_deliveries_lower_estimate_toward_observed_failure_rate(self):
        score, weight = apply_customer_credibility(0.4, completed=5, failed=0)
        self.assertEqual(weight, 0.5)
        self.assertEqual(score, 0.2)

    # Verify refused and returned raise estimate.
    def test_refused_and_returned_raise_estimate(self):
        score, _ = apply_customer_credibility(0.4, completed=5, failed=5)
        self.assertEqual(score, 0.7)

    # Verify more history increases credibility.
    def test_more_history_increases_credibility(self):
        _, small_weight = apply_customer_credibility(0.4, completed=1, failed=1)
        _, large_weight = apply_customer_credibility(0.4, completed=20, failed=20)
        self.assertGreater(large_weight, small_weight)

    # Verify inputs are bounded.
    def test_inputs_are_bounded(self):
        score, weight = apply_customer_credibility(1.5, completed=2, failed=50)
        self.assertEqual(score, 0.999)
        self.assertGreaterEqual(weight, 0)
        self.assertLessEqual(weight, 1)


if __name__ == "__main__":
    unittest.main()
