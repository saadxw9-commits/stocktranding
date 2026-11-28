#!/usr/bin/env python3
"""ملخص SPX قبل الافتتاح: الافتتاح الضمني من العقد الآجل ES والقيمة العادلة، ثم قراءته على مستويات عقودك.

ثلاث طرق، بالأولوية:
  1. cnbc_fair_value: سعر ES الآن − القيمة العادلة المنشورة (CNBC أو غيره) بالنقاط.
  2. vs_fair_value:   إغلاق SPX + «S&P futures vs fair value» (مثل Briefing.com)، وهو الفجوة الضمنية مباشرة.
  3. model:           سعر ES الآن − القيمة العادلة من الفائدة والتوزيعات (skills/option-selector/fair_value.py).

    python3 skills/spx-premarket-brief/premarket.py <لقطة.json> [--md reports/premarket/<اليوم>-spx.md]

بنية اللقطة: fixtures/example.json. كل رقم منقول يحمل source وquoted_at.
"""
from __future__ import annotations

import argparse
import json
import math
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / "option-selector"))
from fair_value import days_to_expiry, fair_value_points  # noqa: E402

MAX_QUOTE_AGE_MIN = 30        # أقدم من هذا = تحذير
DIVERGENCE_PTS = 3.0          # فرق أكبر بين الطرق = تحذير ومراجعة المصدر
ROLL_DAYS = 8                 # قبل انتهاء ES بهذه الأيام ينتقل التداول للعقد التالي


def stamp(value: str) -> datetime:
    dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if dt.utcoffset() is None:
        raise ValueError("timezone required")
    return dt


def _num(v, name):
    if type(v) not in (int, float) or not math.isfinite(v):
        raise ValueError(f"invalid number: {name}")
    return float(v)


def classify_gap(sigma: float | None) -> str:
    if sigma is None:
        return "unknown"
    a = abs(sigma)
    if a < 0.15:
        return "flat"
    if a < 0.5:
        return "small"
    if a < 1.0:
        return "notable"
    return "large"


def locate(level: float, prior: dict) -> str:
    hi, lo = prior.get("high"), prior.get("low")
    if hi is None or lo is None:
        return "unknown"
    if level > hi:
        return "above_prior_high"
    if level < lo:
        return "below_prior_low"
    return "inside_prior_range"


def build(snap: dict) -> dict:
    as_of = stamp(snap["as_of"])
    close = _num(snap["index_close"], "index_close")
    fut = snap["futures"]
    fut_px = _num(fut["price"], "futures.price")
    expiry = date.fromisoformat(fut["expiry"])
    rate = _num(snap.get("rate", 0.04), "rate")
    div = _num(snap.get("dividend_yield", 0.012), "dividend_yield")
    days = days_to_expiry(as_of.date(), expiry)
    warnings, methods = [], {}

    def age_check(block, label):
        if block and block.get("quoted_at"):
            age = (as_of - stamp(block["quoted_at"])).total_seconds() / 60
            if age > MAX_QUOTE_AGE_MIN:
                warnings.append(f"stale_{label}")
        elif block:
            warnings.append(f"no_timestamp_{label}")

    age_check(fut, "futures")
    fv_pub = snap.get("published_fair_value")
    if fv_pub:
        age_check(fv_pub, "published_fair_value")
        methods["cnbc_fair_value"] = dict(implied_open=fut_px - _num(fv_pub["points"], "published_fair_value.points"),
                                          source=fv_pub.get("source"), fair_value_pts=fv_pub["points"])
    vs = snap.get("futures_vs_fair_value")
    if vs:
        age_check(vs, "futures_vs_fair_value")
        methods["vs_fair_value"] = dict(implied_open=close + _num(vs["points"], "futures_vs_fair_value.points"),
                                        source=vs.get("source"), premium_pts=vs["points"])
    model_fv = fair_value_points(close, rate, div, days)
    methods["model"] = dict(implied_open=fut_px - model_fv, source="model", fair_value_pts=round(model_fv, 2),
                            assumptions=dict(rate=rate, dividend_yield=div))
    for m in methods.values():
        m["implied_open"] = round(m["implied_open"], 2)

    order = [k for k in ("cnbc_fair_value", "vs_fair_value", "model") if k in methods]
    chosen = order[0]
    if chosen == "model":
        warnings.append("no_published_fair_value_model_only")
    published = [methods[k]["implied_open"] for k in order if k != "model"]
    spread = max(published) - min(published) if len(published) > 1 else 0.0
    if spread > DIVERGENCE_PTS:
        warnings.append("published_sources_diverge")
    if days <= ROLL_DAYS:
        warnings.append("futures_roll_window_check_contract")

    implied = methods[chosen]["implied_open"]
    gap = implied - close
    iv = snap.get("iv")
    em = close * _num(iv, "iv") * math.sqrt(1 / 252) if iv else None
    sigma = gap / em if em else None
    prior = snap.get("prior_session") or {}

    strikes = []
    for w in snap.get("watch_strikes", []):
        k = _num(w["strike"], "strike")
        right = w.get("right", "call")
        dist = (k - implied) if right == "call" else (implied - k)  # موجب = ما زال خارج المال
        be = w.get("breakeven")
        strikes.append(dict(strike=k, right=right, distance_from_open_pts=round(dist, 2),
                            distance_sigma=round(dist / em, 2) if em else None,
                            breakeven=be,
                            breakeven_distance_pts=round(((be - implied) if right == "call" else (implied - be)), 2)
                            if be is not None else None))

    return dict(
        as_of=as_of.isoformat(), index_close=close,
        futures=dict(price=fut_px, contract=fut.get("contract"), expiry=fut["expiry"], days_to_expiry=days,
                     source=fut.get("source")),
        methods=methods, chosen_method=chosen, published_spread_pts=round(spread, 2),
        implied_open=implied, gap_pts=round(gap, 2), gap_pct=round(gap / close * 100, 3),
        expected_move_1d_pts=round(em, 2) if em else None, gap_sigma=round(sigma, 2) if sigma is not None else None,
        gap_class=classify_gap(sigma), open_location=locate(implied, prior), prior_session=prior,
        watch_strikes=strikes, warnings=warnings,
    )


AR_CLASS = dict(flat="شبه ثابت", small="فجوة صغيرة", notable="فجوة ملحوظة", large="فجوة كبيرة", unknown="غير محسوب")
AR_LOC = dict(above_prior_high="فوق قمة الجلسة السابقة", below_prior_low="دون قاع الجلسة السابقة",
              inside_prior_range="داخل مدى الجلسة السابقة", unknown="غير محسوب")
AR_METHOD = dict(cnbc_fair_value="العقد الآجل − القيمة العادلة المنشورة",
                 vs_fair_value="إغلاق SPX + فرق العقد الآجل عن القيمة العادلة", model="نموذج الفائدة والتوزيعات")
AR_WARN = dict(stale_futures="سعر العقد الآجل أقدم من 30 دقيقة",
               stale_published_fair_value="القيمة العادلة المنشورة أقدم من 30 دقيقة",
               stale_futures_vs_fair_value="رقم الفرق عن القيمة العادلة أقدم من 30 دقيقة",
               no_published_fair_value_model_only="لا رقم منشور: التقدير من النموذج فقط (فرضيات الفائدة والتوزيعات)",
               published_sources_diverge="المصادر المنشورة مختلفة بأكثر من 3 نقاط: راجع الأرقام",
               futures_roll_window_check_contract="أسبوع انتقال العقد الآجل: تأكد أن السعر للعقد الصحيح")


def to_markdown(r: dict) -> str:
    riyadh = stamp(r["as_of"]).astimezone(timezone(timedelta(hours=3)))
    lines = [f"# ملخص SPX قبل الافتتاح — {riyadh:%Y-%m-%d %H:%M} بتوقيت الرياض", ""]
    sign = "+" if r["gap_pts"] >= 0 else ""
    lines += [f"**الافتتاح الضمني:** {r['implied_open']:.2f} ({sign}{r['gap_pts']:.2f} نقطة، {sign}{r['gap_pct']:.2f}%)"
              f" — {AR_CLASS[r['gap_class']]}، {AR_LOC[r['open_location']]}.", ""]
    if r["gap_sigma"] is not None:
        lines.append(f"الفجوة = {r['gap_sigma']} من الحركة المتوقعة ليوم واحد ({r['expected_move_1d_pts']} نقطة). استنتاج.")
        lines.append("")
    lines += ["| الطريقة | الافتتاح الضمني | المصدر |", "|---|---|---|"]
    for k, m in r["methods"].items():
        mark = " ✅" if k == r["chosen_method"] else ""
        lines.append(f"| {AR_METHOD[k]}{mark} | {m['implied_open']:.2f} | {m.get('source') or '—'} |")
    lines.append("")
    if r["watch_strikes"]:
        lines += ["| العقد | المسافة من الافتتاح | بالانحراف اليومي | المسافة إلى التعادل |", "|---|---|---|---|"]
        for w in r["watch_strikes"]:
            lines.append(f"| {w['right']} {w['strike']:.0f} | {w['distance_from_open_pts']} | {w['distance_sigma']} | "
                         f"{w['breakeven_distance_pts'] if w['breakeven_distance_pts'] is not None else '—'} |")
        lines += ["", "المسافة الموجبة: العقد ما زال خارج المال عند الافتتاح.", ""]
    if r["warnings"]:
        lines += ["**تحذيرات:**"] + [f"- {AR_WARN.get(w, w)}" for w in r["warnings"]] + [""]
    lines.append("بعد الافتتاح (5:30 م بتوقيت الرياض) يُعتمد SPX مباشرة. الافتتاح الضمني تقدير لا سعر.")
    return "\n".join(lines) + "\n"


def main():
    for s in (sys.stdout, sys.stderr):
        if hasattr(s, "reconfigure"):
            s.reconfigure(encoding="utf-8")
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("snapshot", type=Path)
    ap.add_argument("--md", type=Path, help="احفظ الملخص العربي في هذا المسار")
    a = ap.parse_args()
    try:
        r = build(json.loads(a.snapshot.read_text(encoding="utf-8")))
    except (ValueError, KeyError, TypeError) as exc:
        print(json.dumps(dict(error=str(exc)), ensure_ascii=False))
        sys.exit(1)
    print(json.dumps(r, ensure_ascii=False, indent=2))
    if a.md:
        a.md.parent.mkdir(parents=True, exist_ok=True)
        a.md.write_text(to_markdown(r), encoding="utf-8")


if __name__ == "__main__":
    main()
