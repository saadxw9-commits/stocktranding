import csv, sys, tempfile, unittest, random
from datetime import datetime, timedelta, timezone
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import sim


def synth(path, days=3, base=7200.0, seed=1):
    rnd = random.Random(seed)
    rows, px = [], base
    d = datetime(2026, 11, 23, tzinfo=timezone.utc)
    n = 0
    while n < days:
        if d.weekday() < 5:
            for sec in range(0, 1800):
                t = d.replace(hour=14, minute=30) + timedelta(seconds=sec)   # 09:30 نيويورك شتاءً
                o = px; px += rnd.uniform(-0.4, 0.4); c = px
                rows.append((t.isoformat().replace("+00:00", "Z"), o, max(o, c) + .1, min(o, c) - .1, c, 100))
            n += 1
        d += timedelta(days=1)
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f); w.writerow(["ts_event", "open", "high", "low", "close", "volume"]); w.writerows(rows)


class T(unittest.TestCase):
    def test_ny_offset(self):
        self.assertEqual(sim.ny_offset(datetime(2026, 11, 27, 15, tzinfo=timezone.utc)).total_seconds(), -5 * 3600)
        self.assertEqual(sim.ny_offset(datetime(2026, 7, 1, 15, tzinfo=timezone.utc)).total_seconds(), -4 * 3600)
        self.assertEqual(sim.ny_offset(datetime(2026, 11, 1, 5, 59, tzinfo=timezone.utc)).total_seconds(), -4 * 3600)
        self.assertEqual(sim.ny_offset(datetime(2026, 11, 1, 6, 0, tzinfo=timezone.utc)).total_seconds(), -5 * 3600)

    def test_sessions_and_replay(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "x.csv"; synth(p)
            bars = sim.load_csv(p)
            s = sim.split_sessions(bars)
            self.assertEqual(len(s), 3)
            self.assertEqual(s[0]["rth"][0]["t"].hour, 9)
            agg, res = sim.replay(bars, "TEST", scale=1.0, out=Path(d) / "o")
            self.assertEqual(agg["days"], 2)          # الجلسة الأولى سابقة فقط
            self.assertFalse(agg["enough_evidence"])
            self.assertEqual(len(list((Path(d) / "o").glob("*.json"))), 2)

    def test_fetch_requires_key(self):
        import os
        os.environ.pop("DATABENTO_API_KEY", None)
        with self.assertRaises(SystemExit):
            sim.fetch("X", "Y", "raw_symbol", "2026-01-01", "2026-01-02", Path("/tmp/x.csv"))


class Calendar(unittest.TestCase):
    def _bar(self, iso, px=7200.0):
        t = sim.to_ny(datetime.fromisoformat(iso).astimezone(timezone.utc))
        return dict(t=t, o=px, h=px + .5, l=px - .5, c=px, v=10)

    def test_holiday_is_not_a_session(self):
        # العقود الآجلة تتداول صباح عيد الشكر؛ يجب ألا تصير جلسة
        bars = [self._bar("2026-11-25T15:00:00+00:00"), self._bar("2026-11-26T15:00:00+00:00"),
                self._bar("2026-11-27T15:00:00+00:00")]
        s = sim.split_sessions(bars)
        self.assertEqual([x["date"].isoformat() for x in s], ["2026-11-25", "2026-11-27"])
        self.assertEqual(len(s[1]["overnight"]), 1)

    def test_early_close(self):
        bars = [self._bar("2026-11-25T15:00:00+00:00"), self._bar("2026-11-27T15:00:00+00:00"),
                self._bar("2026-11-27T18:30:00+00:00")]          # 13:30 نيويورك بعد الإغلاق المبكر
        s = sim.split_sessions(bars)
        self.assertEqual(len(s[1]["rth"]), 1)
        ctx = sim.build_context(s[0], s[1], "SPX", 1.0, 0.5)
        self.assertTrue(ctx["session_close"].startswith("2026-11-27T13:00"))
        self.assertTrue(sim.build_context(s[0], dict(rth=s[0]["rth"], overnight=[]), "SPX", 1.0, 0.5)
                        ["session_close"].startswith("2026-11-25T16:00"))


class Scale(unittest.TestCase):
    def test_known_and_unknown(self):
        self.assertEqual(sim.resolve_scale("SPY", None), 10.0)
        self.assertEqual(sim.resolve_scale("spx", None), 1.0)
        self.assertEqual(sim.resolve_scale("TSLA", 6.2), 6.2)
        with self.assertRaises(SystemExit):
            sim.resolve_scale("TSLA", None)


class Options(unittest.TestCase):
    def test_symbols_and_expiry(self):
        from datetime import date
        self.assertEqual(sim.option_symbol("QQQ", date(2026, 5, 26), "call", 525), "QQQ   260526C00525000")
        self.assertEqual(sim.option_symbol("SPXW", date(2026, 11, 27), "call", 7250), "SPXW  261127C07250000")
        self.assertEqual(sim.option_symbol("TSLA", date(2026, 11, 27), "put", 402.5), "TSLA  261127P00402500")
        self.assertEqual(sim.expiry_for("QQQ", date(2026, 11, 25)), date(2026, 11, 25))
        self.assertEqual(sim.expiry_for("TSLA", date(2026, 11, 25)), date(2026, 11, 27))
        self.assertEqual(sim.expiry_for("TSLA", date(2026, 4, 1)), date(2026, 4, 2))   # الجمعة العظيمة عطلة

    def test_price_trade_fallback_and_pnl(self):
        hdr = "ts_recv,bid_px_00,ask_px_00\n"
        calls = []

        def fake(endpoint, key, **p):
            calls.append(p["symbols"])
            if p["symbols"].endswith("00500000"):      # السعر الأقرب غير مدرج
                raise sim.DatabentoError(422, '{"detail":{"case":"symbology_invalid_request","message":"x"}}')
            return (hdr + "2026-06-01T14:00:00.000000000Z,1.00,1.02\n"
                          "2026-06-01T14:05:00.000000000Z,1.50,1.52\n"
                          "2026-06-01T14:10:00.000000000Z,2.00,2.02\n").encode()
        orig, sim._post = sim._post, fake
        try:
            t = dict(date="2026-06-01", side="call", entry=500.2, opened="2026-06-01T10:00:10-04:00",
                     fills=[dict(at="2026-06-01T10:05:30-04:00", qty=0.5), dict(at="2026-06-01T10:10:30-04:00", qty=0.5)],
                     pattern="failed_breakdown", r=1.0, risk=0.5)
            p = sim.price_trade(t, "QQQ", 1.0, "k")
        finally:
            sim._post = orig
        self.assertEqual(calls[0], "QQQ   260601C00500000")
        self.assertEqual(p["strike"], 501.0)
        self.assertAlmostEqual(p["pnl_per_contract"], (0.5 * (1.50 - 1.02) + 0.5 * (2.00 - 1.02)) * 100)

    def test_unscaled_report_rejected(self):
        import json
        with tempfile.TemporaryDirectory() as d:
            (Path(d) / "x.json").write_text(json.dumps(dict(date="2026-06-01", events=[])), encoding="utf-8")
            with self.assertRaises(SystemExit):
                sim.load_trades(Path(d))

    def test_account_skips_trades_over_risk(self):
        rows = [dict(date="2026-06-01", pattern="a", risk=0.5, ask_in=1.02, bid_in=1.00, pnl_per_contract=50.0),
                dict(date="2026-06-02", pattern="a", risk=5.0, ask_in=6.1, bid_in=6.0, pnl_per_contract=-200.0)]
        acc = sim.account(rows, capital=7000, risk_usd=70, fee=0.65)
        # الأولى: خسارة متوقعة 0.5×0.5×100+2 = 27$ → عقدان؛ الثانية: 250$ للعقد > 70$ → تُتخطّى
        self.assertEqual(acc["taken"], 1)
        self.assertEqual(acc["skipped_risk"], 1)
        self.assertAlmostEqual(acc["net"], 2 * 50 - 2 * 0.65 * 2)


if __name__ == "__main__":
    unittest.main()
