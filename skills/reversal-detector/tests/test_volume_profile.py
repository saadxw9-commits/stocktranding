import sys
import unittest
from pathlib import Path

SKILL = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SKILL))
from volume_profile import build, levels_for_detector, naked_pocs, split_sessions  # noqa: E402
from test_detector import FLAT, RALLY, SWEEP_UP, bars_from, ctx  # noqa: E402
from detector import run  # noqa: E402


def bar(t, lo, hi, v):
    return dict(t=f"2026-12-01T10:{t:02d}:00-05:00", o=lo, h=hi, l=lo, c=hi, v=v)


class ProfileTest(unittest.TestCase):
    def test_poc_and_value_area(self):
        # حجم كبير عند 100، وحجم أقل يتناقص حولها
        bars = [bar(0, 100, 100, 1000), bar(1, 99, 99, 300), bar(2, 101, 101, 300),
                bar(3, 98, 98, 100), bar(4, 102, 102, 100), bar(5, 95, 95, 50), bar(6, 105, 105, 50)]
        p = build(bars, 1.0)
        self.assertEqual(p["poc"], 100)
        self.assertEqual((p["val"], p["vah"]), (99, 102))      # 1600 من 1900 = 84% ≥ 70%
        self.assertGreaterEqual(p["value_area_pct"], 0.70)

    def test_volume_spread_over_bar_range(self):
        p = build([bar(0, 100, 101.75, 800)], 0.25)
        self.assertEqual(len(p["bins"]), 8)
        self.assertAlmostEqual(sum(v for _, v in p["bins"]), 800, places=1)

    def test_price_scale(self):
        p = build([bar(0, 600, 600, 100)], 1.0, price_scale=10.0)
        self.assertEqual(p["poc"], 6000)

    def test_hvn_and_lvn(self):
        # توزيع بقمتين عند 100 و110 وفجوة حجم بينهما
        bars = [bar(i % 60, x, x, v) for i, (x, v) in enumerate(
            [(p, 1000 - 120 * abs(p - 100)) for p in range(95, 106)] +
            [(p, 1000 - 120 * abs(p - 110)) for p in range(106, 116)])]
        p = build(bars, 1.0)
        self.assertTrue(any(abs(h - 100) <= 1 for h in p["hvn"]))
        self.assertTrue(any(abs(h - 110) <= 1 for h in p["hvn"]))
        self.assertTrue(any(104 <= x <= 106 for x in p["lvn"]))

    def test_naked_poc(self):
        s1 = [dict(t="2026-12-01T10:00:00-05:00", h=101, l=99, v=1000)]
        s2 = [dict(t="2026-12-02T10:00:00-05:00", h=110, l=105, v=1000)]
        s3 = [dict(t="2026-12-03T10:00:00-05:00", h=108, l=104, v=1000)]
        profs = [(d, build(bs, 1.0)) for d, bs in split_sessions(s1 + s2 + s3)]
        naked = naked_pocs(profs)
        self.assertEqual([n["date"] for n in naked], ["2026-12-01"])   # 2 ديسمبر لُمست في 3 ديسمبر

    def test_detector_uses_value_area_low(self):
        # جلسة سابقة نقطة تحكمها 5001 وأدنى منطقة قيمتها قرب 4998؛ الكسر الكاذب إلى 4997.5 يجد المنطقة
        prior = [bar(i, x, x, max(50, 600 - 60 * abs(x - 5001))) for i, x in enumerate(range(4994, 5010))]
        vp = {"prior_bars": prior, "bin_size": 1.0}
        d = ctx(bars_from(FLAT + SWEEP_UP + RALLY), levels={}, reference_price=None, volume_profile=vp)
        r = run(d)
        sig = next(e for e in r["events"] if e["type"] == "signal")
        self.assertIn("منطقة القيمة", sig["zone"])
        lv = levels_for_detector(build(prior, 1.0))
        self.assertIn("نقطة التحكم للجلسة السابقة", [n for _, n in lv])


if __name__ == "__main__":
    unittest.main()
