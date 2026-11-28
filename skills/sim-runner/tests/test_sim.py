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
            agg, res = sim.replay(bars, "TEST", out=Path(d) / "o")
            self.assertEqual(agg["days"], 2)          # الجلسة الأولى سابقة فقط
            self.assertFalse(agg["enough_evidence"])
            self.assertEqual(len(list((Path(d) / "o").glob("*.json"))), 2)

    def test_fetch_requires_key(self):
        import os
        os.environ.pop("DATABENTO_API_KEY", None)
        with self.assertRaises(SystemExit):
            sim.fetch("X", "Y", "raw_symbol", "2026-01-01", "2026-01-02", Path("/tmp/x.csv"))


if __name__ == "__main__":
    unittest.main()
