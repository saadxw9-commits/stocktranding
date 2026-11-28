import json
import sys
import unittest
from datetime import date
from pathlib import Path

import yaml

SKILL = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SKILL))
from analyze import compare, finish_probability, touch_probability, effective_years, option_value, stamp
from fair_value import analyze as fair_value


class ProbabilityTest(unittest.TestCase):
    def test_touch_at_least_finish(self):
        for side, level in (("call", 7425), ("put", 7150)):
            f = finish_probability(7297.49, level, side, 2 / 365, 0.17, 0.04, 0.012)
            t = touch_probability(7297.49, level, side, 2 / 365, 0.17, 0.04, 0.012)
            self.assertGreater(t, f)
            self.assertLess(t, 1)

    def test_touch_when_already_past(self):
        self.assertEqual(touch_probability(7300, 7250, "call", 0.01, 0.17, 0.04, 0.0), 1.0)
        self.assertEqual(touch_probability(7200, 7250, "put", 0.01, 0.17, 0.04, 0.0), 1.0)

    def test_effective_time_recovers_price(self):
        years = 2 / 365
        mid = option_value(7297.49, 7425, "call", years, 0.1709, 0.04, 0.012)
        t, src = effective_years(mid, 7297.49, 7425, "call", 4 / 365, 0.1709, 0.04, 0.012)
        self.assertEqual(src, "market_calibrated")
        self.assertAlmostEqual(t * 365, 2, places=3)


class CaseSPXW7425(unittest.TestCase):
    """حالة 27 نوفمبر 2026: Call 7425 بانتهاء 30 نوفمبر، دلتا 0.09، بعد صعود بأحجام ضعيفة."""

    def setUp(self):
        self.data = json.loads((SKILL / "fixtures/spxw-7425-2026-11-27.json").read_text(encoding="utf-8"))
        self.rules = yaml.safe_load((SKILL.parents[1] / "config/risk-rules.yaml").read_text(encoding="utf-8"))
        self.report = compare(self.data, self.rules, stamp(self.data["as_of"]))

    def test_wait(self):
        self.assertEqual(self.report["decision"], "WAIT")
        c = next(c for c in self.report["contracts"] if c["strike"] == 7425)
        self.assertIn("delta_outside_band", c["rejected"])
        self.assertIn("wide_spread", c["rejected"])

    def test_probabilities_match_market_delta(self):
        c = next(c for c in self.report["contracts"] if c["strike"] == 7425)
        p = c["probabilities"]
        self.assertEqual(p["time_basis"], "market_calibrated")
        self.assertAlmostEqual(p["finish_itm"], c["delta"], delta=0.01)
        self.assertTrue(0.07 < p["finish_above_breakeven"] < 0.09)
        self.assertGreater(p["touch_strike"], p["finish_itm"])

    def test_session_context(self):
        ctx = self.report["prior_session"]
        self.assertEqual(ctx["range_pts"], 92.64)
        self.assertGreater(ctx["close_location_pct"], 90)
        self.assertIn("low_volume_prior_session", self.report["warnings"])


class FairValueTest(unittest.TestCase):
    def test_friday_basis(self):
        r = fair_value(7310.50, 7297.49, date(2026, 11, 27), date(2026, 12, 18), 0.04, 0.012,
                       futures_at_index_close=7310.50)
        self.assertEqual(r["days_to_expiry"], 21)
        self.assertAlmostEqual(r["fair_value_model_pts"], 11.92, places=2)
        self.assertAlmostEqual(r["observed_basis_pts"], 13.01, places=2)

    def test_published_overrides_model(self):
        r = fair_value(7350, 7297.49, date(2026, 11, 30), date(2026, 12, 18), 0.04, 0.012,
                       published_fair_value=10.0)
        self.assertEqual(r["fair_value_source"], "published")
        self.assertEqual(r["implied_index"], 7340.0)

    def test_expired(self):
        with self.assertRaises(ValueError):
            fair_value(7300, 7297, date(2026, 12, 19), date(2026, 12, 18), 0.04, 0.012)


if __name__ == "__main__":
    unittest.main()
