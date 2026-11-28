import sys
import unittest
from pathlib import Path

import pandas as pd

SKILL = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SKILL))
from swing_shares import RULES, simulate  # noqa: E402


def series(closes, start="2020-01-01"):
    idx = pd.bdate_range(start, periods=len(closes))
    c = pd.Series(closes, index=idx, dtype=float)
    return pd.DataFrame(dict(Open=c.shift().fillna(c.iloc[0]), High=c + 1, Low=c - 1, Close=c))


class Shares(unittest.TestCase):
    def test_long_call_signal_sized_within_budget(self):
        closes = [400 + i for i in range(260)]
        closes += [closes[-1] - i * 3 for i in range(1, 8)] + [closes[-1] - 18 + i * 4 for i in range(1, 20)]
        tr = simulate(series(closes), None, "QQQ", "S1", "2020-01-01", "2030-01-01")
        self.assertTrue(tr)
        self.assertEqual(tr[0]["instrument"], "QQQ")
        self.assertLessEqual(tr[0]["invested"], RULES["budget"])
        self.assertGreater(tr[0]["shares"], 0)

    def test_bearish_uses_inverse_etf_not_short(self):
        dn = [600 - i for i in range(260)] + [339 - 3 * i for i in range(1, 15)]
        px = series(dn)
        inv = series([20 + 0.03 * i for i in range(len(dn))])
        tr = simulate(px, inv, "QQQ", "S2", "2020-01-01", "2030-01-01")
        self.assertTrue(tr)
        self.assertEqual({t["instrument"] for t in tr}, {"PSQ"})
        self.assertTrue(all(t["shares"] > 0 for t in tr))        # شراء فقط
        self.assertGreater(tr[0]["ret_pct"], 0)                  # الهبوط ربح للصندوق العكسي

    def test_no_trade_without_signal(self):
        px = series([100 + i * 0.5 for i in range(300)])
        self.assertEqual(simulate(px, None, "SPY", "S1", "2020-01-01", "2030-01-01"), [])


if __name__ == "__main__":
    unittest.main()
