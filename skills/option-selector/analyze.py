"""Compare one listed SPX or SPXW series, one purchased contract at a time."""
import argparse
import json
import math
from datetime import datetime, timezone
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


def compare(data, rules, now):
    series = data["series"]
    if series not in ("SPX", "SPXW") or not data["source"].strip():
        raise ValueError("provide SPX/SPXW and source")
    as_of = stamp(data["as_of"])
    last_trade = stamp(data["last_trade_at"])
    settlement = stamp(data["settlement_at"])
    exit_at = stamp(data["exit_at"])
    report = {"decision": "WAIT", "series": series, "source": data["source"],
              "as_of": as_of.isoformat(), "contracts": []}
    if as_of >= last_trade or now >= last_trade:
        return dict(report, reason="trading_ended")
    if not (as_of <= now < exit_at <= settlement and last_trade <= settlement):
        raise ValueError("invalid trading, exit or settlement times")
    if last_trade <= exit_at < settlement:
        raise ValueError("exit during non-tradable interval")
    if data["feed"] != "live" or (now - as_of).total_seconds() > rules["max_quote_age_seconds"]:
        return dict(report, reason="stale_or_nonlive_snapshot")
    side = data["side"]
    spot, target, stop = [number(data[k], 0.000001) for k in ("spot", "target", "invalidation")]
    if not ((side == "call" and stop < spot < target) or
            (side == "put" and target < spot < stop)):
        raise ValueError("direction inconsistent with target or invalidation")
    years = (settlement - exit_at).total_seconds() / (365 * 86400)
    rate = number(data["rate"], -1) if years else 0
    dividend = number(data["dividend_yield"]) if years else 0
    fees = number(data["fees_per_contract_usd"])
    capital = number(rules["capital_usd"])
    budget = min(number(data["available_cash_usd"]), number(data["risk_budget_usd"]),
                 capital * min(rules["max_position_pct"], rules["max_loss_per_trade_pct"],
                               100 - rules["cash_buffer_pct"]) / 100)
    report.update(budget_usd=budget, exit_at=exit_at.isoformat(),
                  valuation="model_constant_iv" if years else "settlement")
    strikes = {}
    seen = set()
    for q in data["quotes"]:
        strike = number(q["strike"], 0.000001)
        right = q["right"]
        key = (strike, right)
        if right not in ("call", "put") or key in seen:
            raise ValueError("invalid or duplicate strike/right in series")
        seen.add(key)
        strikes.setdefault(strike, {})
        bid, ask = number(q["bid"]), number(q["ask"])
        age = (now - stamp(q["quoted_at"])).total_seconds()
        if not (0 <= age <= rules["max_quote_age_seconds"] and 0 < bid <= ask):
            report["contracts"].append(dict(strike=strike, right=right, reason="invalid_or_stale_quote"))
            continue
        strikes[strike][right] = ask
        if right != side:
            continue
        iv = number(q["iv"], 0.000001) if years else 0
        pnl = {}
        for label, level in (("target", target), ("flat", spot), ("stop", stop)):
            value = option_value(level, strike, side, years, iv, rate, dividend)
            pnl[label] = round((value - ask) * 100 - fees, 2)
        cost = round(ask * 100 + fees, 2)
        ratio = pnl["target"] / -pnl["stop"] if pnl["stop"] < 0 else None
        spread = (ask - bid) / ask * 100
        reasons = []
        if cost > budget:
            reasons.append("over_budget")
        if spread > rules["max_option_spread_pct"]:
            reasons.append("wide_spread")
        if ratio is None or ratio < rules["min_reward_risk"]:
            reasons.append("scenario_reward_risk")
        delta = q.get("delta")
        if delta is not None:
            number(delta, -1)
            if not 0 <= delta * (1 if side == "call" else -1) <= 1:
                raise ValueError("invalid signed delta")
        report["contracts"].append(dict(strike=strike, right=side, ask=ask, bid=bid,
            quoted_at=q["quoted_at"], delta=delta, iv=q.get("iv"),
            max_loss_usd=cost, breakeven=round(strike + (1 if side == "call" else -1) * cost / 100, 4),
            pnl_usd=pnl, reward_risk=ratio, spread_pct=round(spread, 3), rejected=reasons))
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


if __name__ == "__main__":
    main()
