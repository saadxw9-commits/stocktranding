#!/usr/bin/env python3
"""مشغّل المحاكاة: يحوّل شموع الثانية (Databento ohlcv-1s أو CSV مماثل) إلى جلسات، ويبني لكل جلسة سياقها
(قمة وقاع وإغلاق اليوم السابق، الجلسة الليلية، ملف الحجم للجلسة السابقة)، ويمرّرها على كاشف الارتداد،
ثم يجمع النتائج، ويسعّر الصفقات بأسعار العقود الفعلية من OPRA. لا ينفّذ أي أمر في أي وسيط.

    # ١) جلب شموع الثانية (المفتاح من المتغير DATABENTO_API_KEY فقط)
    python3 skills/sim-runner/sim.py fetch --dataset EQUS.MINI --symbol QQQ --start 2026-05-26 --end 2026-11-27 --out data/QQQ.csv

    # ٢) إعادة التشغيل (إعدادات الكاشف بنقاط SPX، فـ--scale يحوّل السعر إلى مقياسها)
    python3 skills/sim-runner/sim.py replay data/QQQ.csv --symbol QQQ --filter QQQ --out reports/sim/QQQ

    # ٣) تسعير الصفقات بعقود OPRA الفعلية وحساب حساب افتراضي
    python3 skills/sim-runner/sim.py price-options reports/sim/QQQ --root QQQ --capital 7000 --risk-usd 70

ملاحظات: SPX مؤشر بلا حجم؛ ES عليه فرق سعري (basis) عن SPX. سجل الأخطاء والدروس في docs/lessons-learned.md.
"""
from __future__ import annotations

import argparse
import base64
import csv
import json
import os
import statistics
import sys
import time as _time
import urllib.error
import urllib.parse
import urllib.request
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, time, timedelta, timezone
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

RTH_OPEN, RTH_CLOSE, EARLY_CLOSE = (9, 30), (16, 0), (13, 0)

# تقويم NYSE الرسمي (بيان NYSE Group لأعوام 2025–2027). يُحدَّث عند صدور بيان جديد.
HOLIDAYS = {date.fromisoformat(d) for d in (
    "2025-01-01 2025-01-20 2025-02-17 2025-04-18 2025-05-26 2025-06-19 2025-07-04 2025-09-01 2025-11-27 2025-12-25 "
    "2026-01-01 2026-01-19 2026-02-16 2026-04-03 2026-05-25 2026-06-19 2026-07-03 2026-09-07 2026-11-26 2026-12-25 "
    "2027-01-01 2027-01-18 2027-02-15 2027-03-26 2027-05-31 2027-06-18 2027-07-05 2027-09-06 2027-11-25 2027-12-24"
).split()}
EARLY_CLOSES = {date.fromisoformat(d) for d in
                "2025-07-03 2025-11-28 2025-12-24 2026-11-27 2026-12-24 2027-11-26".split()}

# إعدادات الكاشف بنقاط SPX (نحو 7000). المقياس يحوّل سعر الأداة إليها؛ الأرقام المستديرة تبقى حقيقية (25 نقطة = 2.5$).
KNOWN_SCALE = {"SPX": 1.0, "ES": 1.0, "SPY": 10.0, "QQQ": 10.0, "XSP": 10.0}
SCALED_RANGE = (2000.0, 20000.0)

# الجذور التي لها عقود تنتهي كل يوم تداول؛ غيرها يُستخدم أقرب انتهاء أسبوعي (الجمعة).
DAILY_EXPIRY_ROOTS = {"SPY", "QQQ", "SPXW", "XSP", "IWM"}


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


def session_close(d: date) -> tuple[int, int]:
    return EARLY_CLOSE if d in EARLY_CLOSES else RTH_CLOSE


def is_trading_day(d: date) -> bool:
    return d.weekday() < 5 and d not in HOLIDAYS


def resolve_scale(symbol: str, scale: float | None) -> float:
    """المقياس إلزامي لأي أداة غير معروفة: تطبيق نقاط SPX على سعر أداة أخرى يفسد الإعدادات كلها."""
    if scale is not None:
        return scale
    if symbol.upper() in KNOWN_SCALE:
        return KNOWN_SCALE[symbol.upper()]
    raise SystemExit(f"حدّد --scale لـ{symbol}: إعدادات الكاشف بنقاط SPX، ولا مقياس معتمد لهذه الأداة.")


# ---------- القراءة ----------
def _ts(v: str) -> datetime:
    v = v.strip()
    if v.isdigit():
        return datetime.fromtimestamp(int(v) / 1e9, tz=timezone.utc)
    v = v.replace("Z", "+00:00")
    if "." in v:  # نانوثانية: نكتفي بالثانية الكاملة
        head, tail = v.split(".", 1)
        v = head + tail[tail.index("+"):] if "+" in tail else head
    return datetime.fromisoformat(v).astimezone(timezone.utc)


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
    """جلسة لكل يوم تداول: rth من 09:30 إلى الإغلاق (16:00، أو 13:00 في أيام الإغلاق المبكر)،
    وovernight من 18:00 في آخر يوم تداول (ومن كل شموع العطل وعطلة الأسبوع) إلى 09:30.
    ما بين الإغلاق و18:00 يُستبعد. أيام العطل لا تكوّن جلسة، وإن تداولت فيها العقود الآجلة."""
    days: dict[date, dict] = defaultdict(lambda: dict(rth=[], overnight=[]))

    def next_day(d):
        n = d + timedelta(days=1)
        while not is_trading_day(n):
            n += timedelta(days=1)
        return n

    for b in bars:
        d = b["t"].date()
        if not is_trading_day(d):
            days[next_day(d)]["overnight"].append(b)
        elif RTH_OPEN <= _hm(b) < session_close(d):
            days[d]["rth"].append(b)
        elif _hm(b) < RTH_OPEN:
            days[d]["overnight"].append(b)
        elif _hm(b) >= (18, 0):
            days[next_day(d)]["overnight"].append(b)
    return [dict(date=d, **v) for d, v in sorted(days.items()) if v["rth"]]


def _bar_json(b: dict, scale: float) -> dict:
    return dict(t=b["t"].isoformat(), o=b["o"] * scale, h=b["h"] * scale, l=b["l"] * scale,
                c=b["c"] * scale, v=b["v"])


def build_context(prev: dict, day: dict, symbol: str, scale: float, bin_size: float,
                  daily_iv: float | None = None) -> dict:
    pr = prev["rth"]
    on = day["overnight"]
    levels = dict(prev_high=max(b["h"] for b in pr) * scale, prev_low=min(b["l"] for b in pr) * scale,
                  prev_close=pr[-1]["c"] * scale)
    if not SCALED_RANGE[0] <= levels["prev_close"] <= SCALED_RANGE[1]:
        print(f"تنبيه: سعر {symbol} بعد المقياس {levels['prev_close']:.0f} بعيد عن مقياس SPX؛ "
              "الإعدادات بالنقاط لن تعني الشيء نفسه.", file=sys.stderr)
    if on:
        levels["overnight_high"] = max(b["h"] for b in on) * scale
        levels["overnight_low"] = min(b["l"] for b in on) * scale
    d0 = day["rth"][0]["t"]
    hh, mm = session_close(d0.date())
    close = d0.replace(hour=hh, minute=mm, second=0, microsecond=0)
    ctx = dict(symbol=symbol, source="Databento ohlcv-1s", session_close=close.isoformat(),
               reference_price=levels["prev_close"], levels=levels,
               volume_profile=dict(prior_bars=[_bar_json(b, 1.0) for b in pr], bin_size=bin_size,
                                   price_scale=scale),
               bars=[_bar_json(b, scale) for b in day["rth"]])
    if daily_iv:
        ctx["expected_move"] = dict(reference=levels["prev_close"], iv=daily_iv, weekly=True)
    return ctx


# ---------- التشغيل ----------
def replay(bars, symbol, scale=None, bin_size=0.5, iv=None, cfg=None, out: Path | None = None,
           plans: list[dict] | None = None, levels_shift: float = 0.0):
    scale = resolve_scale(symbol, scale)
    sessions = split_sessions(bars)
    results = []
    for prev, day in zip(sessions, sessions[1:]):
        ctx = build_context(prev, day, symbol, scale, bin_size, iv)
        for plan in plans or []:
            if str(plan.get("date")) == str(day["date"]):
                detector.add_external_levels(ctx, plan, levels_shift)
        rep = detector.run(ctx, cfg)
        rep["date"] = day["date"].isoformat()
        rep["scale"] = scale
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


# ---------- Databento ----------
API = "https://hist.databento.com/v0/"


def _key(key=None):
    key = key or os.environ.get("DATABENTO_API_KEY")
    if not key:
        raise SystemExit("ضع المفتاح في المتغير DATABENTO_API_KEY (لا يُكتب في أي ملف).")
    return key


def _post(endpoint: str, key: str, retries: int = 6, **params) -> bytes:
    auth = base64.b64encode(f"{key}:".encode()).decode()
    req = urllib.request.Request(API + endpoint, data=urllib.parse.urlencode(params).encode(),
                                 headers={"Authorization": f"Basic {auth}"})
    for i in range(retries):
        try:
            with urllib.request.urlopen(req, timeout=300) as r:
                return r.read()
        except urllib.error.HTTPError as e:
            body = e.read().decode(errors="replace")
            if e.code in (429, 500, 502, 503, 504) and i < retries - 1:
                _time.sleep(3 * (i + 1))
                continue
            raise DatabentoError(e.code, body) from None
        except (ConnectionError, TimeoutError, urllib.error.URLError):
            if i == retries - 1:
                raise
            _time.sleep(3 * (i + 1))
    raise RuntimeError("unreachable")


class DatabentoError(Exception):
    def __init__(self, code, body):
        self.code, self.body = code, body
        try:
            detail = json.loads(body).get("detail", {})
        except ValueError:
            detail = {}
        self.case = detail.get("case") if isinstance(detail, dict) else None
        self.message = detail.get("message", body[:300]) if isinstance(detail, dict) else str(detail)
        super().__init__(f"{code} {self.case}: {self.message}")


def fetch(dataset, symbol, stype, start, end, out: Path, schema="ohlcv-1s", key=None):
    key = _key(key)
    try:
        data = _post("timeseries.get_range", key, dataset=dataset, symbols=symbol, stype_in=stype, schema=schema,
                     start=start, end=end, encoding="csv", pretty_px="true", pretty_ts="true", map_symbols="true")
    except DatabentoError as e:
        if e.case and "available" in e.case:
            raise SystemExit(f"البيانات لم تُنشر بعد لهذه المدة: {e.message}") from None
        raise
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_bytes(data)
    return out


# ---------- تسعير الصفقات بعقود OPRA الفعلية ----------
def expiry_for(root: str, d: date) -> date:
    """عقد اليوم نفسه للجذور اليومية، وإلا أقرب جمعة تداول (أو اليوم السابق لها إن كانت عطلة)."""
    if root.upper() in DAILY_EXPIRY_ROOTS:
        return d
    e = d
    while e.weekday() != 4:
        e += timedelta(days=1)
    while not is_trading_day(e):
        e -= timedelta(days=1)
    return e if e >= d else d


def option_symbol(root: str, expiry: date, right: str, strike: float) -> str:
    """رمز OSI بطول 21: الجذر بست خانات، ثم YYMMDD، ثم C أو P، ثم السعر × 1000 بثماني خانات."""
    return f"{root.upper():<6}{expiry:%y%m%d}{'C' if right == 'call' else 'P'}{int(round(strike * 1000)):08d}"


def load_trades(out_dir: Path) -> list[dict]:
    trades = []
    for f in sorted(out_dir.glob("*.json")):
        rep = json.loads(f.read_text(encoding="utf-8"))
        if "scale" not in rep:
            raise SystemExit(f"{f.name} بلا مقياس مسجّل (من نسخة أقدم)؛ أعد replay قبل التسعير.")
        sc = rep["scale"]
        for e in rep["events"]:
            if e["type"] != "trade_closed":
                continue
            fills = [dict(at=x["at"], qty=x["qty"], px=x["price"] / sc, reason=x["reason"]) for x in e["fills"]]
            sign = 1 if e["side"] == "call" else -1
            entry = e["entry"] / sc
            pts = sum(x["qty"] * sign * (x["px"] - entry) for x in fills)
            trades.append(dict(date=rep["date"], side=e["side"], pattern=e["pattern"], entry=entry,
                               opened=e["opened_at"], fills=fills, r=e["result_r"],
                               risk=abs(pts / e["result_r"]) if e["result_r"] else None))
    return trades


def _utc(s: str) -> datetime:
    return datetime.fromisoformat(str(s)).astimezone(timezone.utc)


def _quotes(text: str) -> list[tuple]:
    q = []
    for r in csv.DictReader(text.splitlines()):
        if r.get("bid_px_00") and r.get("ask_px_00"):
            q.append((_ts(r["ts_recv"]), float(r["bid_px_00"]), float(r["ask_px_00"])))
    return q


def _quote_at(q: list[tuple], t: datetime) -> tuple:
    best = q[0]
    for x in q:
        if x[0] <= t:
            best = x
        else:
            break
    return best


def price_trade(t: dict, root: str, strike_step: float, key: str, cache: Path | None = None) -> dict:
    """يشتري بسعر البائع عند الدخول ويبيع بسعر المشتري عند كل خروج، للعقد الأقرب إلى السعر.
    إن لم يُدرج السعر الأقرب يُجرَّب ما حوله."""
    d = date.fromisoformat(t["date"])
    exp = expiry_for(root, d)
    a = (_utc(t["opened"]) - timedelta(seconds=30)).strftime("%Y-%m-%dT%H:%M:%SZ")
    b = (max(_utc(f["at"]) for f in t["fills"]) + timedelta(seconds=5)).strftime("%Y-%m-%dT%H:%M:%SZ")
    k0 = round(t["entry"] / strike_step) * strike_step
    for k in (k0, k0 + strike_step, k0 - strike_step, k0 + 2 * strike_step, k0 - 2 * strike_step):
        sym = option_symbol(root, exp, t["side"], k)
        fn = cache / f"{sym.replace(' ', '_')}_{a.replace(':', '')}.csv" if cache else None
        if fn and fn.exists():
            text = fn.read_text()
        else:
            try:
                text = _post("timeseries.get_range", key, dataset="OPRA.PILLAR", schema="cbbo-1s",
                             stype_in="raw_symbol", symbols=sym, start=a, end=b, encoding="csv",
                             pretty_px="true", pretty_ts="true").decode()
            except DatabentoError as e:
                if e.case == "symbology_invalid_request":
                    text = ""
                else:
                    raise
            if fn:
                fn.write_text(text)
        q = _quotes(text)
        if not q:
            continue
        ein = _quote_at(q, _utc(t["opened"]))
        exits = [(f["qty"], _quote_at(q, _utc(f["at"]))) for f in t["fills"]]
        per = sum(qty * (x[1] - ein[2]) for qty, x in exits) * 100
        return dict(t, symbol=sym, strike=k, expiry=exp.isoformat(), ask_in=ein[2], bid_in=ein[1],
                    exits=[(qty, x[1], x[2]) for qty, x in exits], pnl_per_contract=round(per, 2))
    return dict(t, missing=True)


def price_trades(trades, root, strike_step=1.0, key=None, cache: Path | None = None, workers=8):
    key = _key(key)
    if cache:
        cache.mkdir(parents=True, exist_ok=True)
    with ThreadPoolExecutor(workers) as ex:
        return list(ex.map(lambda t: price_trade(t, root, strike_step, key, cache), trades))


def account(priced: list[dict], capital=7000.0, risk_usd=70.0, fee=0.65, delta=0.5) -> dict:
    """حجم كل صفقة من مخاطرتها المعروفة قبل الدخول: الخسارة المتوقعة للعقد عند الإلغاء ≈ الحساسية × مسافة الإلغاء × 100
    + فرق العرض والطلب. إن تجاوز عقد واحد حد المخاطرة تُتخطّى الصفقة (لا تُرفع إلى عقد واحد)."""
    rows = [p for p in priced if not p.get("missing")]
    risks = [p["risk"] for p in rows if p.get("risk")]
    med = statistics.median(risks) if risks else 0
    eq = low = peak = capital
    dd = 0.0
    wins = losses = 0.0
    nw = nl = skipped = taken = 0
    monthly: dict[str, float] = defaultdict(float)
    by_pattern: dict[str, list] = defaultdict(lambda: [0, 0.0])
    for p in rows:
        per_loss = delta * (p.get("risk") or med) * 100 + (p["ask_in"] - p["bid_in"]) * 100
        qty = int(risk_usd // per_loss) if per_loss > 0 else 0
        if qty < 1:
            skipped += 1
            continue
        taken += 1
        pnl = p["pnl_per_contract"] * qty - 2 * fee * qty
        eq += pnl
        low, peak = min(low, eq), max(peak, eq)
        dd = min(dd, eq - peak)
        monthly[p["date"][:7]] += pnl
        by_pattern[p["pattern"]][0] += 1
        by_pattern[p["pattern"]][1] += pnl
        if pnl > 0:
            wins += pnl
            nw += 1
        elif pnl < 0:
            losses += pnl
            nl += 1
    return dict(capital=capital, final=round(eq, 2), net=round(eq - capital, 2), lowest=round(low, 2),
                max_drawdown=round(dd, 2), wins=nw, win_usd=round(wins, 2), losses=nl, loss_usd=round(losses, 2),
                taken=taken, skipped_risk=skipped, missing=sum(1 for p in priced if p.get("missing")),
                monthly={k: round(v, 2) for k, v in sorted(monthly.items())},
                by_pattern={k: dict(trades=v[0], usd=round(v[1], 2)) for k, v in sorted(by_pattern.items())},
                assumptions=dict(risk_usd=risk_usd, fee_per_contract_side=fee, delta_for_sizing=delta,
                                 fills="شراء بسعر البائع وبيع بسعر المشتري في ثانية الحدث (OPRA cbbo-1s)"))


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
    r.add_argument("--scale", type=float, help="تحويل السعر إلى مقياس SPX؛ إلزامي للأدوات خارج KNOWN_SCALE")
    r.add_argument("--bin", type=float, default=0.5)
    r.add_argument("--iv", type=float, help="تذبذب ضمني سنوي (مثل 0.16) لمناطق الحركة المتوقعة")
    r.add_argument("--out", type=Path)
    r.add_argument("--journal", action="store_true")
    r.add_argument("--levels-file", type=Path, action="append", default=[],
                   help="خطة خارجية بتاريخها؛ تُضاف مستوياتها لجلسة ذلك اليوم فقط")
    r.add_argument("--levels-shift", type=float, default=0.0,
                   help="نقل المستويات الخارجية بالنقاط (مثل فرق ES عن SPX عند الإعادة على ES)")
    r.add_argument("--contract-mode", default="standard", choices=["standard", "cheap"])
    o = sub.add_parser("price-options")
    o.add_argument("replay_dir", type=Path)
    o.add_argument("--root", required=True, help="جذر العقد: QQQ أو SPY أو SPXW أو TSLA ...")
    o.add_argument("--strike-step", type=float, default=1.0)
    o.add_argument("--capital", type=float, default=7000.0)
    o.add_argument("--risk-usd", type=float, default=70.0)
    o.add_argument("--fee", type=float, default=0.65)
    o.add_argument("--cache", type=Path)
    o.add_argument("--out", type=Path)
    a = ap.parse_args()
    if a.cmd == "fetch":
        print(f"حُفظ في {fetch(a.dataset, a.symbol, a.stype, a.start, a.end, a.out)}")
        return
    if a.cmd == "price-options":
        priced = price_trades(load_trades(a.replay_dir), a.root, a.strike_step, cache=a.cache)
        acc = account(priced, a.capital, a.risk_usd, a.fee)
        print(json.dumps(acc, ensure_ascii=False, indent=2))
        if a.out:
            a.out.parent.mkdir(parents=True, exist_ok=True)
            a.out.write_text(json.dumps(dict(account=acc, trades=priced), ensure_ascii=False, indent=1, default=str),
                             encoding="utf-8")
        return
    bars = load_csv(a.csv, a.filter)
    plans = [json.loads(f.read_text(encoding="utf-8")) for f in a.levels_file]
    agg, results = replay(bars, a.symbol, a.scale, a.bin, a.iv, out=a.out, plans=plans,
                          levels_shift=a.levels_shift)
    for rep in results:
        s = rep["summary"]
        print(f"{rep['date']}: إشارات {s['signals']} | مرفوضة {s['rejected']} | صفقات {s['trades']} | {s['total_r']:+}R")
    print(f"\nالإجمالي: {agg['days']} جلسة، {agg['trades']} صفقة، {agg['total_r']:+}R، "
          f"نسبة الربح {agg['win_rate']}، متوسط {agg['avg_r']}R.")
    if not agg["enough_evidence"]:
        print("تنبيه: أقل من 30 صفقة؛ لا تُبنى عليها قرارات تعديل الفلاتر.")
    print("تنبيه: النتيجة على سعر الأداة لا العقد. شغّل price-options قبل أي حكم على العقود.")
    if a.journal:
        for rep in results:
            rep["mode"] = "replay"
            detector.to_journal(rep, "replay", a.contract_mode)
        print("أُضيفت الصفقات إلى السجل")


if __name__ == "__main__":
    main()
