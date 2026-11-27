import json
import sqlite3
import sys
import unittest
from datetime import datetime, timezone
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "skills" / "journal"))
sys.path.insert(0, str(ROOT / "skills" / "option-bridge"))
import shadow  # noqa: E402
from bridge import plan, select  # noqa: E402
from analyze import load_universe, stamp  # noqa: E402

FIX = ROOT / "skills" / "option-bridge" / "fixtures"
LATER = datetime(2026, 12, 12, tzinfo=timezone.utc)


def mem():
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    c.executescript(shadow.DDL)
    return c


class ShadowTest(unittest.TestCase):
    def bridge_report(self, target=470):
        u = load_universe()
        rules = yaml.safe_load((ROOT / "config/risk-rules.yaml").read_text(encoding="utf-8"))
        pl = plan(json.loads((FIX / "verify-TSLA-synthetic.json").read_text(encoding="utf-8")), u)
        pl["target"] = target
        ch = json.loads((FIX / "chain-TSLA-synthetic.json").read_text(encoding="utf-8"))
        return select(pl, ch, rules, u, 7000, 3500, 0.65, "standard", stamp(ch["as_of"]))

    def test_add_bridge_report(self):
        c = mem()
        r = self.bridge_report()
        n = shadow.add(r, 0.65, c)
        self.assertEqual(n, 12)                       # ستة أسعار تنفيذ × تاريخا انتهاء داخل النافذة
        sel = list(c.execute("SELECT contract_key FROM option_decisions WHERE selected=1"))
        self.assertEqual([x[0] for x in sel], [f"TSLA|{r['expiry']}|{r['contract']['strike']:g}|call"])
        self.assertEqual(shadow.add(r, 0.65, c), 0)   # لا تكرار

    def test_resolve_and_pending(self):
        c = mem()
        shadow.add(self.bridge_report(), 0.65, c)
        keys = shadow.pending(LATER, c)
        self.assertIn("TSLA|2026-12-31|390|call", keys)
        self.assertEqual(shadow.pending(datetime(2026, 12, 1, tzinfo=timezone.utc), c), [])
        self.assertEqual(shadow.resolve({"TSLA|2026-12-31|390|call": 50.0}, LATER, c), 1)
        row = c.execute("SELECT pnl_usd, return_pct FROM option_decisions WHERE contract_key=?",
                        ("TSLA|2026-12-31|390|call",)).fetchone()
        self.assertEqual(row["pnl_usd"], round((50.0 - 33.45) * 100 - 0.65, 2))

    def test_stats_verdicts(self):
        c = mem()
        def put(i, status, reasons, ret):
            c.execute("INSERT INTO option_decisions(created_at,contract_key,underlying,expiry,strike,right_,mode,"
                      "horizon,ask,status,reasons,exit_at,exit_price,pnl_usd,return_pct,resolved_at) "
                      "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                      (f"2026-11-{i % 28 + 1:02d}T10:00:00-05:00", f"K{i}", "QQQ", "2026-12-31", 600 + i, "call",
                       "standard", "swing", 5.0, status, reasons, "2026-12-01T15:30:00-05:00", 1, 1, ret, "x"))
        for i in range(40):
            put(i, "eligible", "", 10.0)
            put(100 + i, "rejected", "wide_spread", 12.0)        # رُفضت وأدّت أفضل
            put(200 + i, "rejected", "scenario_reward_risk", -40.0)
        s = shadow.stats(30, c)["standard"]
        self.assertTrue(s["filters"]["wide_spread"]["verdict"].startswith("يستحق المراجعة"))
        self.assertTrue(s["filters"]["scenario_reward_risk"]["verdict"].startswith("نافع"))
        self.assertEqual(shadow.stats(100, c)["standard"]["filters"]["wide_spread"]["verdict"], "عينة صغيرة")


class IntradayStatsTest(unittest.TestCase):
    def test_group_by_zone_kind(self):
        c = sqlite3.connect(":memory:")
        c.row_factory = sqlite3.Row
        c.executescript((ROOT / "db" / "schema.sql").read_text(encoding="utf-8"))
        rows = [("failed_breakdown", "الحد السفلي للحركة المتوقعة اليومية (1σ)", 2.0),
                ("failed_breakdown", "الحد السفلي للحركة المتوقعة الأسبوعية (1σ)", -1.0),
                ("failed_breakdown", "قاع الجلسة السابق", 1.5)]
        for pat, zone, r in rows:
            c.execute("INSERT INTO intraday_signals(created_at,symbol,side,zone,entry,result_r,mode,pattern,"
                      "payload_json) VALUES('t','SPX','call',?,1,?,'paper',?,'{}')", (zone, r, pat))
        out = {x["zone_kind"]: x for x in shadow.intraday_stats(2, c)}
        self.assertEqual(out["الحركة المتوقعة"]["n"], 2)
        self.assertEqual(out["الحركة المتوقعة"]["avg_r"], 0.5)
        self.assertTrue(out["الحركة المتوقعة"]["enough"])
        self.assertFalse(out["طرف الجلسة"]["enough"])


if __name__ == "__main__":
    unittest.main()
