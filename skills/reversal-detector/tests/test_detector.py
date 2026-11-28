import json
import sys
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

SKILL = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SKILL))
from detector import Detector, run  # noqa: E402

ET = timezone(timedelta(hours=-5))
T0 = datetime(2026, 12, 1, 10, 0, tzinfo=ET)


def bars_from(rows, step=10):
    """rows: (o, h, l, c, v) بشموع ١٠ ثوانٍ تبدأ 10:00:00."""
    return [dict(t=(T0 + timedelta(seconds=i * step)).isoformat(), o=o, h=h, l=l, c=c, v=v)
            for i, (o, h, l, c, v) in enumerate(rows)]


def ctx(bars, **kw):
    base = dict(session_close=(T0 + timedelta(hours=6)).isoformat(), reference_price=5010,
                levels={"prev_low": 4998.0}, config={"range_seconds": 60, "reclaim_seconds": 30},
                bars=bars)
    base.update(kw)
    return base


FLAT = [(5001, 5002, 5000, 5001, 100)] * 8           # نطاق 5000–5002
SWEEP_UP = [(5001, 5001, 4997.5, 4999, 200),            # كسر لأسفل بحجم مرتفع قرب قاع اليوم السابق
            (4999, 5003, 4999, 5002.5, 150)]            # استعادة فوق 5002
RALLY = [(5003 + i, 5004 + i, 5002.5 + i, 5004 + i, 100) for i in range(60)]


class DetectorTest(unittest.TestCase):
    def kinds(self, report):
        return [e["type"] for e in report["events"]]

    def test_failed_breakdown_long(self):
        r = run(ctx(bars_from(FLAT + SWEEP_UP + RALLY)))
        entry = next(e for e in r["events"] if e["type"] == "entry")
        self.assertEqual(entry["side"], "call")
        self.assertEqual(entry["stop"], 4996.5)
        self.assertEqual(entry["t1"], 5025)
        self.assertGreater(r["summary"]["total_r"], 0)

    def test_no_signal_without_volume(self):
        low_vol = [(o, h, l, c, 100) for o, h, l, c, _ in SWEEP_UP]
        r = run(ctx(bars_from(FLAT + low_vol + RALLY)))
        self.assertNotIn("entry", self.kinds(r))
        self.assertIn("rejected_signal", self.kinds(r))

    def test_zone_required(self):
        r = run(ctx(bars_from(FLAT + SWEEP_UP + RALLY), levels={}, reference_price=None))
        self.assertNotIn("entry", self.kinds(r))

    def test_stop_hit(self):
        dump = [(5002, 5002, 4990 - i, 4991 - i, 100) for i in range(5)]
        r = run(ctx(bars_from(FLAT + SWEEP_UP + dump)))
        closed = next(e for e in r["events"] if e["type"] == "trade_closed")
        self.assertEqual(closed["fills"][-1]["reason"], "stop")
        self.assertLess(closed["result_r"], 0)

    def test_failed_breakout_short(self):
        mirror = lambda rows: [(10000 - o, 10000 - l, 10000 - h, 10000 - c, v) for o, h, l, c, v in rows]
        r = run(ctx(bars_from(mirror(FLAT + SWEEP_UP + RALLY)), levels={"prev_high": 5002.0},
                    reference_price=4990))
        entry = next(e for e in r["events"] if e["type"] == "entry")
        self.assertEqual(entry["side"], "put")
        self.assertGreater(r["summary"]["total_r"], 0)

    def test_higher_low(self):
        # قاع 4990 ثم صعود ثم عودة إلى 4992–4994 دون كسر القاع، ثم إغلاق فوق النطاق
        drop = [(5000 - 2 * i, 5001 - 2 * i, 4999 - 2 * i, 4998 - 2 * i, 100) for i in range(5)]
        low = [(4991, 4992, 4990, 4991.5, 150)]
        bounce = [(4992 + i, 4994 + i, 4991.5 + i, 4993.5 + i, 100) for i in range(5)]
        back = [(4996 - i, 4996.5 - i, 4994.5 - i, 4995 - i, 100) for i in range(3)]
        base = [(4993, 4994.5, 4992.2, 4993.5, 100)] * 60      # عشر دقائق قرب القاع دون كسره
        go = [(4993.5, 4997, 4993.4, 4996.5, 180)]
        cfg = {"range_seconds": 60, "reclaim_seconds": 30, "zone_tolerance_pts": 3.0}
        r = run(ctx(bars_from(drop + low + bounce + back + base + go + RALLY), config=cfg))
        sig = next(e for e in r["events"] if e["type"] == "signal")
        self.assertEqual(sig["pattern"], "higher_low")
        self.assertEqual(sig["zone_level"], 4990)
        entry = next(e for e in r["events"] if e["type"] == "entry")
        self.assertEqual(entry["pattern"], "higher_low")
        self.assertEqual(entry["stop"], round(sig["extreme"] - 1.0, 2))

    def test_expected_move_zone(self):
        # تذبذب 16% على 5000: الانحراف اليومي ≈ 50.4، فالحد السفلي ≈ 4949.6
        em = {"reference": 5000, "iv": 0.16}
        flat = [(4951, 4952, 4950, 4951, 100)] * 8
        sweep = [(4951, 4951, 4948.5, 4949, 200), (4949, 4953, 4949, 4952.5, 150)]
        rally = [(4953 + i, 4954 + i, 4952.5 + i, 4954 + i, 100) for i in range(80)]
        d = ctx(bars_from(flat + sweep + rally), levels={}, reference_price=None, expected_move=em)
        r = run(d)
        sig = next(e for e in r["events"] if e["type"] == "signal")
        self.assertEqual(sig["zone"], "الحد السفلي للحركة المتوقعة اليومية (1σ)")
        self.assertAlmostEqual(sig["zone_level"], 4949.6, places=1)
        entry = next(e for e in r["events"] if e["type"] == "entry")
        self.assertEqual(entry["t1_zone"], "الحد العلوي للحركة المتوقعة اليومية (1σ)")
        # من سعر Straddle: الأسبوعي = اليومي × جذر 5
        lv = Detector(dict(d, expected_move={"reference": 5000, "daily_straddle_pts": 40}))._expected_move_levels()
        self.assertIn((round(5000 + 40 * 5 ** 0.5, 2), "الحد العلوي للحركة المتوقعة الأسبوعية (1σ)"), lv)

    def test_time_order(self):
        det = Detector(ctx([]))
        b = bars_from(FLAT[:2])
        det.feed(b[1])
        with self.assertRaises(ValueError):
            det.feed(b[0])

    def test_nov27_replay(self):
        data = json.loads((SKILL / "fixtures" / "spx-2026-11-27-approx.json").read_text(encoding="utf-8"))
        r = run(data)
        sig = next(e for e in r["events"] if e["type"] == "signal" and e["side"] == "call")
        entry = next(e for e in r["events"] if e["type"] == "entry" and e["side"] == "call")
        self.assertEqual(sig["at"].strftime("%H:%M"), "10:06")
        self.assertEqual(sig["extreme"], 7211.46)
        self.assertEqual(entry["at"].strftime("%H:%M"), "10:07")
        self.assertEqual(entry["stop"], 7210.46)


class DefaultsUnchanged(unittest.TestCase):
    def test_hypotheses_off_by_default(self):
        data = json.loads((SKILL / "fixtures" / "spx-2026-11-27-approx.json").read_text(encoding="utf-8"))
        pats = {e["pattern"] for e in run(data)["events"] if e["type"] == "signal"}
        self.assertNotIn("breakdown_retest", pats)


class Nov27BothSides(unittest.TestCase):
    """27 نوفمبر: كسر 7250 ثم فشل استعادته (Put)، ثم ارتداد من القاع 7211 (Call). بيانات تقريبية."""

    def setUp(self):
        self.data = json.loads((SKILL / "fixtures" / "spx-2026-11-27-approx.json").read_text(encoding="utf-8"))
        self.plan = json.loads((SKILL / "fixtures" / "external-plans" / "2026-11-27-spxsniper.json")
                               .read_text(encoding="utf-8"))
        self.data["config"] = {"level_retest": True, "reverse_on_opposite_signal": True}   # الفرضيتان المعلنتان مفعّلتان صراحةً

    def test_put_then_reverse_to_call(self):
        ev = run(self.data)["events"]
        sig = [e for e in ev if e["type"] == "signal"]
        self.assertEqual([(e["side"], e["pattern"], e["at"].strftime("%H:%M")) for e in sig[:2]],
                         [("put", "breakdown_retest", "09:37"), ("call", "failed_breakdown", "10:06")])
        put_entry = next(e for e in ev if e["type"] == "entry" and e["side"] == "put")
        self.assertEqual((put_entry["entry"], put_entry["stop"]), (7247.75, 7251.5))
        rev = next(e for e in ev if e["type"] == "exit" and e["reason"] == "reverse")
        self.assertEqual(rev["at"].strftime("%H:%M"), "10:06")
        closed = [e for e in ev if e["type"] == "trade_closed"]
        self.assertEqual([c["side"] for c in closed], ["put", "call"])
        self.assertTrue(all(c["result_r"] > 0 for c in closed))

    def test_external_plan_levels_are_tagged(self):
        from detector import add_external_levels
        sys.path.insert(0, str(SKILL.parents[0] / "journal"))
        from shadow import zone_kind
        ev = run(add_external_levels(self.data, self.plan))["events"]
        put = next(e for e in ev if e["type"] == "entry" and e["side"] == "put")
        self.assertTrue(put["zone"].startswith("خارجي"))
        self.assertEqual(zone_kind(put["zone"]), "مستوى خارجي")
        self.assertEqual(put["t1"], 7240.0)

    def test_no_reverse_when_disabled(self):
        self.data["config"] = {"level_retest": True, "reverse_on_opposite_signal": False}
        ev = run(self.data)["events"]
        self.assertFalse(any(e["type"] == "exit" and e["reason"] == "reverse" for e in ev))


class FlatCutoff(unittest.TestCase):
    def test_no_entry_after_flat_cutoff(self):
        # إشارة قرب الإغلاق: الدخول بعد (الإغلاق − 15 دقيقة) يُتخطّى ولا يُفتح مركز
        rows = FLAT + SWEEP_UP + RALLY[:5]
        c = ctx(bars_from(rows), session_close=(T0 + timedelta(minutes=10)).isoformat())
        ev = run(c)["events"]
        self.assertFalse(any(e["type"] == "entry" for e in ev))
        self.assertTrue(any(e["type"] == "skip" and e.get("reason") == "after_flat_cutoff" for e in ev))


class LevelRetest(unittest.TestCase):
    """نمط الاستمرار بالاتجاهين على مستوى خارجي 5010."""

    def run_rows(self, rows):
        return run(ctx(bars_from(rows, step=60), levels={"custom": [{"price": 5010, "name": "اختبار"}]},
                       reference_price=None, config={"level_retest": True}))["events"]

    def test_breakout_retest_call(self):
        rows = [(5008, 5009, 5007, 5008, 100)] * 4 + [(5008, 5013, 5008, 5012, 100)]   # كسر لأعلى
        rows += [(5012, 5016, 5012, 5015, 100), (5015, 5016, 5013, 5014, 100)]          # ابتعاد
        rows += [(5014, 5014, 5010.5, 5013.5, 100)]  # ستة: هبوط إلى المستوى… ثم شمعة صاعدة
        rows += [(5011, 5014, 5010.5, 5013.8, 100)]
        rows += [(5014 + i, 5015 + i, 5013.5 + i, 5015 + i, 100) for i in range(40)]
        sig = [e for e in self.run_rows(rows) if e["type"] == "signal"]
        self.assertEqual((sig[0]["side"], sig[0]["pattern"]), ("call", "breakout_retest"))

    def test_reclaim_cancels_breakdown(self):
        rows = [(5011, 5012, 5010.5, 5011, 100)] * 4 + [(5011, 5011, 5006, 5007, 100)]  # كسر لأسفل
        rows += [(5007, 5008, 5005, 5006, 100), (5006, 5012, 5006, 5011.5, 100)]          # استعادة بالإغلاق
        rows += [(5011.5, 5011.8, 5008, 5009, 100)] * 3
        sig = [e for e in self.run_rows(rows) if e["type"] == "signal" and e["pattern"] == "breakdown_retest"]
        self.assertEqual(sig, [])


if __name__ == "__main__":
    unittest.main()


class OptionSide(unittest.TestCase):
    def test_put_signal_not_priced_with_call_contract(self):
        from detector import OptionMap, DEFAULTS
        spec = dict(strike=7250, right="call", expiry="2026-12-01T16:00:00-05:00", iv=0.2)
        om = OptionMap(spec, DEFAULTS)
        at = datetime(2026, 12, 1, 10, 0, tzinfo=ET)
        self.assertIsNotNone(om.price(7240, at, "call"))
        self.assertIsNone(om.price(7240, at, "put"))
        self.assertIsNotNone(om.price(7240, at))
