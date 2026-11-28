#!/usr/bin/env python3
"""اختبار فرضية swing-shares-v1 (docs/hypotheses/swing-shares-v1.md): الإشارة نفسها على الأسهم،
والهبوط بشراء الصندوق العكسي (PSQ أو SH) لأن البيع على المكشوف ممنوع.

    python3 skills/swing-options-test/swing_shares.py fetch --out data/swing
    python3 skills/swing-options-test/swing_shares.py run --data data/swing --out reports/swing-shares
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent))
from swing_test import PERIODS, indicators, signals  # noqa: E402

RULES = dict(budget=1800.0, target_atr=2.0, stop_atr=1.0, max_hold=10, slip=0.0002)   # معلنة 28 نوفمبر 2026
PAIRS = {"QQQ": "PSQ", "SPY": "SH"}


def simulate(px: pd.DataFrame, inv: pd.DataFrame | None, asset: str, variant: str, start: str, end: str, rules=RULES):
    d = indicators(px)
    sig = signals(d, variant)
    idx = list(d.index)
    trades, i = [], 0
    while i < len(idx) - 1:
        t = idx[i]
        if not (pd.Timestamp(start) <= t <= pd.Timestamp(end)) or sig.iloc[i] not in ("call", "put") or pd.isna(d.atr.iloc[i]):
            i += 1
            continue
        side, j = sig.iloc[i], i + 1
        sgn = 1 if side == "call" else -1
        S0, atr = d.Open.iloc[j], d.atr.iloc[i]
        target, stop = S0 + sgn * rules["target_atr"] * atr, S0 - sgn * rules["stop_atr"] * atr
        last = min(j + rules["max_hold"], len(idx)) - 1
        exit_px = why = None
        for k in range(j, last + 1):
            o, hi, lo, cl = d.Open.iloc[k], d.High.iloc[k], d.Low.iloc[k], d.Close.iloc[k]
            if k > j and sgn * (o - stop) <= 0:          # فجوة خلف الإلغاء
                exit_px, why = o, "إلغاء بفجوة"
            elif k > j and sgn * (o - target) >= 0:      # فجوة خلف الهدف
                exit_px, why = o, "هدف بفجوة"
            elif (lo <= stop) if sgn > 0 else (hi >= stop):
                exit_px, why = stop, "إلغاء"
            elif (hi >= target) if sgn > 0 else (lo <= target):
                exit_px, why = target, "هدف"
            elif k == last:
                exit_px, why = cl, "وقف زمني"
            if why:
                break
        r_und = exit_px / S0 - 1
        if side == "call":
            instrument, entry_px = asset, S0
            ret = r_und
        else:
            instrument = PAIRS[asset]
            entry_px = inv.Open.get(idx[j]) if inv is not None else None
            ret = -r_und                                  # تقريب الصندوق العكسي المعلن
        price = entry_px if entry_px and entry_px > 0 else S0
        shares = int(rules["budget"] // price)
        cost_basis = shares * price
        pnl = cost_basis * (ret - 2 * rules["slip"])
        actual_inv = None
        if side == "put" and inv is not None and why == "وقف زمني" and idx[k] in inv.index and entry_px:
            actual_inv = round(inv.Close.loc[idx[k]] / entry_px - 1 + r_und, 5)   # فرق التتبع الفعلي (تشخيص)
        trades.append(dict(asset=asset, variant=variant, side=side, instrument=instrument, signal=str(t.date()),
                           entry=str(idx[j].date()), exit=str(idx[k].date()), shares=shares, invested=round(cost_basis, 2),
                           why=why, ret_pct=round(ret * 100, 3), pnl=round(pnl, 2), tracking_gap=actual_inv))
        i = k + 1
    return trades


def max_drawdown(pnl: pd.Series) -> float:
    eq = pnl.cumsum()
    return float((eq - eq.cummax()).min()) if len(eq) else 0.0


def verdict(df: pd.DataFrame, px_by_asset: dict) -> dict:
    out = {}
    for (asset, variant), g in df.groupby(["asset", "variant"]):
        rows, ok = {}, True
        for p, (a, b) in PERIODS.items():
            gp = g[g.period == p]
            srt = gp.pnl.sort_values(ascending=False)
            px = px_by_asset[asset].loc[a:b]
            bh = RULES["budget"] * (px.Close.iloc[-1] / px.Open.iloc[0] - 1)
            r = dict(n=len(gp), win=round((gp.pnl > 0).mean() * 100) if len(gp) else 0, net=round(gp.pnl.sum()),
                     net_wo_top3=round(srt.iloc[3:].sum()), max_dd=round(max_drawdown(gp.pnl)),
                     calls=int((gp.side == "call").sum()), puts=int((gp.side == "put").sum()),
                     put_net=round(gp[gp.side == "put"].pnl.sum()), buy_hold_1800=round(bh))
            ok &= r["net"] > 0 and r["n"] >= 30 and r["net_wo_top3"] > 0
            rows[p] = r
        out[f"{asset} {variant}"] = dict(periods=rows, accepted=bool(ok))
    return out


def fetch(out: Path):
    import yfinance as yf
    out.mkdir(parents=True, exist_ok=True)
    for sym in ("QQQ", "SPY", "PSQ", "SH"):
        d = yf.download(sym, start="2017-06-01", end="2026-11-28", interval="1d", progress=False, auto_adjust=False)
        d.columns = [c[0] for c in d.columns]
        d.to_csv(out / f"{sym}.csv")
        print(sym, len(d), d.index[0].date(), d.index[-1].date())


def load(data: Path, sym: str) -> pd.DataFrame:
    d = pd.read_csv(data / f"{sym}.csv", index_col=0, parse_dates=True)
    return d[["Open", "High", "Low", "Close"]].dropna()


def run(data: Path, out: Path | None):
    rows, pxs = [], {}
    for asset, inv_sym in PAIRS.items():
        px, inv = load(data, asset), load(data, inv_sym)
        pxs[asset] = px
        for variant in ("S1", "S2"):
            for period, (a, b) in PERIODS.items():
                rows += [dict(t, period=period) for t in simulate(px, inv, asset, variant, a, b)]
    df = pd.DataFrame(rows)
    v = verdict(df, pxs)
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
        print(json.dumps(run(a.data, a.out)[1], ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
