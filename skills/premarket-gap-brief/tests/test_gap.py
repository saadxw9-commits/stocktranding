import json
import sys
import unittest
from pathlib import Path

SKILL = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SKILL))
from gap_brief import brief_one, load_config, run  # noqa: E402

CFG = load_config()


def item(**kw):
    base = dict(etf="EWZ", etf_prev_close=30.0, index_ref=100.0, index_now=105.0, fx_ref=5.0, fx_now=5.0)
    base.update(kw)
    return base


class GapTest(unittest.TestCase):
    def test_local_move_only(self):
        r = brief_one(item(), CFG)
        self.assertEqual(r["expected_gap_pct"], 5.0)
        self.assertEqual(r["expected_open"], 31.5)
        self.assertEqual(r["level"], "large")

    def test_per_usd_currency(self):
        # الريال يقوى من 5.0 إلى 4.9 ريال للدولار: قيمة الأسهم بالدولار ترتفع
        r = brief_one(item(index_now=100.0, fx_now=4.9), CFG)
        self.assertAlmostEqual(r["expected_gap_pct"], 2.04, places=2)
        self.assertEqual(r["level"], "watch")

    def test_usd_per_currency(self):
        # اليورو يضعف من 1.10 إلى 1.089 دولار: قيمة الأسهم بالدولار تنخفض
        r = brief_one(item(etf="EWG", index_now=100.0, fx_ref=1.10, fx_now=1.089), CFG)
        self.assertAlmostEqual(r["expected_gap_pct"], -1.0, places=2)
        self.assertEqual(r["direction"], "down")

    def test_small_gap(self):
        r = brief_one(item(index_now=100.5), CFG)
        self.assertEqual(r["level"], "none")

    def test_priced_in_and_error(self):
        r = brief_one(item(etf_premarket=30.9, etf_open=31.2), CFG)
        self.assertEqual(r["priced_in_share"], 0.6)
        self.assertEqual(r["estimate_error_pct"], 1.0)

    def test_unknown_etf(self):
        with self.assertRaises(ValueError):
            brief_one(item(etf="XYZ"), CFG)

    def test_sorted_by_size(self):
        r = run(dict(items=[item(index_now=101), item(etf="EWG", index_now=110, fx_ref=1.1, fx_now=1.1)]), CFG)
        self.assertEqual(r["items"][0]["etf"], "EWG")

    def test_nov27_fixture(self):
        data = json.loads((SKILL / "fixtures" / "2026-11-27-approx.json").read_text(encoding="utf-8"))
        r = run(data, CFG)["items"][0]
        self.assertEqual(r["level"], "large")
        self.assertGreater(r["expected_gap_pct"], 9.81)  # التقدير أعلى من حركة اليوم الفعلية


if __name__ == "__main__":
    unittest.main()
