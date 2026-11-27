"""Compare one listed option series (index, ETF or stock), one purchased contract at a time.\n\nAllowed underlyings and their rules live in config/options-universe.yaml."""
import argparse
import json
import math
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from statistics import NormalDist

import yaml


def stamp(value):
    dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if dt.utcoffset() is None:
        raise ValueError("timezone required")
    return dt


def number(value, minimum=0):
    if type(value) not in (float, int) or not math.isfinite(value) or value < minimum:
        raise ValueError("invalid numeric input")
    return value


def option_value(spot, strike, side, years, iv, rate, dividend):
    sign = 1 if side == "call" else -1
    if years == 0:
        return max(sign * (spot - strike), 0)
    vol = iv * math.sqrt(years)
    d1 = (math.log(spot / strike) + (rate - dividend + iv**2 / 2) * years) / vol
    normal = NormalDist()
    return sign * (spot * math.exp(-dividend * years) * normal.cdf(sign * d1)
                   - strike * math.exp(-rate * years) * normal.cdf(sign * (d1 - vol)))


ROOT = Path(__file__).resolve().parents[2]
YEAR = 365 * 86400


def load_universe(path=None):
    return yaml.safe_load(Path(path or ROOT / "config/options-universe.yaml").read_text(encoding="utf-8"))


def resolve(universe, name):
    """يعيد (الرمز المعتمد، قواعده بعد دمج القيم الافتراضية) أو يرفع خطأ إن لم يكن معتمدًا."""
    for key, spec in universe["underlyings"].items():
        if name == key or name in spec.get("aliases", []):
            return key, {**universe["defaults"], **spec}
    raise ValueError(f"underlying {name} not in config/options-universe.yaml")


def add_trading_days(dt, n):
    """يضيف أيام تداول بتخطي السبت والأحد (العطل الرسمية غير محسوبة)."""
    while n > 0:
        dt += timedelta(days=1)
        if dt.weekday() < 5:
            n -= 1
    return dt


def option_delta(spot, strike, side, years, iv, rate, dividend):
    if years <= 0 or iv <= 0:
        return float(side == "call" and spot > strike) - float(side == "put" and spot < strike)
    d1 = (math.log(spot / strike) + (rate - dividend + iv**2 / 2) * years) / (iv * math.sqrt(years))
    n = NormalDist().cdf
    return math.exp(-dividend * years) * (n(d1) if side == "call" else n(d1) - 1)


def compare(data, rules, now, universe=None):
    universe = universe or load_universe()
    name = data.get("underlying") or data["series"]
    if not str(data.get("source", "")).strip():
        raise ValueError("provide source")
    key, u = resolve(universe, name)
    horizon = data.get("horizon", "intraday")
    as_of = stamp(data["as_of"])
    last_trade = stamp(data["last_trade_at"])
    settlement = stamp(data["settlement_at"])
    exit_at = stamp(data["exit_at"])
    physical = u["settlement"] == "physical"
    report = {"decision": "WAIT", "underlying": key, "series": data.get("series", key), "source": data["source"],
              "as_of": as_of.isoformat(), "horizon": horizon, "settlement": u["settlement"],
              "exercise": u["exercise"], "expiry": settlement.date().isoformat(), "contracts": [], "warnings": []}
    if horizon not in u["horizons"]:
        return dict(report, reason="horizon_not_allowed", allowed=u["horizons"])
    if as_of >= last_trade or now >= last_trade:
        return dict(report, reason="trading_ended")
    if not (as_of <= now < exit_at <= settlement and last_trade <= settlement):
        raise ValueError("invalid trading, exit or settlement times")
    if last_trade <= exit_at < settlement:
        raise ValueError("exit during non-tradable interval")
    if physical:
        # عقود التسليم الفعلي لا تُحمل حتى الانتهاء: تُغلق قبل آخر تداول
        cutoff = last_trade - timedelta(minutes=u["close_before_expiry_minutes"])
        report["must_close_by"] = cutoff.isoformat()
        if exit_at > cutoff:
            return dict(report, reason="physical_settlement_exit_after_cutoff")
    if data["feed"] != "live" or (now - as_of).total_seconds() > rules["max_quote_age_seconds"]:
        return dict(report, reason="stale_or_nonlive_snapshot")
    if u.get("earnings_check"):
        if "earnings_date" not in data:
            report["warnings"].append("earnings_date_unknown")
        elif data["earnings_date"] and as_of < stamp(data["earnings_date"]) <= exit_at \
                and not data.get("allow_through_earnings"):
            return dict(report, reason="earnings_in_window", earnings_date=data["earnings_date"])
    iv_rank = data.get("iv_rank")
    if iv_rank is None:
        report["warnings"].append("iv_rank_unknown")
    elif number(iv_rank) > u["max_iv_rank"]:
        report["iv_rank"] = iv_rank
        return dict(report, reason="iv_expensive_vs_history")
    mode = data.get("mode", "standard")
    if mode not in ("standard", "cheap"):
        raise ValueError("mode must be standard or cheap")
    cheap = u.get("cheap_mode", {}) if mode == "cheap" else None
    report["mode"] = mode
    if cheap is not None and cheap.get("requires_signal", True) and not data.get("signal"):
        return dict(report, reason="cheap_mode_requires_signal")
    side = data["side"]
    spot, target, stop = [number(data[k], 0.000001) for k in ("spot", "target", "invalidation")]
    if data.get("levels_from") == "SPX" and u.get("scale_from_spx"):
        spot, target, stop = [round(x * u["scale_from_spx"], 4) for x in (spot, target, stop)]
        report["scaled_levels"] = dict(spot=spot, target=target, invalidation=stop)
    if not ((side == "call" and stop < spot < target) or
            (side == "put" and target < spot < stop)):
        raise ValueError("direction inconsistent with target or invalidation")
    mult = u["multiplier"]
    years = (settlement - exit_at).total_seconds() / YEAR
    years_now = (settlement - as_of).total_seconds() / YEAR
    # الإلغاء يُضرب غالبًا مبكرًا: في صفقات الأيام والأسابيع يُقيَّم بعد stop_scenario_days من أيام التداول
    stop_at = exit_at
    if horizon == "swing":
        stop_at = min(add_trading_days(as_of, u.get("swing_expiry", {}).get("stop_scenario_days", 2)), exit_at)
    years_stop = (settlement - stop_at).total_seconds() / YEAR
    rate = number(data["rate"], -1)
    dividend = number(data["dividend_yield"])
    fees = number(data["fees_per_contract_usd"])
    capital = number(rules["capital_usd"])
    # علاوة العقد = أقصى خسارة، فحدود الأسهم لا تنطبق. السقف من options_max_premium_pct.
    budget = min(number(data["available_cash_usd"]), number(data["risk_budget_usd"]),
                 capital * number(rules["options_max_premium_pct"]) / 100)
    if cheap is not None:
        budget = min(budget, number(cheap["max_premium_usd"]))
    report.update(budget_usd=budget, exit_at=exit_at.isoformat(), stop_scenario_at=stop_at.isoformat(),
                  valuation=("model_constant_iv" if years else "settlement")
                  + ("_american_approx" if u["exercise"] == "american" and years else ""))
    lo_d, hi_d = cheap["delta_band"] if cheap is not None else u["delta_band"]
    spread_by_horizon = u.get("max_spread_pct") or {}
    max_spread = cheap["max_spread_pct"] if cheap is not None else \
        spread_by_horizon.get(horizon, rules["max_option_spread_pct"])
    report["max_spread_pct"] = max_spread
    strikes = {}
    seen = set()
    for q in data["quotes"]:
        strike = number(q["strike"], 0.000001)
        right = q["right"]
        key2 = (strike, right)
        if right not in ("call", "put") or key2 in seen:
            raise ValueError("invalid or duplicate strike/right in series")
        seen.add(key2)
        strikes.setdefault(strike, {})
        bid, ask = number(q["bid"]), number(q["ask"])
        age = (now - stamp(q["quoted_at"])).total_seconds()
        if not (0 <= age <= rules["max_quote_age_seconds"] and 0 < bid <= ask):
            report["contracts"].append(dict(strike=strike, right=right, reason="invalid_or_stale_quote"))
            continue
        strikes[strike][right] = ask
        if right != side:
            continue
        iv = number(q["iv"], 0.000001)
        pnl = {}
        for label, level, yrs in (("target", target, years), ("flat", spot, years), ("stop", stop, years_stop)):
            value = option_value(level, strike, side, yrs, iv if yrs else 0,
                                 rate if yrs else 0, dividend if yrs else 0)
            pnl[label] = round((value - ask) * mult - fees, 2)
        cost = round(ask * mult + fees, 2)
        ratio = pnl["target"] / -pnl["stop"] if pnl["stop"] < 0 else None
        spread = (ask - bid) / ask * 100
        delta = q.get("delta")
        if delta is not None:
            number(delta, -1)
            if not 0 <= delta * (1 if side == "call" else -1) <= 1:
                raise ValueError("invalid signed delta")
            delta_src = "source"
        else:
            delta = round(option_delta(spot, strike, side, years_now, iv, rate, dividend), 4)
            delta_src = "model"
        reasons = []
        if cost > budget:
            reasons.append("over_budget")
        if spread > max_spread:
            reasons.append("wide_spread")
        oi, vol = q.get("open_interest"), q.get("volume")
        if oi is None or vol is None:
            reasons.append("liquidity_unknown")
        else:
            if number(oi) < u["min_open_interest"]:
                reasons.append("low_open_interest")
            if number(vol) < u["min_volume"]:
                reasons.append("low_volume")
        if not lo_d <= abs(delta) <= hi_d:
            reasons.append("delta_outside_band")
        payoff = round(pnl["target"] / cost, 3) if cost else None
        if cheap is not None:
            # العلاوة كلها هي الإلغاء: يُقاس الربح عند الهدف بمضاعف العلاوة
            if payoff is None or payoff < cheap["min_payoff_multiple"]:
                reasons.append("cheap_payoff")
        elif ratio is None or ratio < rules["min_reward_risk"]:
            reasons.append("scenario_reward_risk")
        band = "otm" if abs(delta) < 0.42 else "atm" if abs(delta) <= 0.58 else "itm"
        report["contracts"].append(dict(strike=strike, right=side, ask=ask, bid=bid,
            quoted_at=q["quoted_at"], delta=delta, delta_source=delta_src, moneyness=band, iv=q.get("iv"),
            open_interest=oi, volume=vol,
            max_loss_usd=cost, breakeven=round(strike + (1 if side == "call" else -1) * cost / mult, 4),
            pnl_usd=pnl, reward_risk=ratio, payoff_multiple=payoff, spread_pct=round(spread, 3), rejected=reasons))
    if strikes:
        atm = min(strikes, key=lambda k: (abs(k - spot), k))
        pair = strikes[atm]
        if set(pair) == {"call", "put"}:
            debit = pair["call"] + pair["put"]
            report["atm_straddle"] = dict(strike=atm, ask_points=round(debit, 4),
                settlement_breakevens=[round(atm - debit, 4), round(atm + debit, 4)])
    valid = [q for q in report["contracts"] if q.get("rejected") == []]
    if not valid:
        return dict(report, reason="no_eligible_contract")
    best = max(valid, key=lambda q: (q["pnl_usd"]["target"], -q["max_loss_usd"]))
    if not rules["allow_options"]:
        return dict(report, reason="options_disabled", paper_candidate=best["strike"])
    return dict(report, decision="CANDIDATE", strike=best["strike"])


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("snapshot", type=Path)
    parser.add_argument("--replay", action="store_true", help="evaluate at snapshot time, paper only")
    parser.add_argument("--journal", action="store_true", help="record every evaluated contract in the shadow log")
    parser.add_argument("--fees", type=float, default=None, help="fees per contract for the shadow log")
    args = parser.parse_args()
    root = Path.cwd()
    try:
        rules = yaml.safe_load((root / "config/risk-rules.yaml").read_text(encoding="utf-8"))
        data = json.loads(args.snapshot.read_text(encoding="utf-8"))
        now = stamp(data["as_of"]) if args.replay else datetime.now(timezone.utc)
        result = compare(data, rules, now)
        if args.replay:
            result["mode"] = "historical_simulation"
            if result["decision"] == "CANDIDATE":
                result["decision"] = "PAPER_CANDIDATE"
    except (ValueError, KeyError, TypeError, AttributeError, OSError, OverflowError) as exc:
        result = dict(decision="WAIT", reason=f"input_error: {exc}")
    print(json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False))
    if args.journal and result.get("contracts"):
        sys.path.insert(0, str(ROOT / "skills" / "journal"))
        from shadow import add
        fees = args.fees if args.fees is not None else data.get("fees_per_contract_usd", 0)
        print(f"سجل الظل: {add(result, fees)} عقد")


if __name__ == "__main__":
    main()
