import copy
import json
import sys
import unittest
from pathlib import Path

import yaml

SKILL = Path(__file__).resolve().parents[1]
ROOT = SKILL.parents[1]
sys.path.insert(0, str(SKILL))
from bridge import plan, select  # noqa: E402
from analyze import load_universe, stamp  # noqa: E402

FIX = SKILL / "fixtures"


class BridgeTest(unittest.TestCase):
    def setUp(self):
        self.u = load_universe()
        self.ver = json.loads((FIX / "verify-TSLA-synthetic.json").read_text(encoding="utf-8"))
        self.chain = json.loads((FIX / "chain-TSLA-synthetic.json").read_text(encoding="utf-8"))
        self.rules = yaml.safe_load((ROOT / "config/risk-rules.yaml").read_text(encoding="utf-8"))
        self.rules.update(capital_usd=100000, min_reward_risk=1.0)
        self.now = stamp(self.chain["as_of"])

    def run_select(self, pl=None, chain=None, **kw):
        return select(pl or plan(self.ver, self.u), chain or self.chain, self.rules, self.u,
                      kw.get("cash", 5000), kw.get("budget", 3000), 0, kw.get("mode", "standard"), self.now)

    def test_plan(self):
        pl = plan(self.ver, self.u)
        self.assertEqual((pl["status"], pl["side"], pl["horizon"]), ("ready", "call", "swing"))
        self.assertEqual(pl["exit_at"], "2026-12-11T15:30:00-05:00")   # عشرة أيام تداول
        self.assertEqual(pl["expiry_window"], {"min_dte": 28, "max_dte": 73})
        self.assertIn("earnings_date", pl["needs"])

    def test_select_best_expiry(self):
        r = self.run_select()
        self.assertEqual(r["decision"], "CANDIDATE")
        reasons = {x["expiry"]: x.get("reason") for x in r["expiries"]}
        self.assertEqual(reasons["2026-12-11"], "outside_expiry_window")
        self.assertEqual(reasons["2027-03-19"], "outside_expiry_window")
        self.assertIn(r["expiry"], ("2026-12-31", "2027-01-15"))
        self.assertLessEqual(r["contract"]["max_loss_usd"], 3000)

    def test_reward_risk_with_early_stop(self):
        # بشرط الضعفين: هدف 432 لا يكفي لتغطية تآكل العقد، وهدف أبعد يكفي
        self.rules["min_reward_risk"] = 2.0
        self.assertEqual(self.run_select()["decision"], "WAIT")
        pl = plan(self.ver, self.u)
        pl["target"] = 470
        r = self.run_select(pl=pl)
        self.assertEqual(r["decision"], "CANDIDATE")
        self.assertGreaterEqual(r["contract"]["reward_risk"], 2.0)

    def test_budget_blocks(self):
        r = self.run_select(budget=500)
        self.assertEqual(r["decision"], "WAIT")

    def test_wait_for_entry(self):
        ch = copy.deepcopy(self.chain)
        ch["spot"] = 390
        self.assertEqual(self.run_select(chain=ch)["reason"], "wait_for_entry")
        ch["spot"] = 385
        self.assertEqual(self.run_select(chain=ch)["reason"], "invalidated")

    def test_blocked_decisions(self):
        v = copy.deepcopy(self.ver)
        v["decision"] = "تعارض — لا يُعرض كاقتراح"
        self.assertEqual(plan(v, self.u)["reason"], "verification_not_ready")
        v = copy.deepcopy(self.ver)
        v["symbol"] = "AAPL"
        self.assertEqual(plan(v, self.u)["reason"], "not_in_options_universe")

    def test_short_maps_to_put(self):
        v = copy.deepcopy(self.ver)
        v["primary"]["direction"] = "short"
        v["conservative_levels"] = {"entry": 400, "stop": 412, "target": 370, "reward_risk": 2.5}
        self.assertEqual(plan(v, self.u)["side"], "put")

    def test_pending_owner(self):
        v = copy.deepcopy(self.ver)
        v["decision"] = "اقتراح معلّق — يحتاج مراجعتك"
        r = self.run_select(pl=plan(v, self.u))
        self.assertEqual(r["decision"], "PENDING_OWNER")

    def test_ewz_intraday_blocked(self):
        v = copy.deepcopy(self.ver)
        v["symbol"] = "EWZ"
        v["primary"]["horizon_days"] = 0
        self.assertEqual(plan(v, self.u)["reason"], "horizon_not_allowed")


if __name__ == "__main__":
    unittest.main()
