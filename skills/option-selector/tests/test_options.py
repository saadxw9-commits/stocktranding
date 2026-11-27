import json
import sys
import unittest
from datetime import timedelta
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
        self.rules.update(capital_usd=100000)
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

    def test_stale_atm_leg(self):
        self.data["quotes"][2]["quoted_at"] = (self.now - timedelta(minutes=2)).isoformat()
        self.assertNotIn("atm_straddle", self.run_case())
