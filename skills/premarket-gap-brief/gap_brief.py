#!/usr/bin/env python3
"""ملخص ما قبل الافتتاح: الفجوة المتوقعة لصناديق الدول من حركة أسواقها الأصلية وعملاتها.

الفكرة: صندوق مثل EWZ يُسعَّر بالدولار في نيويورك، لكن أسهمه تتداول في ساو باولو. إذا تحرك السوق
المحلي أو العملة والسوق الأمريكية مغلقة (عطلة، أو قبل الافتتاح)، يفتح الصندوق بفجوة تعوّض الحركة.

    الفجوة المتوقعة = بيتا × [(المؤشر الآن ÷ المؤشر المرجعي) × أثر العملة − 1]

المؤشر المرجعي = قيمة المؤشر المحلي وقت آخر إغلاق للصندوق في أمريكا. أثر العملة لزوج USDBRL
(عملة محلية لكل دولار) = السعر المرجعي ÷ السعر الآن، ولزوج EURUSD = السعر الآن ÷ السعر المرجعي.

    python3 skills/premarket-gap-brief/gap_brief.py <لقطة.json> [--out reports/gaps/<اليوم>.json]

بنية اللقطة: انظر fixtures/. لكل صندوق: etf_prev_close وindex_ref وindex_now وfx_ref وfx_now،
واختياريًا etf_premarket (سعر ما قبل الافتتاح) وetf_open (للتقييم بعد الافتتاح) وus_sessions_missed.
"""
from __future__ import annotations

import argparse
import json
import sys
import math
from pathlib import Path

import yaml

def _find_root() -> Path:
    """جذر المستودع: أول مجلد أعلى يحوي config/risk-rules.yaml. يعمل من skills/ ومن النسخ في .claude/skills/."""
    here = Path(__file__).resolve()
    for p in here.parents:
        if (p / "config" / "risk-rules.yaml").exists():
            return p
    return here.parents[2]


ROOT = _find_root()

def _utf8_console() -> None:
    """طرفية ويندوز قد تكون بترميز cp1252 عند تحويل المخرجات؛ النص العربي يحتاج UTF-8."""
    for s in (sys.stdout, sys.stderr):
        if hasattr(s, "reconfigure"):
            s.reconfigure(encoding="utf-8")



def load_config(path=None):
    return yaml.safe_load(Path(path or ROOT / "config/country-etfs.yaml").read_text(encoding="utf-8"))


def positive(x, name):
    if type(x) not in (int, float) or not math.isfinite(x) or x <= 0:
        raise ValueError(f"{name} must be a positive number")
    return float(x)


def brief_one(item: dict, cfg: dict) -> dict:
    etf = item["etf"]
    spec = cfg["etfs"].get(etf)
    if not spec:
        raise ValueError(f"{etf} not in config/country-etfs.yaml")
    th = cfg["thresholds"]
    prev = positive(item["etf_prev_close"], "etf_prev_close")
    idx_ret = positive(item["index_now"], "index_now") / positive(item["index_ref"], "index_ref")
    fx_ref, fx_now = positive(item["fx_ref"], "fx_ref"), positive(item["fx_now"], "fx_now")
    fx_factor = fx_ref / fx_now if spec["fx_quote"] == "per_usd" else fx_now / fx_ref
    beta = item.get("beta", spec.get("beta", 1.0))
    local_pct = (idx_ret - 1) * 100
    fx_pct = (fx_factor - 1) * 100
    gap_pct = beta * (idx_ret * fx_factor - 1) * 100
    out = dict(etf=etf, country=spec["country"], index=spec["index"], fx=spec["fx"],
               us_sessions_missed=item.get("us_sessions_missed", 0),
               local_index_pct=round(local_pct, 2), fx_effect_pct=round(fx_pct, 2), beta=beta,
               expected_gap_pct=round(gap_pct, 2), expected_open=round(prev * (1 + gap_pct / 100), 2),
               etf_prev_close=prev, as_of=item.get("as_of"))
    size = abs(gap_pct)
    out["level"] = "large" if size >= th["large_pct"] else "watch" if size >= th["watch_pct"] else "none"
    out["direction"] = "up" if gap_pct > 0 else "down" if gap_pct < 0 else "flat"
    pre = item.get("etf_premarket")
    if pre is not None:
        pre_pct = (positive(pre, "etf_premarket") / prev - 1) * 100
        out["premarket_pct"] = round(pre_pct, 2)
        # كم من الفجوة المتوقعة ظهر في سعر ما قبل الافتتاح
        out["priced_in_share"] = round(pre_pct / gap_pct, 2) if abs(gap_pct) >= 0.1 else None
    opened = item.get("etf_open")
    if opened is not None:
        open_pct = (positive(opened, "etf_open") / prev - 1) * 100
        out["actual_open_pct"] = round(open_pct, 2)
        out["estimate_error_pct"] = round(gap_pct - open_pct, 2)
    out["guidance"] = guidance(out, th)
    return out


def guidance(o: dict, th: dict) -> list[str]:
    if o["level"] == "none":
        return ["لا فجوة تُذكر. يُعامل الصندوق كيوم عادي."]
    word = "صعود" if o["direction"] == "up" else "هبوط"
    rule = [f"فجوة {word} متوقعة {o['expected_gap_pct']:+.2f}% "
            f"(المؤشر المحلي {o['local_index_pct']:+.2f}%، العملة {o['fx_effect_pct']:+.2f}%)."]
    if o["level"] == "large":
        rule.append(f"لا شراء عقود في اتجاه الفجوة قبل مرور {th['no_chase_minutes']} دقيقة من الافتتاح؛ "
                    "السعر الجديد يظهر مع أول صفقة، والشراء عنده لحاق بالحركة.")
        rule.append("بعد الافتتاح: راقب امتداد الفجوة أو تراجعها بكاشف الارتداد على شارت الصندوق نفسه.")
    else:
        rule.append("تابع الافتتاح قبل أي قرار على هذا الصندوق.")
    rule.append("إن كان لديك مركز مفتوح على الصندوق: قرر قبل الافتتاح هل تبيع في الفجوة أم تحتفظ.")
    share = o.get("priced_in_share")
    if share is not None:
        rule.append(f"سعر ما قبل الافتتاح يعكس نحو {share:.0%} من الفجوة المتوقعة.")
    return rule


def run(snapshot: dict, cfg: dict | None = None) -> dict:
    cfg = cfg or load_config()
    items = [dict(it, as_of=it.get("as_of", snapshot.get("as_of"))) for it in snapshot["items"]]
    rows = [brief_one(it, cfg) for it in items]
    rows.sort(key=lambda r: -abs(r["expected_gap_pct"]))
    return dict(as_of=snapshot.get("as_of"), source=snapshot.get("source"), items=rows)


LEVELS = {"large": "فجوة كبيرة", "watch": "تستحق المتابعة", "none": "لا فجوة"}


def main():
    _utf8_console()
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("snapshot", type=Path)
    ap.add_argument("--out", type=Path)
    a = ap.parse_args()
    report = run(json.loads(a.snapshot.read_text(encoding="utf-8")))
    print(f"ملخص ما قبل الافتتاح — {report['as_of']}\n")
    for r in report["items"]:
        print(f"{r['etf']} ({r['country']}): {LEVELS[r['level']]} — متوقع {r['expected_gap_pct']:+.2f}% "
              f"→ افتتاح قرب {r['expected_open']}")
        for g in r["guidance"]:
            print(f"   • {g}")
        if "actual_open_pct" in r:
            print(f"   • الافتتاح الفعلي {r['actual_open_pct']:+.2f}%، خطأ التقدير {r['estimate_error_pct']:+.2f} نقطة مئوية")
        print()
    if a.out:
        a.out.parent.mkdir(parents=True, exist_ok=True)
        a.out.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")


if __name__ == "__main__":
    main()
