#!/usr/bin/env python3
"""ملف الحجم (Volume Profile): توزيع حجم التداول على مستويات السعر.

المخرجات لكل جلسة (أو مجموعة جلسات):
- نقطة التحكم POC: السعر الذي تداول عنده أكبر حجم.
- منطقة القيمة VAH/VAL: النطاق الذي يحوي 70% من الحجم حول نقطة التحكم.
- عقد الحجم المرتفع HVN: سعر قَبِله السوق طويلًا، يتصرف كدعم أو مقاومة.
- عقد الحجم المنخفض LVN: سعر رفضه السوق، يعبره السعر غالبًا بسرعة.
- نقاط التحكم غير المختبرة (Naked POC): نقطة تحكم جلسة سابقة لم يلمسها السعر بعدها.

الحجم يُوزَّع على نطاق كل شمعة بالتساوي، فالدقة أعلى كلما صغرت الشمعة (ثانية أو دقيقة).
مؤشر SPX لا حجم له: ابنِ الملف من SPY أو العقود الآجلة ES، ثم حوّل الأسعار بـ price_scale.

    python3 skills/reversal-detector/volume_profile.py <شموع.json> --bin 0.25 [--scale 10.0] [--sessions]
"""
from __future__ import annotations

import argparse
import json
import math
import sys
from collections import defaultdict
from pathlib import Path


def _bin(price: float, size: float) -> float:
    return round(math.floor(price / size + 1e-9) * size, 10)


def build(bars: list[dict], bin_size: float, value_area_pct: float = 0.70, price_scale: float = 1.0) -> dict:
    """يبني ملف الحجم من شموع فيها h وl وv. price_scale يحوّل الأسعار (مثلًا SPY → SPX)."""
    if bin_size <= 0:
        raise ValueError("bin_size must be positive")
    hist: dict[float, float] = defaultdict(float)
    for b in bars:
        v = b.get("v")
        if not v:
            continue
        lo, hi = _bin(b["l"] * price_scale, bin_size), _bin(b["h"] * price_scale, bin_size)
        n = int(round((hi - lo) / bin_size)) + 1
        for k in range(n):
            hist[round(lo + k * bin_size, 10)] += v / n
    if not hist:
        raise ValueError("no volume in bars")
    prices = sorted(hist)
    vols = [hist[p] for p in prices]
    total = sum(vols)
    mid = (prices[0] + prices[-1]) / 2
    poc_i = max(range(len(prices)), key=lambda i: (vols[i], -abs(prices[i] - mid)))
    # منطقة القيمة: توسّع من نقطة التحكم نحو الجهة الأعلى حجمًا حتى تبلغ النسبة
    lo_i = hi_i = poc_i
    acc = vols[poc_i]
    while acc < value_area_pct * total and (lo_i > 0 or hi_i < len(prices) - 1):
        up = vols[hi_i + 1] if hi_i < len(prices) - 1 else -1
        down = vols[lo_i - 1] if lo_i > 0 else -1
        if up >= down:
            hi_i += 1
            acc += up
        else:
            lo_i -= 1
            acc += down
    hvn, lvn = nodes(prices, vols)
    return dict(poc=prices[poc_i], vah=round(prices[hi_i] + bin_size, 10), val=prices[lo_i],
                value_area_pct=round(acc / total, 3), total_volume=total, bin_size=bin_size,
                low=prices[0], high=round(prices[-1] + bin_size, 10), hvn=hvn, lvn=lvn,
                bins=list(zip(prices, [round(v, 2) for v in vols])))


def nodes(prices: list[float], vols: list[float], window: int = 1) -> tuple[list[float], list[float]]:
    """عقد الحجم بعد تنعيم بسيط.
    المرتفعة: قمة محلية تتجاوز الوسيط بـ30% على الأقل.
    المنخفضة: أدنى نقطة بين عقدتين مرتفعتين متتاليتين، إذا قلّ حجمها عن 70% من أصغرهما."""
    if len(vols) < 2 * window + 3:
        return [], []
    sm = []
    for i in range(len(vols)):
        w = vols[max(0, i - window):i + window + 1]
        sm.append(sum(w) / len(w))
    med = sorted(sm)[len(sm) // 2]
    peaks = [i for i in range(1, len(sm) - 1)
             if sm[i] >= sm[i - 1] and sm[i] > sm[i + 1] and sm[i] >= 1.3 * med]
    lvn = []
    for a, b in zip(peaks, peaks[1:]):
        j = min(range(a + 1, b), key=lambda k: sm[k], default=None)
        if j is not None and sm[j] <= 0.7 * min(sm[a], sm[b]):
            lvn.append(prices[j])
    return [prices[i] for i in peaks], lvn


def split_sessions(bars: list[dict]) -> list[tuple[str, list[dict]]]:
    """يقسم الشموع إلى جلسات حسب التاريخ في الطابع الزمني (YYYY-MM-DD في أول 10 أحرف)."""
    out: dict[str, list[dict]] = defaultdict(list)
    for b in bars:
        out[str(b["t"])[:10]].append(b)
    return sorted(out.items())


def naked_pocs(sessions: list[tuple[str, dict]], later_ranges: list[tuple[float, float]] | None = None) -> list[dict]:
    """نقاط تحكم الجلسات التي لم يلمسها السعر في أي جلسة لاحقة.
    sessions: [(التاريخ، ملف الجلسة)] مرتبة زمنيًا؛ نطاق كل جلسة من low/high في ملفها."""
    out = []
    for i, (day, prof) in enumerate(sessions):
        poc = prof["poc"]
        touched = any(p["low"] <= poc <= p["high"] for _, p in sessions[i + 1:])
        touched = touched or any(lo <= poc <= hi for lo, hi in (later_ranges or []))
        if not touched and i < len(sessions) - 1:
            out.append(dict(date=day, poc=poc))
    return out


def levels_for_detector(prior: dict, naked: list[dict] | None = None, include_hvn: bool = True) -> list[tuple[float, str]]:
    """مستويات جاهزة لخريطة المناطق في كاشف الارتداد."""
    lv = [(prior["poc"], "نقطة التحكم للجلسة السابقة"),
          (prior["vah"], "أعلى منطقة القيمة للجلسة السابقة"),
          (prior["val"], "أدنى منطقة القيمة للجلسة السابقة")]
    if include_hvn:
        lv += [(p, "عقدة حجم مرتفع") for p in prior["hvn"] if p not in (prior["poc"],)]
    lv += [(n["poc"], f"نقطة تحكم غير مختبرة ({n['date']})") for n in (naked or [])]
    return lv


def main():
    for s in (sys.stdout, sys.stderr):
        if hasattr(s, "reconfigure"):
            s.reconfigure(encoding="utf-8")
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("bars", type=Path, help="ملف JSON فيه bars (قائمة شموع t,o,h,l,c,v) أو قائمة مباشرة")
    ap.add_argument("--bin", type=float, required=True, help="حجم الخانة السعرية")
    ap.add_argument("--scale", type=float, default=1.0, help="تحويل الأسعار، مثل 10.0 من SPY إلى SPX")
    ap.add_argument("--value-area", type=float, default=0.70)
    ap.add_argument("--sessions", action="store_true", help="ملف لكل جلسة مع نقاط التحكم غير المختبرة")
    a = ap.parse_args()
    data = json.loads(a.bars.read_text(encoding="utf-8"))
    bars = data["bars"] if isinstance(data, dict) else data
    if a.sessions:
        profs = [(d, build(bs, a.bin, a.value_area, a.scale)) for d, bs in split_sessions(bars)]
        for d, p in profs:
            print(f"{d}: نقطة التحكم {p['poc']:g} | منطقة القيمة {p['val']:g}–{p['vah']:g} | النطاق {p['low']:g}–{p['high']:g}")
        for n in naked_pocs(profs):
            print(f"نقطة تحكم غير مختبرة: {n['poc']:g} ({n['date']})")
        result = dict(sessions={d: {k: v for k, v in p.items() if k != "bins"} for d, p in profs},
                      naked_pocs=naked_pocs(profs))
    else:
        p = build(bars, a.bin, a.value_area, a.scale)
        print(f"نقطة التحكم {p['poc']:g} | منطقة القيمة {p['val']:g}–{p['vah']:g} ({p['value_area_pct']:.0%}) "
              f"| عقد مرتفعة {p['hvn']} | عقد منخفضة {p['lvn']}")
        result = {k: v for k, v in p.items() if k != "bins"}
    print(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    main()
