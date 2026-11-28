import math
import sys
import unittest
from pathlib import Path

import pandas as pd

SKILL = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SKILL))
from swing_test import RULES, bs, choose_contract, delta, indicators, signals, simulate, verdict  # noqa: E402


def series(closes, start="2020-01-01"):
    idx = pd.bdate_range(start, periods=len(closes))
    c = pd.Series(closes, index=idx, dtype=float)
    return pd.DataFrame(dict(Open=c.shift().fillna(c.iloc[0]), High=c + 2, Low=c - 2, Close=c))


def flat_iv(px, level=20.0):
    return pd.DataFrame(dict(Open=level, High=level, Low=level, Close=level), index=px.index)


class Pricing(unittest.TestCase):
    def test_put_call_parity(self):
        S, K, T, iv, r = 600.0, 610.0, 35 / 365, 0.22, RULES["rate"]
        lhs = bs(S, K, T, iv, "call") - bs(S, K, T, iv, "put")
        self.assertAlmostEqual(lhs, S - K * math.exp(-r * T), places=6)

    def test_choose_contract_fits_budget(self):
        K, ask, qty = choose_contract(730.0, 0.20, "call", 1.0, 0.013)
        self.assertLessEqual(qty * (ask * 100 + RULES["fee"]), RULES["budget"])
        self.assertGreaterEqual(abs(delta(730.0, K, RULES["dte"] / 365, 0.20, "call", q=0.013)), RULES["delta_min"])

    def test_spx_too_big_for_budget(self):
        # عقد SPX كامل لا يتسع له 1800$ حتى عند دلتا 0.30
        self.assertIsNone(choose_contract(7300.0, 0.20, "call", 5.0, 0.013))


class Signals(unittest.TestCase):
    def test_uptrend_pullback_gives_call(self):
        up = [100 + i * 0.5 for i in range(260)]
        dip = [up[-1] - i * 1.5 for i in range(1, 8)] + [up[-1] - 9 + i * 2 for i in range(1, 10)]
        d = indicators(series(up + dip))
        s = signals(d, "S1").dropna()
        self.assertIn("call", set(s))
        self.assertNotIn("put", set(s))

    def test_downtrend_breakdown_gives_put(self):
        dn = [300 - i * 0.5 for i in range(260)] + [169 - i for i in range(5)]
        s = signals(indicators(series(dn)), "S2").dropna()
        self.assertEqual(set(s), {"put"})


class Simulation(unittest.TestCase):
    def test_no_trade_without_signal(self):
        # يوم بلا إشارة يُخزَّن NaN لا None؛ لا يجوز أن يُعامل إشارة (خطأ اكتُشف قبل الدمج)
        px = series([100 + i * 0.5 for i in range(300)])     # صعود مستمر بلا عبور لمتوسط 20
        self.assertTrue(signals(indicators(px), "S1").dropna().empty)
        self.assertEqual(simulate(px, flat_iv(px), "QQQ", "S1", "2020-01-01", "2030-01-01"), [])

    def test_trade_and_verdict(self):
        closes = [100 + i * 0.5 for i in range(260)]
        closes += [closes[-1] - i * 1.5 for i in range(1, 8)] + [closes[-1] - 9 + i * 2 for i in range(1, 30)]
        px = series(closes)
        tr = [t for t in simulate(px * 2.6, flat_iv(px), "QQQ", "S1", "2020-01-01", "2030-01-01") if "pnl" in t]   # نحو 600$ مثل QQQ
        self.assertTrue(tr)
        self.assertLessEqual(tr[0]["cost"], RULES["budget"])
        df = pd.DataFrame([dict(t, period="P1") for t in tr])
        v = verdict(df)
        self.assertFalse(v["QQQ S1"]["accepted"])   # فترة واحدة وعدد قليل: لا قبول


if __name__ == "__main__":
    unittest.main()
