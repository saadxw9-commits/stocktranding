#!/usr/bin/env python3
"""تموضع البائعين في سلسلة عقود الخيارات: أين يبيع الكبار عقود Put وCall، وماذا يعني ذلك لاتجاه الشراء.

التحليل فقط. المنظومة تبقى للشراء (Call أو Put) بلا بيع مكشوف.

ما يُحسب من لقطة سلسلة واحدة لتاريخ انتهاء واحد:
  ١. جدران البيع: أكبر العقود القائمة في Put تحت السعر (دعم محتمل) وفي Call فوقه (مقاومة محتملة).
  ٢. ضغط البائعين: إن وُجد في اللقطة حجم منفّذ عند سعر الطلب (بيع) وعند سعر العرض (شراء) لكل عقد،
     أو تغيّر العقود القائمة مع هبوط سعر العقد (كتابة عقود جديدة).
  ٣. نسبة Put إلى Call بالعقود القائمة وبالحجم.
  ٤. نقطة الألم الأقصى: السعر الذي تخسر عنده عقود المشترين أكبر قيمة عند الانتهاء.
  ٥. تعرّض الصنّاع لجاما (تقدير) ومستوى انقلابه: فوقه تميل الحركة للارتداد، وتحته للاندفاع.
ثم ميل الاتجاه (Call أو Put أو محايد) بدرجة ثقة، ومستويات بصيغة الخطط الخارجية لكاشف الارتداد.

    python3 skills/options-positioning/positioning.py <سلسلة.json> [--levels-out خطة.json] [--md تقرير.md]
"""
from __future__ import annotations

import argparse
import json
import math
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from statistics import NormalDist

YEAR = 365 * 86400
MIN_YEARS = 1800 / YEAR          # نصف ساعة: حد أدنى للزمن حتى لا تنفجر جاما قرب الانتهاء
N = NormalDist()


def stamp(v: str) -> datetime:
    dt = datetime.fromisoformat(v.replace("Z", "+00:00"))
    if dt.utcoffset() is None:
        raise ValueError("timezone required")
    return dt


def num(v, name, minimum=None):
    if type(v) not in (int, float) or not math.isfinite(v) or (minimum is not None and v < minimum):
        raise ValueError(f"invalid number: {name}")
    return float(v)


def gamma(spot, strike, years, iv):
    if iv <= 0:
        return 0.0
    t = max(years, MIN_YEARS)
    d1 = (math.log(spot / strike) + iv * iv / 2 * t) / (iv * math.sqrt(t))
    return math.exp(-d1 * d1 / 2) / math.sqrt(2 * math.pi) / (spot * iv * math.sqrt(t))


def total_gex(rows, spot, years, mult):
    """تقدير شائع: الصنّاع يملكون جاما موجبة في Call وسالبة في Put. دولار لكل 1% حركة."""
    g = 0.0
    for r in rows:
        if not r.get("iv") or not r.get("open_interest"):
            continue
        sign = 1 if r["right"] == "call" else -1
        g += sign * gamma(spot, r["strike"], years, r["iv"]) * r["open_interest"] * mult * spot * spot * 0.01
    return g


def zero_gamma(rows, spot, years, mult, span=0.05, steps=200):
    """أقرب سعر لانقلاب إشارة التعرّض الكلي لجاما ضمن ±span من السعر."""
    lo, hi = spot * (1 - span), spot * (1 + span)
    prev_s = prev_g = None
    flips = []
    for i in range(steps + 1):
        s = lo + (hi - lo) * i / steps
        g = total_gex(rows, s, years, mult)
        if prev_g is not None and (prev_g < 0) != (g < 0):
            flips.append(prev_s + (s - prev_s) * abs(prev_g) / (abs(prev_g) + abs(g)))
        prev_s, prev_g = s, g
    return min(flips, key=lambda x: abs(x - spot)) if flips else None


def max_pain(rows):
    strikes = sorted({r["strike"] for r in rows})
    def payout(settle):
        return sum(r["open_interest"] * (max(settle - r["strike"], 0) if r["right"] == "call"
                                         else max(r["strike"] - settle, 0)) for r in rows if r.get("open_interest"))
    return min(strikes, key=payout) if strikes else None


def seller_flow(r):
    """بيع صافٍ في العقد: من جهة التنفيذ إن وُجدت، وإلا من تغيّر العقود القائمة مع هبوط السعر."""
    sold, bought = r.get("volume_at_bid"), r.get("volume_at_ask")
    if sold is not None and bought is not None:
        return dict(net_sold=sold - bought, basis="aggressor_side")
    d_oi, d_px = r.get("oi_change"), r.get("price_change")
    if d_oi is not None and d_px is not None:
        if d_oi > 0 and d_px < 0:
            return dict(net_sold=d_oi, basis="oi_up_price_down")    # كتابة عقود جديدة غالبًا
        if d_oi > 0 and d_px > 0:
            return dict(net_sold=-d_oi, basis="oi_up_price_up")     # شراء عقود جديدة غالبًا
        return dict(net_sold=0, basis="oi_flat_or_down")
    return None


def analyze(snap: dict) -> dict:
    spot = num(snap["spot"], "spot", 0.000001)
    as_of, expiry = stamp(snap["as_of"]), stamp(snap["expiry"])
    years = max((expiry - as_of).total_seconds(), 0) / YEAR
    mult = num(snap.get("multiplier", 100), "multiplier", 1)
    rows = []
    for q in snap["chain"]:
        if q.get("right") not in ("call", "put"):
            raise ValueError("right must be call or put")
        rows.append(dict(q, strike=num(q["strike"], "strike", 0.000001),
                         open_interest=num(q.get("open_interest", 0), "open_interest", 0),
                         volume=num(q.get("volume", 0), "volume", 0)))
    warnings = []
    if not any(r["open_interest"] for r in rows):
        raise ValueError("chain has no open interest")
    if not all(r.get("iv") for r in rows):
        warnings.append("iv_missing_some_strikes_gex_partial")
    calls = [r for r in rows if r["right"] == "call"]
    puts = [r for r in rows if r["right"] == "put"]

    top = snap.get("top_n", 3)
    put_walls = sorted([r for r in puts if r["strike"] <= spot], key=lambda r: -r["open_interest"])[:top]
    call_walls = sorted([r for r in calls if r["strike"] >= spot], key=lambda r: -r["open_interest"])[:top]

    oi_c, oi_p = sum(r["open_interest"] for r in calls), sum(r["open_interest"] for r in puts)
    v_c, v_p = sum(r["volume"] for r in calls), sum(r["volume"] for r in puts)
    pcr_oi = round(oi_p / oi_c, 3) if oi_c else None
    pcr_vol = round(v_p / v_c, 3) if v_c else None

    flows, sold_puts, sold_calls = [], 0.0, 0.0
    for r in rows:
        f = seller_flow(r)
        if f is None:
            continue
        flows.append(dict(strike=r["strike"], right=r["right"], **f))
        if r["right"] == "put":
            sold_puts += f["net_sold"]
        else:
            sold_calls += f["net_sold"]
    if not flows:
        warnings.append("no_seller_flow_data")

    pain = max_pain(rows)
    gex = total_gex(rows, spot, years, mult)
    flip = zero_gamma(rows, spot, years, mult)

    # ميل الاتجاه: كل عامل صوت من -1 (Put) إلى +1 (Call)، ويُذكر سببه
    votes = []
    if put_walls and call_walls:
        d_sup, d_res = spot - put_walls[0]["strike"], call_walls[0]["strike"] - spot
        if d_sup + d_res > 0:
            v = (d_res - d_sup) / (d_res + d_sup)   # الدعم أقرب من المقاومة = مساحة للصعود
            votes.append(("walls", round(v, 2), f"أقرب جدار Put {put_walls[0]['strike']:.0f} وأقرب جدار Call {call_walls[0]['strike']:.0f}"))
    if flows:
        total = abs(sold_puts) + abs(sold_calls)
        if total:
            v = (sold_puts - sold_calls) / total     # بيع Put = البائع يراهن ألا يهبط السعر
            votes.append(("seller_flow", round(v, 2), f"صافي بيع Put {sold_puts:.0f} مقابل Call {sold_calls:.0f}"))
    if pain is not None and years <= 3 / 365:
        gap = (pain - spot) / spot
        v = max(-1.0, min(1.0, gap / 0.005))         # 0.5% بعد = صوت كامل، قرب الانتهاء فقط
        votes.append(("max_pain", round(v, 2), f"الألم الأقصى {pain:.0f}"))
    if pcr_vol is not None:
        # نسبة مرتفعة جدًا تعني تحوطًا مزدحمًا (ميل معاكس صعودي)، والمنخفضة جدًا طمعًا (ميل هبوطي)
        v = 0.5 if pcr_vol >= 1.3 else -0.5 if pcr_vol <= 0.6 else 0.0
        if v:
            votes.append(("put_call_ratio", v, f"نسبة Put إلى Call بالحجم {pcr_vol}"))
    score = round(sum(v for _, v, _ in votes) / len(votes), 2) if votes else 0.0
    bias = "call" if score >= 0.25 else "put" if score <= -0.25 else "neutral"
    regime = None
    if flip is not None:
        regime = "positive_gamma" if spot > flip else "negative_gamma"
    agree = sum(1 for _, v, _ in votes if (v > 0) == (score > 0) and abs(v) > 0.1)
    confidence = min(100, int(abs(score) * 60 + agree * 8)) if votes else 0

    levels = [dict(price=r["strike"], name=f"جدار Put ({int(r['open_interest'])})") for r in put_walls]
    levels += [dict(price=r["strike"], name=f"جدار Call ({int(r['open_interest'])})") for r in call_walls]
    if pain is not None:
        levels.append(dict(price=pain, name="الألم الأقصى"))
    if flip is not None:
        levels.append(dict(price=round(flip, 2), name="انقلاب جاما"))

    return dict(
        as_of=as_of.isoformat(), expiry=expiry.isoformat(), spot=spot, source=snap.get("source"),
        put_walls=[dict(strike=r["strike"], open_interest=r["open_interest"]) for r in put_walls],
        call_walls=[dict(strike=r["strike"], open_interest=r["open_interest"]) for r in call_walls],
        put_call_ratio=dict(open_interest=pcr_oi, volume=pcr_vol),
        max_pain=pain, gex_usd_per_1pct=round(gex), zero_gamma=round(flip, 2) if flip else None,
        gamma_regime=regime, seller_flow=flows,
        bias=dict(direction=bias, score=score, confidence=confidence, certainty="استنتاج",
                  votes=[dict(factor=f, vote=v, why=w) for f, v, w in votes]),
        detector_hint=("ثق بأنماط الارتداد (failed_breakdown وhigher_low وعكسها)" if regime == "positive_gamma"
                       else "ثق بأنماط الاستمرار (breakdown_retest وbreakout_retest)" if regime == "negative_gamma"
                       else None),
        levels=levels, warnings=warnings,
    )


def to_plan(r: dict) -> dict:
    """بصيغة fixtures/external-plans لكاشف الارتداد ومشغّل المحاكاة."""
    return dict(source=f"تموضع البائعين من {r.get('source') or 'سلسلة العقود'}", certainty="استنتاج",
                date=r["as_of"][:10], scale="SPX", levels=r["levels"])


AR_DIR = dict(call="ميل صاعد (Call)", put="ميل هابط (Put)", neutral="محايد")
AR_REG = dict(positive_gamma="جاما موجبة: الحركة تميل للارتداد والتهدئة",
              negative_gamma="جاما سالبة: الحركة تميل للاندفاع والاستمرار")


def to_markdown(r: dict) -> str:
    riyadh = stamp(r["as_of"]).astimezone(timezone(timedelta(hours=3)))
    b = r["bias"]
    out = [f"# تموضع البائعين — {riyadh:%Y-%m-%d %H:%M} بتوقيت الرياض", "",
           f"**الميل:** {AR_DIR[b['direction']]}، الدرجة {b['score']:+.2f}، الثقة {b['confidence']} من 100 (استنتاج).", ""]
    if r["gamma_regime"]:
        out += [f"**البيئة:** {AR_REG[r['gamma_regime']]}. مستوى الانقلاب {r['zero_gamma']}.", ""]
    out += ["| العامل | الصوت | السبب |", "|---|---|---|"]
    out += [f"| {v['factor']} | {v['vote']:+.2f} | {v['why']} |" for v in b["votes"]]
    out += ["", "| المستوى | النوع |", "|---|---|"]
    out += [f"| {lv['price']:.0f} | {lv['name']} |" for lv in sorted(r["levels"], key=lambda x: -x["price"])]
    out += ["", "الميل لا يكفي سببًا للدخول. المستويات مناطق لكاشف الارتداد، والإشارة منه."]
    if r["warnings"]:
        out += ["", "**تحذيرات:** " + "، ".join(r["warnings"])]
    return "\n".join(out) + "\n"


def main():
    for s in (sys.stdout, sys.stderr):
        if hasattr(s, "reconfigure"):
            s.reconfigure(encoding="utf-8")
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("chain", type=Path)
    ap.add_argument("--levels-out", type=Path, help="احفظ المستويات خطةً خارجية لكاشف الارتداد")
    ap.add_argument("--md", type=Path)
    a = ap.parse_args()
    try:
        r = analyze(json.loads(a.chain.read_text(encoding="utf-8")))
    except (ValueError, KeyError, TypeError) as exc:
        print(json.dumps(dict(error=str(exc)), ensure_ascii=False))
        sys.exit(1)
    print(json.dumps(r, ensure_ascii=False, indent=2))
    for path, text in ((a.levels_out, json.dumps(to_plan(r), ensure_ascii=False, indent=2) if a.levels_out else None),
                       (a.md, to_markdown(r) if a.md else None)):
        if path:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text, encoding="utf-8")


if __name__ == "__main__":
    main()
