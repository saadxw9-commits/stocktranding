import copy
import json
import sys
import unittest
from pathlib import Path

SKILL = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SKILL))
sys.path.insert(0, str(SKILL.parent / "reversal-detector"))
from positioning import analyze, max_pain, seller_flow, to_markdown, to_plan  # noqa: E402


class PositioningTest(unittest.TestCase):
    def setUp(self):
        self.snap = json.loads((SKILL / "fixtures/synthetic-spxw-2026-11-30.json").read_text(encoding="utf-8"))

    def test_walls(self):
        r = analyze(self.snap)
        self.assertEqual(r["put_walls"][0]["strike"], 7250)
        self.assertEqual(r["call_walls"][0]["strike"], 7350)

    def test_put_selling_gives_call_bias(self):
        r = analyze(self.snap)
        self.assertEqual(r["bias"]["direction"], "call")
        self.assertEqual(r["bias"]["certainty"], "استنتاج")
        self.assertIn("seller_flow", [v["factor"] for v in r["bias"]["votes"]])

    def test_call_selling_gives_put_bias(self):
        s = copy.deepcopy(self.snap)
        for q in s["chain"]:   # اعكس: بيع كثيف في Call قريب، وشراء Put
            if q["right"] == "call" and q["strike"] in (7300, 7325):
                q.update(volume_at_bid=5000, volume_at_ask=50, open_interest=12000)
            if q["right"] == "put":
                q.update(volume_at_bid=20, volume_at_ask=2000)
                if q["strike"] == 7250:
                    q["open_interest"] = 800
        r = analyze(s)
        self.assertEqual(r["bias"]["direction"], "put")

    def test_oi_change_fallback(self):
        self.assertEqual(seller_flow(dict(oi_change=500, price_change=-0.4))["basis"], "oi_up_price_down")
        self.assertEqual(seller_flow(dict(oi_change=500, price_change=0.4))["net_sold"], -500)
        self.assertIsNone(seller_flow({}))

    def test_no_flow_warns(self):
        s = copy.deepcopy(self.snap)
        for q in s["chain"]:
            q.pop("volume_at_bid"); q.pop("volume_at_ask")
        self.assertIn("no_seller_flow_data", analyze(s)["warnings"])

    def test_max_pain_simple(self):
        # تسوية 100: 10×10=100، تسوية 105: 50+150=200، تسوية 110: 0+300=300، فالألم الأقصى 100
        rows = [dict(strike=110, right="put", open_interest=10), dict(strike=100, right="call", open_interest=30),
                dict(strike=105, right="call", open_interest=0)]
        self.assertEqual(max_pain(rows), 100)

    def test_gamma_regime_and_flip(self):
        r = analyze(self.snap)
        self.assertIsNotNone(r["zero_gamma"])
        self.assertIn(r["gamma_regime"], ("positive_gamma", "negative_gamma"))

    def test_plan_feeds_detector(self):
        from detector import add_external_levels
        plan = to_plan(analyze(self.snap))
        data = add_external_levels({"levels": {}}, plan)
        self.assertTrue(any("جدار Put" in c["name"] for c in data["levels"]["custom"]))
        self.assertIn("تموضع البائعين", to_markdown(analyze(self.snap)))


if __name__ == "__main__":
    unittest.main()
