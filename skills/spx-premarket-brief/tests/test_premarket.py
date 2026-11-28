import copy
import json
import sys
import unittest
from pathlib import Path

SKILL = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SKILL))
from premarket import build, classify_gap, to_markdown


class PremarketTest(unittest.TestCase):
    def setUp(self):
        self.snap = json.loads((SKILL / "fixtures/example.json").read_text(encoding="utf-8"))

    def test_published_fair_value_first(self):
        r = build(self.snap)
        self.assertEqual(r["chosen_method"], "cnbc_fair_value")
        self.assertAlmostEqual(r["implied_open"], 7339.85, places=2)
        self.assertEqual(r["open_location"], "above_prior_high")
        self.assertEqual(r["warnings"], [])

    def test_fallback_to_vs_fair_value(self):
        del self.snap["published_fair_value"]
        r = build(self.snap)
        self.assertEqual(r["chosen_method"], "vs_fair_value")
        self.assertAlmostEqual(r["implied_open"], 7338.49, places=2)

    def test_model_only_warns(self):
        del self.snap["published_fair_value"]
        del self.snap["futures_vs_fair_value"]
        r = build(self.snap)
        self.assertEqual(r["chosen_method"], "model")
        self.assertIn("no_published_fair_value_model_only", r["warnings"])

    def test_divergence_and_stale(self):
        self.snap["futures_vs_fair_value"]["points"] = 30.0
        self.snap["futures"]["quoted_at"] = "2026-11-30T07:00:00-05:00"
        r = build(self.snap)
        self.assertIn("published_sources_diverge", r["warnings"])
        self.assertIn("stale_futures", r["warnings"])

    def test_watch_strike_distance(self):
        w = build(self.snap)["watch_strikes"][0]
        self.assertAlmostEqual(w["distance_from_open_pts"], 85.15, places=2)
        self.assertAlmostEqual(w["breakeven_distance_pts"], 88.95, places=2)
        self.assertGreater(w["distance_sigma"], 1)

    def test_roll_window(self):
        s = copy.deepcopy(self.snap)
        s["as_of"] = "2026-12-11T08:45:00-05:00"
        s["futures"]["quoted_at"] = s["published_fair_value"]["quoted_at"] = s["futures_vs_fair_value"]["quoted_at"] = s["as_of"]
        self.assertIn("futures_roll_window_check_contract", build(s)["warnings"])

    def test_classify(self):
        self.assertEqual(classify_gap(0.1), "flat")
        self.assertEqual(classify_gap(-1.2), "large")

    def test_markdown_arabic(self):
        md = to_markdown(build(self.snap))
        self.assertIn("الافتتاح الضمني", md)
        self.assertIn("بتوقيت الرياض", md)


if __name__ == "__main__":
    unittest.main()
