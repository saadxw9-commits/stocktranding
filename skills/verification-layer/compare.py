#!/usr/bin/env python3
"""طبقة التحقق: تقارن رأي المصدر الأساسي بآراء الرديف والعدسات، وتقيس التفاوت وتصنّفه.

الاستخدام:
    python3 compare.py opinions.json                 # تقرير عربي على الشاشة
    python3 compare.py opinions.json --json          # النتيجة بصيغة JSON
    python3 compare.py opinions.json --out reports/  # يحفظ التقرير والنتيجة

الملف المدخل: قائمة آراء بصيغة schemas/opinion.schema.json لأصل واحد، فيها رأي أساسي واحد.
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]
SEVERITY_ORDER = {"مقبول": 0, "يحتاج مراجعة": 1, "خطير": 2}
SIZE_ORDER = {"صغير": 0, "متوسط": 1, "كبير": 2}
BULLISH = {"long": 1, "neutral": 0, "avoid": -1, "short": -1}


def load_cfg() -> tuple[dict, dict, set]:
    ver = yaml.safe_load((ROOT / "config" / "verification.yaml").read_text(encoding="utf-8"))
    risk = yaml.safe_load((ROOT / "config" / "risk-rules.yaml").read_text(encoding="utf-8"))
    lenses = yaml.safe_load((ROOT / "config" / "lenses.yaml").read_text(encoding="utf-8"))
    red = {f"lens:{n}" for n in lenses.get("red_flags", [])}
    return ver, risk, red


# ---------- فحص الرأي الواحد ----------

def internal_issues(op: dict) -> list[str]:
    """أخطاء منطقية داخل الرأي نفسه."""
    issues = []
    d, e, s, t = op["direction"], op.get("entry"), op.get("stop"), op.get("targets") or []
    if d in ("long", "short"):
        if e is None:
            issues.append("لا يوجد سعر دخول")
        if s is None:
            issues.append("لا يوجد وقف خسارة")
        if not t:
            issues.append("لا يوجد هدف")
    if e is not None and s is not None:
        if d == "long" and s >= e:
            issues.append("وقف الخسارة أعلى من سعر الدخول أو يساويه في صفقة شراء")
        if d == "short" and s <= e:
            issues.append("وقف الخسارة أدنى من سعر الدخول في صفقة بيع")
    if e is not None and t:
        if d == "long" and min(t) <= e:
            issues.append("هدف أدنى من سعر الدخول في صفقة شراء")
        if d == "short" and max(t) >= e:
            issues.append("هدف أعلى من سعر الدخول في صفقة بيع")
    return issues


def reward_risk(op: dict) -> float | None:
    e, s, t = op.get("entry"), op.get("stop"), op.get("targets") or []
    if e is None or s is None or not t or e == s:
        return None
    return round(abs(t[0] - e) / abs(e - s), 2)


def pct_gap(a: float | None, b: float | None) -> float | None:
    if a is None or b is None or a == 0:
        return None
    return round(abs(a - b) / abs(a) * 100, 2)


def size_of(value: float | None, small: float, large: float) -> str:
    if value is None:
        return "صغير"
    if value < small:
        return "صغير"
    if value >= large:
        return "كبير"
    return "متوسط"


# ---------- مقارنة رأيين ----------

def compare_pair(p: dict, o: dict, ver: dict, risk: dict, red: set) -> dict:
    res: dict = {"source": o["source"], "role": o["role"], "notes": []}

    # الاتجاه
    bp, bo = BULLISH[p["direction"]], BULLISH[o["direction"]]
    if p["direction"] == o["direction"]:
        rel = "اتفاق"
    elif bp * bo < 0:
        rel = "تعاكس"
    else:
        rel = "اتفاق جزئي"
    res["direction"] = {"primary": p["direction"], "other": o["direction"], "relation": rel}

    # المستويات
    gaps = {
        "entry_pct": pct_gap(p.get("entry"), o.get("entry")),
        "stop_pct": pct_gap(p.get("stop"), o.get("stop")),
        "target_pct": pct_gap((p.get("targets") or [None])[0], (o.get("targets") or [None])[0]),
    }
    res["levels_gap"] = gaps
    level_values = [g for g in gaps.values() if g is not None]
    max_level_gap = max(level_values) if level_values else None
    lg = ver["levels_gap_pct"]
    level_size = size_of(max_level_gap, lg["small"], lg["large"])

    conf_gap = round(abs(p["confidence"] - o["confidence"]), 1)
    cg = ver["confidence_gap_points"]
    conf_size = size_of(conf_gap, cg["small"], cg["large"])
    res["confidence_gap"] = conf_gap

    if rel == "تعاكس":
        size = "كبير"
    elif rel == "اتفاق جزئي":
        size = max(level_size, conf_size, "متوسط", key=SIZE_ORDER.get)
    else:
        size = max(level_size, conf_size, key=SIZE_ORDER.get)
    res["size"] = size

    # الاتجاه الإيجابي أو السلبي (بالنسبة لرأي الأساسي)
    rr_p, rr_o = reward_risk(p), reward_risk(o)
    res["reward_risk"] = {"primary": rr_p, "other": rr_o}
    score = (bo - bp) * 100 + (o["confidence"] - p["confidence"])
    if rr_p is not None and rr_o is not None and rel == "اتفاق":
        score += (rr_o - rr_p) * 10
    if size == "صغير" and abs(score) < 5:
        sign = "محايد"
    elif score > 0:
        sign = "إيجابي"
    else:
        sign = "سلبي"
    res["sign"] = sign

    # المنطقية
    hp, ho = max(p["horizon_days"], 1), max(o["horizon_days"], 1)
    hr = round(max(hp, ho) / min(hp, ho), 2)
    res["horizon_ratio"] = hr
    method_diff = bool(p.get("method") and o.get("method") and p["method"] != o["method"])
    weak = [x["source"] for x in (p, o) if len(x.get("evidence") or []) < ver["min_evidence_items"]]
    if size == "صغير":
        logic = "منطقي"
    elif weak:
        logic = "غير محسوم"
        res["notes"].append("أدلة غير كافية لدى: " + "، ".join(weak))
    elif rel != "تعاكس" and (hr >= ver["horizon_ratio_explains"] or method_diff):
        logic = "منطقي"
        res["notes"].append("الفرق يفسره اختلاف " + ("الأفق الزمني" if hr >= ver["horizon_ratio_explains"] else "المنهج"))
    else:
        logic = "غير منطقي"
    res["logic"] = logic

    # الخطورة
    issues_o = internal_issues(o)
    severity = "مقبول"
    if rel == "تعاكس":
        if o["role"] == "lens" and not (o["source"] in red and o["direction"] == "avoid"):
            severity = "يحتاج مراجعة"
            res["notes"].append("عدسة استثمارية تخالف الاتجاه")
        else:
            severity = "خطير"
            res["notes"].append("اتجاهان متعاكسان")
    if issues_o and o["role"] != "lens":
        severity = max(severity, "خطير", key=SEVERITY_ORDER.get)
        res["notes"].append("أخطاء داخلية في الرأي: " + "؛ ".join(issues_o))
    if (rr_p is not None and rr_o is not None and rel == "اتفاق"
            and min(rr_p, rr_o) < risk["min_reward_risk"] <= max(rr_p, rr_o)):
        severity = max(severity, "يحتاج مراجعة", key=SEVERITY_ORDER.get)
        res["notes"].append("أحد الرأيين فقط يحقق الحد الأدنى للعائد مقابل المخاطرة")
    if size == "كبير" and logic != "منطقي":
        severity = max(severity, "يحتاج مراجعة", key=SEVERITY_ORDER.get)
    res["severity"] = severity
    return res


# ---------- الحكم الكلي ----------

def verify(opinions: list[dict]) -> dict:
    ver, risk, red = load_cfg()
    primaries = [o for o in opinions if o["role"] == "primary"]
    if len(primaries) != 1:
        raise SystemExit("يجب أن يكون في الملف رأي أساسي واحد بالضبط")
    p = primaries[0]
    others = [o for o in opinions if o is not p]
    symbols = {o["symbol"] for o in opinions}
    if len(symbols) != 1:
        raise SystemExit("الآراء يجب أن تكون عن أصل واحد")

    p_issues = internal_issues(p)
    pairs = [compare_pair(p, o, ver, risk, red) for o in others]

    # الثقة الموحدة
    w = ver["weights"]
    num = w["primary"] * p["confidence"]
    den = w["primary"]
    for o, pr in zip(others, pairs):
        wt = w[o["role"]]
        rel = pr["direction"]["relation"]
        c = o["confidence"] if rel == "اتفاق" else (o["confidence"] * 0.5 if rel == "اتفاق جزئي" else 0)
        num += wt * c
        den += wt
    worst_size = max([pr["size"] for pr in pairs] or ["صغير"], key=SIZE_ORDER.get)
    penalty = ver["divergence_penalty"]["small" if worst_size == "صغير" else "medium" if worst_size == "متوسط" else "large"]
    consensus = round(max(0.0, num / den - penalty), 1)

    worst = max([pr["severity"] for pr in pairs] or ["مقبول"], key=SEVERITY_ORDER.get)
    if p_issues:
        worst = "خطير"

    # المستويات المتحفظة
    agree = [o for o, pr in zip(others, pairs) if pr["direction"]["relation"] == "اتفاق" and o["role"] != "lens"]
    levels = None
    if p["direction"] in ("long", "short") and p.get("entry") is not None:
        stops = [x["stop"] for x in [p, *agree] if x.get("stop") is not None]
        tgts = [x["targets"][0] for x in [p, *agree] if x.get("targets")]
        if stops and tgts:
            if p["direction"] == "long":
                c_stop, c_tgt = max(stops), min(tgts)
            else:
                c_stop, c_tgt = min(stops), max(tgts)
            rr = round(abs(c_tgt - p["entry"]) / abs(p["entry"] - c_stop), 2) if p["entry"] != c_stop else None
            levels = {"entry": p["entry"], "stop": c_stop, "target": c_tgt, "reward_risk": rr}

    if worst == "خطير":
        decision = "تعارض — لا يُعرض كاقتراح"
    elif p["direction"] in ("neutral", "avoid"):
        decision = "لا فرصة من المصدر الأساسي"
    elif worst == "يحتاج مراجعة":
        decision = "اقتراح معلّق — يحتاج مراجعتك"
    elif consensus < risk["min_confidence_to_suggest"]:
        decision = "دون حد الثقة — لا يُعرض"
    elif levels and levels["reward_risk"] is not None and levels["reward_risk"] < risk["min_reward_risk"]:
        decision = "العائد مقابل المخاطرة دون الحد — لا يُعرض"
    else:
        decision = "صالح للعرض"

    return {
        "symbol": p["symbol"],
        "asset_class": p["asset_class"],
        "verified_at": datetime.now().isoformat(timespec="seconds"),
        "primary": {"source": p["source"], "direction": p["direction"], "confidence": p["confidence"],
                    "issues": p_issues, "reward_risk": reward_risk(p),
                    "as_of": p.get("as_of"), "horizon_days": p.get("horizon_days"), "method": p.get("method")},
        "pairs": pairs,
        "worst_severity": worst,
        "worst_size": worst_size,
        "consensus_confidence": consensus,
        "conservative_levels": levels,
        "decision": decision,
    }


# ---------- التقرير العربي ----------

DIR_AR = {"long": "شراء", "short": "بيع", "neutral": "محايد", "avoid": "تجنّب"}
ROLE_AR = {"primary": "أساسي", "secondary": "رديف", "lens": "عدسة"}


def fmt(v) -> str:
    return "—" if v is None else (f"{v}٪" if isinstance(v, (int, float)) else str(v))


def report_ar(r: dict) -> str:
    L = [f"# تحقق: {r['symbol']}", "",
         f"**الحكم:** {r['decision']}",
         f"**الثقة الموحدة:** {r['consensus_confidence']} من ١٠٠",
         f"**أقصى خطورة:** {r['worst_severity']} — **أكبر تفاوت:** {r['worst_size']}", ""]
    p = r["primary"]
    L.append(f"المصدر الأساسي ({p['source']}): {DIR_AR[p['direction']]}، ثقة {p['confidence']}، "
             f"عائد إلى مخاطرة {fmt(p['reward_risk']).rstrip('٪')}")
    if p["issues"]:
        L.append("أخطاء في رأي الأساسي: " + "؛ ".join(p["issues"]))
    L += ["", "| المصدر | الدور | الاتجاه | فرق الدخول | فرق الوقف | فرق الهدف | فرق الثقة | الحجم | النوع | المنطقية | الخطورة |",
          "|---|---|---|---|---|---|---|---|---|---|---|"]
    for pr in r["pairs"]:
        g = pr["levels_gap"]
        L.append(f"| {pr['source']} | {ROLE_AR[pr['role']]} | {pr['direction']['relation']} ({DIR_AR[pr['direction']['other']]}) | "
                 f"{fmt(g['entry_pct'])} | {fmt(g['stop_pct'])} | {fmt(g['target_pct'])} | {pr['confidence_gap']} | "
                 f"{pr['size']} | {pr['sign']} | {pr['logic']} | {pr['severity']} |")
    notes = [f"- {pr['source']}: {n}" for pr in r["pairs"] for n in pr["notes"]]
    if notes:
        L += ["", "**ملاحظات:**", *notes]
    lv = r["conservative_levels"]
    if lv:
        L += ["", "**المستويات المتحفظة** (أقرب وقف وأقرب هدف بين المصادر المتفقة):",
              f"دخول {lv['entry']} — وقف {lv['stop']} — هدف {lv['target']} — عائد إلى مخاطرة {lv['reward_risk']}"]
    return "\n".join(L) + "\n"


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("file")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--out")
    a = ap.parse_args()
    ops = json.loads(Path(a.file).read_text(encoding="utf-8"))
    r = verify(ops)
    if a.out:
        out = Path(a.out)
        out.mkdir(parents=True, exist_ok=True)
        stem = f"verify-{r['symbol']}-{r['verified_at'][:10]}"
        (out / f"{stem}.json").write_text(json.dumps(r, ensure_ascii=False, indent=2), encoding="utf-8")
        (out / f"{stem}.md").write_text(report_ar(r), encoding="utf-8")
    print(json.dumps(r, ensure_ascii=False, indent=2) if a.json else report_ar(r))
    return 0


if __name__ == "__main__":
    sys.exit(main())
