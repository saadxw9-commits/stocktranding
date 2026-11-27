#!/usr/bin/env python3
"""جسر من حكم طبقة التحقق إلى اختيار عقد الخيارات لصفقات الأيام والأسابيع.

المسار: Jake وtradermonty والعدسات ← stock-analyst (آراء موحدة) ← verification-layer (حكم ومستويات متحفظة)
← هذا الجسر ← option-selector (لكل تاريخ انتهاء) ← أفضل عقد.

خطوتان:
    plan    من تقرير التحقق: الاتجاه (Call/Put)، المستويات، وقت الخروج، ونافذة تواريخ الانتهاء المناسبة.
    select  من الخطة وسلسلة عقود فعلية لعدة تواريخ انتهاء: يشغّل option-selector لكل تاريخ بأفق خروج
            واحد، ثم يختار أعلى ربح عند الهدف بين المرشحين.

    python3 skills/option-bridge/bridge.py plan reports/verification/verify-TSLA-2026-11-27.json --out plan.json
    python3 skills/option-bridge/bridge.py select plan.json chain.json --cash 2000 --budget 300 [--mode cheap] [--replay]
"""
from __future__ import annotations

import argparse
import json
import math
import sys
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "skills" / "option-selector"))
from analyze import compare, load_universe, resolve, stamp  # noqa: E402

ET = timezone(timedelta(hours=-5))  # توقيت نيويورك الشتوي؛ الطوابع في اللقطات تحمل منطقتها الفعلية
READY = "صالح للعرض"
PENDING = "اقتراح معلّق — يحتاج مراجعتك"


def add_trading_days(d: date, n: int) -> date:
    """يضيف أيام تداول بتخطي السبت والأحد (العطل الرسمية غير محسوبة؛ تحقق من التقويم)."""
    while n > 0:
        d += timedelta(days=1)
        if d.weekday() < 5:
            n -= 1
    return d


def plan(ver: dict, universe: dict) -> dict:
    p = ver.get("primary", {})
    out = dict(symbol=ver["symbol"], decision=ver["decision"], confidence=ver.get("consensus_confidence"),
               method=p.get("method"))
    if ver["decision"] not in (READY, PENDING):
        return dict(out, status="blocked", reason="verification_not_ready")
    side = {"long": "call", "short": "put"}.get(p.get("direction"))
    if not side:
        return dict(out, status="blocked", reason="no_direction")
    lv = ver.get("conservative_levels")
    if not lv or None in (lv.get("entry"), lv.get("stop"), lv.get("target")):
        return dict(out, status="blocked", reason="missing_levels")
    try:
        key, u = resolve(universe, ver["symbol"])
    except ValueError:
        return dict(out, status="blocked", reason="not_in_options_universe",
                    hint="أضف الرمز إلى config/options-universe.yaml أولًا")
    hd = p.get("horizon_days")
    if hd is None:
        return dict(out, status="blocked", reason="missing_horizon")
    horizon = "intraday" if hd == 0 else "swing"
    if horizon not in u["horizons"]:
        return dict(out, status="blocked", reason="horizon_not_allowed", allowed=u["horizons"])
    start = parse_time(p.get("as_of")) or datetime.combine(date.fromisoformat(ver["verified_at"][:10]),
                                                            time(9, 30), ET)
    exit_day = add_trading_days(start.date(), hd)
    exit_at = datetime.combine(exit_day, time(15, 30), start.tzinfo or ET)
    se = u.get("swing_expiry", {})
    hold_cal = (exit_day - start.date()).days
    min_dte = max(se.get("min_dte", 21), math.ceil(hold_cal * se.get("hold_multiple", 2))) if hd else 0
    return dict(out, status="ready" if ver["decision"] == READY else "pending_owner",
                underlying=key, side=side, horizon=horizon, horizon_days=hd,
                entry=lv["entry"], target=lv["target"], invalidation=lv["stop"],
                planned_at=start.isoformat(), exit_at=exit_at.isoformat(),
                expiry_window=dict(min_dte=min_dte, max_dte=min_dte + se.get("window_days", 45) if hd else 0),
                entry_tolerance_pct=se.get("entry_tolerance_pct", 1.0),
                needs=["سلسلة عقود فعلية لتواريخ الانتهاء ضمن النافذة", "iv_rank"]
                + (["earnings_date"] if u.get("earnings_check") else []))


def parse_time(v: str | None) -> datetime | None:
    """وقت الرأي؛ إن خلا من المنطقة الزمنية يُعد بتوقيت نيويورك."""
    if not v:
        return None
    dt = datetime.fromisoformat(v.replace("Z", "+00:00"))
    return dt if dt.utcoffset() is not None else dt.replace(tzinfo=ET)


def entry_state(pl: dict, spot: float) -> str | None:
    tol = pl["entry_tolerance_pct"] / 100
    if pl["side"] == "call":
        if spot <= pl["invalidation"]:
            return "invalidated"
        if spot >= pl["target"]:
            return "target_already_reached"
        if spot < pl["entry"] * (1 - tol):
            return "wait_for_entry"
    else:
        if spot >= pl["invalidation"]:
            return "invalidated"
        if spot <= pl["target"]:
            return "target_already_reached"
        if spot > pl["entry"] * (1 + tol):
            return "wait_for_entry"
    return None


def select(pl: dict, chain: dict, rules: dict, universe: dict, cash: float, budget: float,
           fees: float = 0.0, mode: str = "standard", now: datetime | None = None, signal: dict | None = None) -> dict:
    if pl.get("status") not in ("ready", "pending_owner"):
        return dict(decision="WAIT", reason=f"plan_{pl.get('status')}", plan=pl)
    if chain["underlying"] != pl["underlying"]:
        raise ValueError("chain underlying differs from plan")
    as_of = stamp(chain["as_of"])
    now = now or datetime.now(timezone.utc)
    spot = chain["spot"]
    state = entry_state(pl, spot)
    report = dict(underlying=pl["underlying"], side=pl["side"], spot=spot, as_of=chain["as_of"],
                  exit_at=pl["exit_at"], status=pl["status"], mode=mode, expiries=[])
    if state:
        return dict(report, decision="WAIT", reason=state)
    w = pl["expiry_window"]
    for e in chain["expiries"]:
        dte = (date.fromisoformat(e["expiry"]) - as_of.date()).days
        row = dict(expiry=e["expiry"], dte=dte)
        if pl["horizon"] == "swing" and not (w["min_dte"] <= dte <= w["max_dte"]):
            report["expiries"].append(dict(row, decision="SKIP", reason="outside_expiry_window"))
            continue
        if stamp(e["last_trade_at"]) <= stamp(pl["exit_at"]):
            report["expiries"].append(dict(row, decision="SKIP", reason="expires_before_exit"))
            continue
        snap = dict(underlying=pl["underlying"], series=e.get("series", pl["underlying"]), source=chain["source"],
                    feed=chain.get("feed", "live"), as_of=chain["as_of"], last_trade_at=e["last_trade_at"],
                    settlement_at=e["settlement_at"], exit_at=pl["exit_at"], horizon=pl["horizon"],
                    side=pl["side"], spot=spot, target=pl["target"], invalidation=pl["invalidation"],
                    available_cash_usd=cash, risk_budget_usd=budget, fees_per_contract_usd=fees,
                    rate=chain.get("rate", 0.04), dividend_yield=chain.get("dividend_yield", 0.0),
                    quotes=e["quotes"], mode=mode)
        for k in ("iv_rank", "earnings_date", "allow_through_earnings"):
            if k in chain:
                snap[k] = chain[k]
        if signal:
            snap["signal"] = signal
        r = compare(snap, rules, now, universe)
        best = None
        if r["decision"] == "CANDIDATE" or r.get("paper_candidate") is not None:
            strike = r.get("strike", r.get("paper_candidate"))
            best = next(c for c in r["contracts"] if c.get("strike") == strike and c.get("right") == pl["side"])
        report["expiries"].append(dict(row, decision=r["decision"], reason=r.get("reason"),
                                       must_close_by=r.get("must_close_by"), warnings=r.get("warnings"),
                                       best=best, rejected={c["strike"]: c.get("rejected") or c.get("reason")
                                                            for c in r["contracts"]}))
    cands = [x for x in report["expiries"] if x.get("best")]
    if not cands:
        return dict(report, decision="WAIT", reason="no_eligible_contract")
    win = max(cands, key=lambda x: (x["best"]["pnl_usd"]["target"], -x["best"]["max_loss_usd"]))
    decision = "CANDIDATE" if rules.get("allow_options") else "PAPER_CANDIDATE"
    if pl["status"] == "pending_owner":
        decision = "PENDING_OWNER"
    return dict(report, decision=decision, expiry=win["expiry"], contract=win["best"],
                must_close_by=win.get("must_close_by"))


RIGHT = {"call": "شراء Call", "put": "شراء Put"}


def describe(r: dict) -> list[str]:
    if "contract" not in r:
        return [f"القرار: انتظر ({r.get('reason')})"]
    c = r["contract"]
    lines = [f"القرار: {r['decision']} — {RIGHT[r['side']]} على {r['underlying']}، انتهاء {r['expiry']}، سعر التنفيذ {c['strike']}",
             f"سعر الشراء {c['ask']} (أقصى خسارة {c['max_loss_usd']}$)، الحساسية {c['delta']} ({c['moneyness']})، الفارق {c['spread_pct']}%",
             f"عند الهدف {c['pnl_usd']['target']:+}$ | عند الثبات {c['pnl_usd']['flat']:+}$ | عند الإلغاء {c['pnl_usd']['stop']:+}$ "
             f"(تقدير بتذبذب ثابت حتى {r['exit_at']})"]
    if r.get("must_close_by"):
        lines.append(f"أغلق العقد قبل {r['must_close_by']} (تسوية بتسليم الأسهم)")
    for x in r["expiries"]:
        if x is not None and x.get("expiry") != r["expiry"]:
            lines.append(f"  {x['expiry']} ({x['dte']} يومًا): {x['decision']} {x.get('reason') or ''}".rstrip())
    return lines


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    a1 = sub.add_parser("plan")
    a1.add_argument("verification", type=Path)
    a1.add_argument("--out", type=Path)
    a2 = sub.add_parser("select")
    a2.add_argument("plan", type=Path)
    a2.add_argument("chain", type=Path)
    a2.add_argument("--cash", type=float, required=True, help="النقد المتاح للصفقة بعد الاحتياطي")
    a2.add_argument("--budget", type=float, required=True, help="ميزانيتك لهذه الصفقة")
    a2.add_argument("--fees", type=float, default=0.0)
    a2.add_argument("--mode", default="standard", choices=["standard", "cheap"])
    a2.add_argument("--signal", type=Path, help="إشارة مؤكدة (مطلوبة لنمط العقود الرخيصة)")
    a2.add_argument("--replay", action="store_true", help="تقييم في وقت اللقطة، ورقي فقط")
    a2.add_argument("--out", type=Path)
    a = ap.parse_args()
    universe = load_universe()
    if a.cmd == "plan":
        result = plan(json.loads(a.verification.read_text(encoding="utf-8")), universe)
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        rules = yaml.safe_load((ROOT / "config/risk-rules.yaml").read_text(encoding="utf-8"))
        chain = json.loads(a.chain.read_text(encoding="utf-8"))
        sig = json.loads(a.signal.read_text(encoding="utf-8")) if a.signal else None
        result = select(json.loads(a.plan.read_text(encoding="utf-8")), chain, rules, universe,
                        a.cash, a.budget, a.fees, a.mode, stamp(chain["as_of"]) if a.replay else None, sig)
        if a.replay:
            result["mode_run"] = "historical_simulation"
        print("\n".join(describe(result)))
    if a.out:
        a.out.parent.mkdir(parents=True, exist_ok=True)
        a.out.write_text(json.dumps(result, ensure_ascii=False, indent=2, default=str), encoding="utf-8")


if __name__ == "__main__":
    main()
