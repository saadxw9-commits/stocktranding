#!/usr/bin/env python3
"""مشغّل المحاكاة: يحوّل شموع الثانية (Databento ohlcv-1s أو CSV مماثل) إلى جلسات، ويبني لكل جلسة سياقها
(قمة وقاع وإغلاق اليوم السابق، الجلسة الليلية، ملف الحجم للجلسة السابقة)، ويمرّرها على كاشف الارتداد،
ثم يجمع النتائج. لا ينفّذ أي أمر في أي وسيط.

    # جلب شموع الثانية (يحتاج DATABENTO_API_KEY)
    python3 skills/sim-runner/sim.py fetch --dataset GLBX.MDP3 --symbol ES.c.0 --stype continuous \
        --start 2026-11-16 --end 2026-11-28 --out data/es-1s.csv

    # إعادة التشغيل على الملف
    python3 skills/sim-runner/sim.py replay data/es-1s.csv --symbol ES --scale 1 --out reports/sim [--journal]

ملاحظات: SPX مؤشر بلا حجم؛ ES عليه فرق سعري (basis) عن SPX، فمستويات ES ليست مستويات SPX بالضبط.
لقياس بنية الكاشف يكفي، ولقياس الأداء على العقود استخدم SPY أو QQQ (dataset: XNAS.ITCH أو DBEQ.BASIC).
"""
from __future__ import annotations

import argparse
import base64
import csv
import json
import os
import sys
import urllib.parse
import urllib.request
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
from pathlib import Path


def _find_root() -> Path:
    here = Path(__file__).resolve()
    for p in here.parents:
        if (p / "config" / "risk-rules.yaml").exists():
            return p
    return here.parents[2]


ROOT = _find_root()


def _utf8_console() -> None:
    for s in (sys.stdout, sys.stderr):
        if hasattr(s, "reconfigure"):
            s.reconfigure(encoding="utf-8")


sys.path.insert(0, str(ROOT / "skills" / "reversal-detector"))
sys.path.insert(0, str(ROOT / "skills" / "option-selector"))
import detector  # noqa: E402

RTH_OPEN, RTH_CLOSE = (9, 30), (16, 0)


# ---------- توقيت نيويورك دون مكتبة المناطق الزمنية (تعمل على ويندوز بلا tzdata) ----------
def _nth_sunday(y: int, m: int, n: int) -> date:
    d = date(y, m, 1)
    d += timedelta(days=(6 - d.weekday()) % 7)
    return d + timedelta(weeks=n - 1)


def ny_offset(utc: datetime) -> timedelta:
    y = utc.year
    start = datetime.combine(_nth_sunday(y, 3, 2), datetime.min.time()).replace(tzinfo=timezone.utc) + timedelta(hours=7)
    end = datetime.combine(_nth_sunday(y, 11, 1), datetime.min.time()).replace(tzinfo=timezone.utc) + timedelta(hours=6)
    return timedelta(hours=-4) if start <= utc < end else timedelta(hours=-5)


def to_ny(utc: datetime) -> datetime:
    off = ny_offset(utc)
    return (utc + off).replace(tzinfo=timezone(off))


# ---------- القراءة ----------
def _ts(v: str) -> datetime:
    v = v.strip()
    if v.isdigit():
        return datetime.fromtimestamp(int(v) / 1e9, tz=timezone.utc)
    return datetime.fromisoformat(v.replace("Z", "+00:00")).astimezone(timezone.utc)


def load_csv(path: Path, symbol: str | None = None) -> list[dict]:
    """يقرأ أعمدة ts_event وopen وhigh وlow وclose وvolume. إن وُجد عمود symbol يُصفّى به."""
    bars = []
    with open(path, encoding="utf-8", newline="") as f:
        for r in csv.DictReader(f):
            if symbol and r.get("symbol") and not r["symbol"].startswith(symbol):
                continue
            t = to_ny(_ts(r.get("ts_event") or r["t"]))
            bars.append(dict(t=t, o=float(r["open"]), h=float(r["high"]), l=float(r["low"]),
                             c=float(r["close"]), v=float(r.get("volume") or 0)))
    bars.sort(key=lambda b: b["t"])
    return bars


def _hm(b):
    return (b["t"].hour, b["t"].minute)


def split_sessions(bars: list[dict]) -> list[dict]:
    """جلسة لكل يوم تداول: rth من 09:30 إلى 16:00، وovernight من 18:00 اليوم السابق إلى 09:30."""
    days: dict[date, dict] = defaultdict(lambda: dict(rth=[], overnight=[]))
    for b in bars:
        d = b["t"].date()
        if RTH_OPEN <= _hm(b) < RTH_CLOSE:
            days[d]["rth"].append(b)
        elif _hm(b) < RTH_OPEN:
            days[d]["overnight"].append(b)
        elif _hm(b) >= (18, 0):
            nxt = d + timedelta(days=1)
            while nxt.weekday() >= 5:
                nxt += timedelta(days=1)
            days[nxt]["overnight"].append(b)
    out = [dict(date=d, **v) for d, v in sorted(days.items()) if v["rth"]]
    return out


def _bar_json(b: dict, scale: float) -> dict:
    return dict(t=b["t"].isoformat(), o=b["o"] * scale, h=b["h"] * scale, l=b["l"] * scale,
                c=b["c"] * scale, v=b["v"])


def build_context(prev: dict, day: dict, symbol: str, scale: float, bin_size: float,
                  daily_iv: float | None = None) -> dict:
    pr = prev["rth"]
    on = day["overnight"]
    levels = dict(prev_high=max(b["h"] for b in pr) * scale, prev_low=min(b["l"] for b in pr) * scale,
                  prev_close=pr[-1]["c"] * scale)
    if on:
        levels["overnight_high"] = max(b["h"] for b in on) * scale
        levels["overnight_low"] = min(b["l"] for b in on) * scale
    close = day["rth"][0]["t"].replace(hour=RTH_CLOSE[0], minute=RTH_CLOSE[1], second=0, microsecond=0)
    ctx = dict(symbol=symbol, source="Databento ohlcv-1s", session_close=close.isoformat(),
               reference_price=levels["prev_close"], levels=levels,
               volume_profile=dict(prior_bars=[_bar_json(b, 1.0) for b in pr], bin_size=bin_size,
                                   price_scale=scale),
               bars=[_bar_json(b, scale) for b in day["rth"]])
    if daily_iv:
        ctx["expected_move"] = dict(reference=levels["prev_close"], iv=daily_iv, weekly=True)
    return ctx


# ---------- التشغيل ----------
def replay(bars, symbol, scale=1.0, bin_size=0.25, iv=None, cfg=None, out: Path | None = None):
    sessions = split_sessions(bars)
    results = []
    for prev, day in zip(sessions, sessions[1:]):
        ctx = build_context(prev, day, symbol, scale, bin_size, iv)
        rep = detector.run(ctx, cfg)
        rep["date"] = day["date"].isoformat()
        results.append(rep)
        if out:
            out.mkdir(parents=True, exist_ok=True)
            (out / f"{symbol}-{rep['date']}.json").write_text(
                json.dumps(rep, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    return aggregate(results), results


def aggregate(results: list[dict]) -> dict:
    trades = [e for r in results for e in r["events"] if e["type"] == "trade_closed"]
    rs = [t["result_r"] for t in trades]
    wins = [x for x in rs if x > 0]
    return dict(days=len(results), signals=sum(r["summary"]["signals"] for r in results),
                rejected=sum(r["summary"]["rejected"] for r in results), trades=len(trades),
                total_r=round(sum(rs), 2), win_rate=round(len(wins) / len(rs), 3) if rs else None,
                avg_r=round(sum(rs) / len(rs), 3) if rs else None,
                enough_evidence=len(rs) >= 30)


# ---------- الجلب ----------
def fetch(dataset, symbol, stype, start, end, out: Path, schema="ohlcv-1s", key=None):
    key = key or os.environ.get("DATABENTO_API_KEY")
    if not key:
        raise SystemExit("ضع المفتاح في المتغير DATABENTO_API_KEY (لا يُكتب في أي ملف).")
    body = urllib.parse.urlencode(dict(dataset=dataset, symbols=symbol, stype_in=stype, schema=schema,
                                       start=start, end=end, encoding="csv", pretty_px="true",
                                       pretty_ts="true", map_symbols="true")).encode()
    auth = base64.b64encode(f"{key}:".encode()).decode()
    req = urllib.request.Request("https://hist.databento.com/v0/timeseries.get_range", data=body,
                                 headers={"Authorization": f"Basic {auth}"})
    out.parent.mkdir(parents=True, exist_ok=True)
    with urllib.request.urlopen(req, timeout=300) as r, open(out, "wb") as f:
        while chunk := r.read(1 << 20):
            f.write(chunk)
    return out


def main():
    _utf8_console()
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    f = sub.add_parser("fetch")
    f.add_argument("--dataset", required=True)
    f.add_argument("--symbol", required=True)
    f.add_argument("--stype", default="raw_symbol")
    f.add_argument("--start", required=True)
    f.add_argument("--end", required=True)
    f.add_argument("--out", type=Path, required=True)
    r = sub.add_parser("replay")
    r.add_argument("csv", type=Path)
    r.add_argument("--symbol", default="SPX")
    r.add_argument("--filter", help="تصفية عمود symbol ببداية الاسم")
    r.add_argument("--scale", type=float, default=1.0)
    r.add_argument("--bin", type=float, default=0.25)
    r.add_argument("--iv", type=float, help="تذبذب ضمني سنوي (مثل 0.16) لمناطق الحركة المتوقعة")
    r.add_argument("--out", type=Path)
    r.add_argument("--journal", action="store_true")
    r.add_argument("--contract-mode", default="standard", choices=["standard", "cheap"])
    a = ap.parse_args()
    if a.cmd == "fetch":
        print(f"حُفظ في {fetch(a.dataset, a.symbol, a.stype, a.start, a.end, a.out)}")
        return
    bars = load_csv(a.csv, a.filter)
    agg, results = replay(bars, a.symbol, a.scale, a.bin, a.iv, out=a.out)
    for rep in results:
        s = rep["summary"]
        print(f"{rep['date']}: إشارات {s['signals']} | مرفوضة {s['rejected']} | صفقات {s['trades']} | {s['total_r']:+}R")
    print(f"\nالإجمالي: {agg['days']} جلسة، {agg['trades']} صفقة، {agg['total_r']:+}R، "
          f"نسبة الربح {agg['win_rate']}، متوسط {agg['avg_r']}R.")
    if not agg["enough_evidence"]:
        print("تنبيه: أقل من 30 صفقة؛ لا تُبنى عليها قرارات تعديل الفلاتر.")
    if a.journal:
        for rep in results:
            rep["mode"] = "replay"
            detector.to_journal(rep, "replay", a.contract_mode)
        print("أُضيفت الصفقات إلى السجل")


if __name__ == "__main__":
    main()
