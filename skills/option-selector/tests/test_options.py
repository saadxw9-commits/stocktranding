import json
import sys
import unittest
from datetime import timedelta
import copy
from pathlib import Path

import yaml

SKILL = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SKILL))
from analyze import compare, stamp


class ContractTest(unittest.TestCase):
    def setUp(self):
        self.data = json.loads((SKILL / "example.json").read_text())
        self.rules = yaml.safe_load((SKILL.parents[1] / "config/risk-rules.yaml").read_text())
        self.now = stamp(self.data["as_of"])

    def run_case(self):
        return compare(self.data, self.rules, self.now)

    def test_example(self):
        report = self.run_case()
        self.assertEqual(report["decision"], "WAIT")
        self.assertEqual(report["atm_straddle"]["settlement_breakevens"], [7237.5, 7282.5])
        self.assertEqual(report["contracts"][0]["pnl_usd"]["target"], 1770)
        self.assertEqual(report["contracts"][0]["breakeven"], 7272.3)
        self.assertEqual(report["contracts"][-1]["pnl_usd"]["target"], -470)

    def test_put(self):
        self.data.update(side="put", target=7200, invalidation=7270,
                         risk_budget_usd=2000, available_cash_usd=3000)
        self.rules.update(capital_usd=100000, allow_options=False)
        self.assertEqual(self.run_case()["reason"], "options_disabled")
        self.rules["allow_options"] = True
        report = self.run_case()
        self.assertEqual(report["decision"], "CANDIDATE")
        self.assertEqual(report["strike"], 7260)
        self.assertEqual(report["contracts"][0]["breakeven"], 7249.5)

    def test_before_settlement(self):
        self.data["exit_at"] = (self.now + timedelta(hours=1)).isoformat()
        self.assertEqual(self.run_case()["valuation"], "model_constant_iv")
        self.assertGreater(self.run_case()["contracts"][-1]["pnl_usd"]["target"], -470)

    def test_ended(self):
        self.now = stamp(self.data["last_trade_at"])
        self.assertEqual(self.run_case()["reason"], "trading_ended")

    def test_stale(self):
        self.now += timedelta(minutes=2)
        self.assertEqual(self.run_case()["reason"], "stale_or_nonlive_snapshot")

    def test_full_premium_budget(self):
        # عقد بعلاوة 831$ يمر برأس مال 1000$ بعد إلغاء حد 2%
        self.data.update(spot=7216, target=7290, invalidation=7205,
                         risk_budget_usd=1000, available_cash_usd=1000)
        self.data["quotes"] = [dict(strike=7250, right="call", bid=8.2, ask=8.3, iv=0.417,
                                    open_interest=3000, volume=900, quoted_at=self.data["as_of"])]
        report = self.run_case()
        self.assertEqual(report["budget_usd"], 1000)
        self.assertEqual(report["decision"], "CANDIDATE")

    def test_stale_atm_leg(self):
        self.data["quotes"][2]["quoted_at"] = (self.now - timedelta(minutes=2)).isoformat()
        self.assertNotIn("atm_straddle", self.run_case())


class UniverseTest(unittest.TestCase):
    """قواعد الأصول من config/options-universe.yaml."""

    def setUp(self):
        self.base = json.loads((SKILL / "example.json").read_text())
        self.rules = yaml.safe_load((SKILL.parents[1] / "config/risk-rules.yaml").read_text())
        self.rules.update(capital_usd=100000)
        self.now = stamp(self.base["as_of"])

    def etf(self, name="QQQ", **kw):
        d = copy.deepcopy(self.base)
        d.update(underlying=name, series=name, spot=600, target=607, invalidation=597,
                 risk_budget_usd=5000, available_cash_usd=5000, horizon="intraday",
                 last_trade_at="2026-11-27T13:00:00-05:00", settlement_at="2026-11-27T13:00:00-05:00",
                 exit_at="2026-11-27T12:00:00-05:00", iv_rank=40)
        d["quotes"] = [dict(strike=600, right="call", bid=2.0, ask=2.04, iv=0.2, open_interest=8000,
                            volume=3000, quoted_at=d["as_of"])]
        d.update(kw)
        return d

    def run_case(self, d):
        return compare(d, self.rules, self.now)

    def test_qqq_candidate_with_close_cutoff(self):
        r = self.run_case(self.etf())
        self.assertEqual(r["decision"], "CANDIDATE")
        self.assertEqual(r["settlement"], "physical")
        self.assertEqual(r["must_close_by"], "2026-11-27T12:50:00-05:00")
        self.assertEqual(r["contracts"][0]["moneyness"], "atm")

    def test_physical_hold_to_expiry_refused(self):
        r = self.run_case(self.etf(exit_at="2026-11-27T13:00:00-05:00"))
        self.assertEqual(r["reason"], "physical_settlement_exit_after_cutoff")

    def test_ewz_intraday_refused(self):
        r = self.run_case(self.etf("EWZ"))
        self.assertEqual(r["reason"], "horizon_not_allowed")

    def test_unknown_underlying(self):
        with self.assertRaises(ValueError):
            self.run_case(self.etf("AAPL"))

    def test_tsla_earnings_window(self):
        d = self.etf("TSLA", horizon="swing", earnings_date="2026-11-27T11:30:00-05:00")
        self.assertEqual(self.run_case(d)["reason"], "earnings_in_window")
        d.pop("earnings_date")
        self.assertIn("earnings_date_unknown", self.run_case(d)["warnings"])

    def test_iv_expensive(self):
        self.assertEqual(self.run_case(self.etf(iv_rank=85))["reason"], "iv_expensive_vs_history")

    def test_liquidity_and_delta(self):
        d = self.etf()
        d["quotes"][0].update(open_interest=10, volume=5)
        d["quotes"].append(dict(strike=640, right="call", bid=0.01, ask=0.02, iv=0.2,
                                open_interest=9000, volume=9000, quoted_at=d["as_of"]))
        r = self.run_case(d)
        self.assertIn("low_open_interest", r["contracts"][0]["rejected"])
        self.assertIn("delta_outside_band", r["contracts"][1]["rejected"])

    def test_xsp_scaled_from_spx(self):
        d = copy.deepcopy(self.base)
        d.update(underlying="XSP", series="XSP", levels_from="SPX", spot=7226, target=7250,
                 invalidation=7210.46, risk_budget_usd=2000, available_cash_usd=2000)
        d["quotes"] = [dict(strike=725, right="call", bid=0.9, ask=0.95, iv=0.417, open_interest=400,
                            volume=300, quoted_at=d["as_of"])]
        r = self.run_case(d)
        self.assertEqual(r["scaled_levels"]["spot"], 722.6)
        self.assertEqual(r["contracts"][0]["max_loss_usd"], 95)

    def test_cheap_mode(self):
        # عقد QQQ 645 عند إشارة 10:07 في 27 نوفمبر (أرقام تقديرية)
        d = self.etf(spot=639, target=650, invalidation=636.5, exit_at="2026-11-27T12:50:00-05:00")
        d["quotes"] = [dict(strike=645, right="call", bid=0.42, ask=0.47, iv=0.5, open_interest=4290,
                            volume=20000, quoted_at=d["as_of"])]
        std = self.run_case(d)
        self.assertIn("delta_outside_band", std["contracts"][0]["rejected"])
        d["mode"] = "cheap"
        self.assertEqual(self.run_case(d)["reason"], "cheap_mode_requires_signal")
        d["signal"] = {"pattern": "higher_low", "at": "2026-11-27T10:06:00-05:00"}
        r = self.run_case(d)
        self.assertEqual(r["decision"], "CANDIDATE")
        self.assertEqual(r["budget_usd"], 100)
        self.assertGreaterEqual(r["contracts"][0]["payoff_multiple"], 3)
        d["target"] = 646
        self.assertIn("cheap_payoff", self.run_case(d)["contracts"][0]["rejected"])
