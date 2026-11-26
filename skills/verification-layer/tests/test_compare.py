import json
import sys
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
import compare  # noqa: E402


def load(name):
    return json.loads((HERE / name).read_text(encoding="utf-8"))


class TestCompare(unittest.TestCase):
    def test_agreement_passes(self):
        r = compare.verify(load("sample_agree.json"))
        self.assertEqual(r["decision"], "صالح للعرض")
        self.assertEqual(r["worst_severity"], "مقبول")

    def test_opposite_direction_is_dangerous(self):
        r = compare.verify(load("sample_conflict.json"))
        self.assertEqual(r["worst_severity"], "خطير")
        self.assertTrue(r["decision"].startswith("تعارض"))

    def test_horizon_explains_large_gap(self):
        ops = load("sample_agree.json")[:2]
        ops[1]["targets"] = [125]
        ops[1]["horizon_days"] = 40
        r = compare.verify(ops)
        pair = r["pairs"][0]
        self.assertEqual(pair["size"], "كبير")
        self.assertEqual(pair["logic"], "منطقي")
        self.assertEqual(pair["sign"], "إيجابي")

    def test_broken_stop_is_dangerous(self):
        ops = load("sample_agree.json")[:2]
        ops[1]["stop"] = 105
        r = compare.verify(ops)
        self.assertEqual(r["worst_severity"], "خطير")

    def test_lens_disagreement_needs_review_only(self):
        ops = load("sample_agree.json")
        ops[2]["direction"] = "avoid"
        r = compare.verify(ops)
        self.assertEqual(r["pairs"][1]["severity"], "يحتاج مراجعة")

    def test_red_flag_lens_avoid_is_dangerous(self):
        ops = load("sample_agree.json")
        ops[2]["source"] = "lens:muddy-waters-forensic"
        ops[2]["direction"] = "avoid"
        r = compare.verify(ops)
        self.assertEqual(r["worst_severity"], "خطير")


if __name__ == "__main__":
    unittest.main()
