#!/usr/bin/env python3
"""اختبار فرضية صفقات الأيام والأسابيع بعقود الخيارات (docs/hypotheses/swing-v1.md).

القواعد كلها هنا كما أُعلنت، ولا تُعدَّل بعد رؤية النتائج.

    # جلب البيانات اليومية (يحتاج yfinance) ثم التشغيل
    python3 skills/swing-options-test/swing_test.py fetch --out data/swing
    python3 skills/swing-options-test/swing_test.py run --data data/swing --out reports/swing

الأسعار تقديرية بنموذج بلاك–شولز، وليست أسعار OPRA.
"""
from __future__ import annotations

import argparse
import json
import math
import sys
from pathlib import Path

import pandas as pd

N = lambda x: 0.5 * (1 + math.erf(x / math.sqrt(2)))

RULES = dict(            # معلنة في 28 نوفمبر 2026 قبل الاختبار
    budget=1800.0, dte=35, delta_target=0.50, delta_min=0.30,
    target_atr=2.0, stop_atr=1.0, max_hold=10,
    spread_pct=0.01, fee=0.65, rate=0.04,
)
ASSETS = {   # الأصل: (رمز السعر، رمز التذبذب، المقياس، خطوة سعر التنفيذ، عائد التوزيعات)
    "XSP": ("^SPX", "^VIX", 0.1, 1.0, 0.013),
    "QQQ": ("QQQ", "^VXN", 1.0, 1.0, 0.006),
}
PERIODS = {"P1": ("2019-01-01", "2022-12-31"), "P2": ("2023-01-01", "2026-11-27")}


# ---------- التسعير ----------
def bs(S, K, years, iv, side, r=RULES["rate"], q=0.0):
    if years <= 0 or iv <= 0:
        return max(S - K, 0.0) if side == "call" else max(K - S, 0.0)
    v = iv * math.sqrt(years)
    d1 = (math.log(S / K) + (r - q + iv * iv / 2) * years) / v
    d2 = d1 - v
    if side == "call":
        return S * math.exp(-q * years) * N(d1) - K * math.exp(-r * years) * N(d2)
    return K * math.exp(-r * years) * N(-d2) - S * math.exp(-q * years) * N(-d1)


def delta(S, K, years, iv, side, r=RULES["rate"], q=0.0):
    v = iv * math.sqrt(years)
    d1 = (math.log(S / K) + (r - q + iv * iv / 2) * years) / v
    return math.exp(-q * years) * (N(d1) if side == "call" else N(d1) - 1)


def choose_contract(S, iv, side, step, q, rules=RULES):
    """أقرب سعر تنفيذ لدلتا 0.50 يتسع له المبلغ، مع دلتا 0.30 على الأقل. يعيد (السعر، سعر الشراء، العدد) أو None."""
    years = rules["dte"] / 365
    sgn = 1 if side == "call" else -1
    atm = round(S / step) * step
    ks = sorted({k for k in (atm + sgn * step * j for j in range(-20, 200)) if k > 0},
                key=lambda k: abs(abs(delta(S, k, years, iv, side, q=q)) - rules["delta_target"]))
    best = ks[0]
    order = [best] + sorted([k for k in ks if sgn * (k - best) > 0], key=lambda k: sgn * k)   # الأبعد عن السعر بالتدريج
    for k in order:
        if abs(delta(S, k, years, iv, side, q=q)) < rules["delta_min"]:
            return None
        ask = bs(S, k, years, iv, side, q=q) * (1 + rules["spread_pct"])
        per = ask * 100 + rules["fee"]
        if per <= rules["budget"]:
            return k, ask, int(rules["budget"] // per)
    return None


# ---------- المؤشرات والإشارات ----------
def indicators(px: pd.DataFrame) -> pd.DataFrame:
    d = px.copy()
    for n in (20, 50, 200):
        d[f"sma{n}"] = d.Close.rolling(n).mean()
    tr = pd.concat([d.High - d.Low, (d.High - d.Close.shift()).abs(), (d.Low - d.Close.shift()).abs()], axis=1).max(axis=1)
    d["atr"] = tr.rolling(14).mean()
    d["hi20"] = d.Close.shift(1).rolling(20).max()
    d["lo20"] = d.Close.shift(1).rolling(20).min()
    d["up"] = (d.Close > d.sma200) & (d.sma50 > d.sma200)
    d["down"] = (d.Close < d.sma200) & (d.sma50 < d.sma200)
    return d


def signals(d: pd.DataFrame, variant: str) -> pd.Series:
    """'call' أو 'put' أو None لكل يوم."""
    if variant == "S1":
        cross_up = (d.Close.shift() <= d.sma20.shift()) & (d.Close > d.sma20)
        cross_dn = (d.Close.shift() >= d.sma20.shift()) & (d.Close < d.sma20)
        call, put = d.up & cross_up, d.down & cross_dn
    elif variant == "S2":
        call, put = d.up & (d.Close > d.hi20), d.down & (d.Close < d.lo20)
    else:
        raise ValueError("variant must be S1 or S2")
    out = pd.Series(None, index=d.index, dtype=object)
    out[call] = "call"
    out[put] = "put"
    return out


# ---------- المحاكاة ----------
def simulate(px: pd.DataFrame, iv: pd.DataFrame, asset: str, variant: str, start: str, end: str, rules=RULES):
    _, _, scale, step, q = ASSETS[asset]
    d = indicators(px)
    sig = signals(d, variant)
    iv = iv.reindex(d.index).ffill()
    idx = list(d.index)
    trades, i = [], 0
    while i < len(idx) - 1:
        t = idx[i]
        if not (pd.Timestamp(start) <= t <= pd.Timestamp(end)) or sig.iloc[i] not in ("call", "put") or pd.isna(d.atr.iloc[i]):
            i += 1
            continue
        side, j = sig.iloc[i], i + 1
        S0 = d.Open.iloc[j] * scale
        vol0 = iv.Open.iloc[j] / 100
        if not vol0 > 0:                       # افتتاح التذبذب مفقود أو صفر في بعض الأيام: إغلاق اليوم السابق
            vol0 = iv.Close.iloc[i] / 100
        pick = choose_contract(S0, vol0, side, step, q, rules)
        if pick is None:
            trades.append(dict(asset=asset, variant=variant, signal=str(t.date()), side=side, skipped="budget_or_delta"))
            i += 1
            continue
        K, ask, qty = pick
        atr = d.atr.iloc[i] * scale
        sgn = 1 if side == "call" else -1
        target, stop = S0 + sgn * rules["target_atr"] * atr, S0 - sgn * rules["stop_atr"] * atr
        exit_px = why = None
        for k in range(j, min(j + rules["max_hold"], len(idx))):
            hi, lo, cl = d.High.iloc[k] * scale, d.Low.iloc[k] * scale, d.Close.iloc[k] * scale
            hit_stop = (lo <= stop) if sgn > 0 else (hi >= stop)
            hit_tgt = (hi >= target) if sgn > 0 else (lo <= target)
            if hit_stop:
                exit_px, why = stop, "إلغاء"
            elif hit_tgt:
                exit_px, why = target, "هدف"
            elif k == min(j + rules["max_hold"], len(idx)) - 1:
                exit_px, why = cl, "وقف زمني"
            if why:
                break
        days_held = (idx[k] - idx[j]).days
        years_left = max(rules["dte"] - days_held, 0) / 365
        vol_k = iv.Close.iloc[k] / 100 if iv.Close.iloc[k] > 0 else vol0
        bid = bs(exit_px, K, years_left, vol_k, side, q=q) * (1 - rules["spread_pct"])
        pnl = qty * ((bid - ask) * 100 - 2 * rules["fee"])
        trades.append(dict(asset=asset, variant=variant, signal=str(t.date()), side=side, entry=str(idx[j].date()),
                           exit=str(idx[k].date()), S0=round(S0, 2), K=K, qty=qty, ask=round(ask, 2), bid=round(bid, 2),
                           why=why, pnl=round(pnl, 2), cost=round(qty * (ask * 100 + rules["fee"]), 2),
                           r_underlying=round(sgn * (exit_px - S0) / atr, 3)))   # للتشخيص: نتيجة الأصل نفسه بوحدة ATR
        i = k + 1   # مركز واحد: لا إشارة جديدة قبل الخروج
    return trades


def verdict(df: pd.DataFrame) -> dict:
    """معيار القبول المعلن: موجب في الفترتين، 30 صفقة على الأقل، وموجب بعد حذف أفضل 3."""
    out = {}
    for (asset, variant), g in df.groupby(["asset", "variant"]):
        rows, ok = {}, True
        for p, gp in g.groupby("period"):
            pnl = gp.pnl.sort_values(ascending=False)
            r = dict(n=len(gp), win=round((gp.pnl > 0).mean() * 100), net=round(pnl.sum()),
                     net_wo_top3=round(pnl.iloc[3:].sum()), avg=round(gp.pnl.mean()), worst=round(pnl.min()))
            ok &= r["net"] > 0 and r["n"] >= 30 and r["net_wo_top3"] > 0
            rows[p] = r
        ok &= set(rows) == set(PERIODS)
        out[f"{asset} {variant}"] = dict(periods=rows, accepted=bool(ok))
    return out


# ---------- البيانات ----------
def fetch(out: Path):
    import yfinance as yf
    out.mkdir(parents=True, exist_ok=True)
    for sym in sorted({s for a in ASSETS.values() for s in a[:2]}):
        d = yf.download(sym, start="2017-06-01", end="2026-11-28", interval="1d", progress=False, auto_adjust=False)
        d.columns = [c[0] for c in d.columns]
        d.to_csv(out / f"{sym.strip('^')}.csv")
        print(sym, len(d), d.index[0].date(), d.index[-1].date())


def load(data: Path, sym: str) -> pd.DataFrame:
    d = pd.read_csv(data / f"{sym.strip('^')}.csv", index_col=0, parse_dates=True)
    return d[["Open", "High", "Low", "Close"]].dropna()


def run(data: Path, out: Path | None):
    rows = []
    for asset, (psym, vsym, *_rest) in ASSETS.items():
        px, iv = load(data, psym), load(data, vsym)
        for variant in ("S1", "S2"):
            for period, (a, b) in PERIODS.items():
                for t in simulate(px, iv, asset, variant, a, b):
                    rows.append(dict(t, period=period))
    df = pd.DataFrame(rows)
    taken = df[df.get("skipped").isna()] if "skipped" in df else df
    v = verdict(taken)
    if out:
        out.mkdir(parents=True, exist_ok=True)
        df.to_csv(out / "trades.csv", index=False)
        (out / "verdict.json").write_text(json.dumps(v, ensure_ascii=False, indent=2), encoding="utf-8")
    return df, v


def main():
    for s in (sys.stdout, sys.stderr):
        if hasattr(s, "reconfigure"):
            s.reconfigure(encoding="utf-8")
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    f = sub.add_parser("fetch"); f.add_argument("--out", type=Path, required=True)
    r = sub.add_parser("run"); r.add_argument("--data", type=Path, required=True); r.add_argument("--out", type=Path)
    a = ap.parse_args()
    if a.cmd == "fetch":
        fetch(a.out)
    else:
        _, v = run(a.data, a.out)
        print(json.dumps(v, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
