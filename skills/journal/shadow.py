#!/usr/bin/env python3
"""سجل الظل لعقود الخيارات: كل عقد قيّمته المنظومة يُسجَّل، المقبول والمرفوض، مع أسباب الرفض.
بعد وقت الخروج تُحسم نتيجته بسعر العقد الفعلي، ثم يقيس التقرير كل فلتر:
هل العقود التي رفضها كانت ستخسر (فلتر نافع) أم ستربح (فلتر يستحق المراجعة)؟

    python3 skills/journal/shadow.py add <تقرير option-selector أو option-bridge>.json [--fees 0.65]
    python3 skills/journal/shadow.py resolve --prices <أسعار_الخروج>.json [--as-of وقت]   # {"QQQ|2026-11-27|645|call": 5.15}
    python3 skills/journal/shadow.py pending                                 # العقود التي حان حسمها
    python3 skills/journal/shadow.py stats [--min-sample 30]
    python3 skills/journal/shadow.py intraday-stats [--mode paper]           # إشارات الارتداد حسب النمط والمنطقة
"""
from __future__ import annotations

import argparse
import json
import sys
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from journal import _utf8_console, conn  # noqa: E402

DDL = """
CREATE TABLE IF NOT EXISTS option_decisions (
    id              INTEGER PRIMARY KEY,
    created_at      TEXT NOT NULL,
    contract_key    TEXT NOT NULL,          -- الأصل|الانتهاء|السعر|النوع
    underlying      TEXT NOT NULL,
    expiry          TEXT NOT NULL,
    strike          REAL NOT NULL,
    right_          TEXT NOT NULL,
    mode            TEXT NOT NULL,          -- standard / cheap
    horizon         TEXT NOT NULL,
    ask             REAL NOT NULL,
    delta           REAL,
    status          TEXT NOT NULL,          -- eligible / rejected
    reasons         TEXT NOT NULL DEFAULT '',
    selected        INTEGER NOT NULL DEFAULT 0,
    exit_at         TEXT NOT NULL,
    fees            REAL NOT NULL DEFAULT 0,
    exit_price      REAL,
    pnl_usd         REAL,
    return_pct      REAL,
    resolved_at     TEXT,
    UNIQUE(created_at, contract_key, mode, exit_at)
);
CREATE INDEX IF NOT EXISTS idx_option_decisions_open ON option_decisions(resolved_at, exit_at);
"""

REASONS_AR = {
    "over_budget": "فوق الميزانية", "wide_spread": "فارق واسع", "liquidity_unknown": "سيولة غير معروفة",
    "low_open_interest": "عقود قائمة قليلة", "low_volume": "تداول ضعيف", "delta_outside_band": "حساسية خارج النطاق",
    "scenario_reward_risk": "عائد إلى مخاطرة دون الحد", "cheap_payoff": "مضاعف ربح دون الحد",
}


def db():
    c = conn()
    c.executescript(DDL)
    return c


def rows_from_report(r: dict) -> list[dict]:
    """يقبل تقرير option-selector (عقود مباشرة) أو option-bridge (عقود داخل كل تاريخ انتهاء)."""
    out = []
    blocks = r.get("expiries") if "expiries" in r else [r]
    chosen = (r.get("expiry"), (r.get("contract") or {}).get("strike")) if "contract" in r else None
    for b in blocks:
        expiry = b.get("expiry") or (b.get("settlement_at") or r.get("expiry") or "")[:10]
        for c in b.get("contracts", []):
            if "ask" not in c:          # عرض سعر غير صالح، لم يُقيَّم
                continue
            sel = chosen == (expiry, c["strike"]) if chosen else (
                r.get("decision") in ("CANDIDATE", "PAPER_CANDIDATE") and r.get("strike") == c["strike"])
            out.append(dict(underlying=r["underlying"], expiry=expiry, strike=c["strike"], right_=c["right"],
                            mode=r.get("mode", "standard"), horizon=r.get("horizon") or b.get("horizon", "swing"),
                            ask=c["ask"], delta=c.get("delta"),
                            status="eligible" if not c.get("rejected") else "rejected",
                            reasons=",".join(c.get("rejected") or []), selected=int(bool(sel)),
                            exit_at=r["exit_at"], created_at=r["as_of"]))
    return out


def add(report: dict, fees: float = 0.0, c=None) -> int:
    c = c or db()
    n = 0
    for x in rows_from_report(report):
        key = f"{x['underlying']}|{x['expiry']}|{x['strike']:g}|{x['right_']}"
        cur = c.execute(
            "INSERT OR IGNORE INTO option_decisions(created_at,contract_key,underlying,expiry,strike,right_,mode,"
            "horizon,ask,delta,status,reasons,selected,exit_at,fees) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (x["created_at"], key, x["underlying"], x["expiry"], x["strike"], x["right_"], x["mode"], x["horizon"],
             x["ask"], x["delta"], x["status"], x["reasons"], x["selected"], x["exit_at"], fees))
        n += cur.rowcount
    c.commit()
    return n


def resolve(prices: dict, now: datetime | None = None, c=None) -> int:
    """يحسم العقود التي حلّ وقت خروجها. السعر = سعر بيع العقد الفعلي عند الخروج (أو قيمته عند التسوية)."""
    c = c or db()
    now = now or datetime.now(timezone.utc)
    n = 0
    for row in c.execute("SELECT id, contract_key, ask, fees, exit_at FROM option_decisions WHERE resolved_at IS NULL"):
        if datetime.fromisoformat(row["exit_at"]) > now or row["contract_key"] not in prices:
            continue
        px = float(prices[row["contract_key"]])
        pnl = round((px - row["ask"]) * 100 - row["fees"], 2)
        cost = row["ask"] * 100 + row["fees"]
        c.execute("UPDATE option_decisions SET exit_price=?, pnl_usd=?, return_pct=?, resolved_at=? WHERE id=?",
                  (px, pnl, round(pnl / cost * 100, 2) if cost else None, now.isoformat(timespec="seconds"), row["id"]))
        n += 1
    c.commit()
    return n


def pending(now: datetime | None = None, c=None) -> list[str]:
    c = c or db()
    now = now or datetime.now(timezone.utc)
    keys = {r["contract_key"] for r in c.execute(
        "SELECT contract_key, exit_at FROM option_decisions WHERE resolved_at IS NULL")
        if datetime.fromisoformat(r["exit_at"]) <= now}
    return sorted(keys)


def summarize(rets: list[float]) -> dict:
    if not rets:
        return dict(n=0, win_rate=None, avg_return_pct=None)
    return dict(n=len(rets), win_rate=round(sum(r > 0 for r in rets) / len(rets) * 100, 1),
                avg_return_pct=round(sum(rets) / len(rets), 2))


def stats(min_sample: int = 30, c=None) -> dict:
    c = c or db()
    rows = list(c.execute("SELECT * FROM option_decisions WHERE resolved_at IS NOT NULL"))
    out = {}
    for mode in sorted({r["mode"] for r in rows}):
        rs = [r for r in rows if r["mode"] == mode]
        eligible = summarize([r["return_pct"] for r in rs if r["status"] == "eligible"])
        selected = summarize([r["return_pct"] for r in rs if r["selected"]])
        by_reason, sole = defaultdict(list), defaultdict(list)
        for r in rs:
            reasons = [x for x in r["reasons"].split(",") if x]
            for x in reasons:
                by_reason[x].append(r["return_pct"])
            if len(reasons) == 1:
                sole[reasons[0]].append(r["return_pct"])
        filters = {}
        for reason in sorted(by_reason):
            s_all, s_sole = summarize(by_reason[reason]), summarize(sole[reason])
            if s_sole["n"] < min_sample or eligible["n"] < min_sample:
                verdict = "عينة صغيرة"
            elif s_sole["avg_return_pct"] >= eligible["avg_return_pct"]:
                verdict = "يستحق المراجعة: ما رفضه وحده أدّى مثل المقبول أو أفضل"
            elif s_sole["avg_return_pct"] < 0:
                verdict = "نافع: ما رفضه وحده كان خاسرًا في المتوسط"
            else:
                verdict = "محايد: ما رفضه رابح لكنه أضعف من المقبول"
            filters[reason] = dict(all=s_all, sole=s_sole, verdict=verdict)
        out[mode] = dict(eligible=eligible, selected=selected, filters=filters)
    return out


def zone_kind(zone: str | None) -> str:
    z = zone or ""
    for key, kind in (("لحركة المتوقعة", "الحركة المتوقعة"), ("نقطة التحكم", "ملف الحجم"),
                      ("نقطة تحكم", "ملف الحجم"), ("منطقة القيمة", "ملف الحجم"), ("عقدة حجم", "ملف الحجم"),
                      ("رقم مستدير", "رقم مستدير"),
                      ("الجلسة السابق", "طرف الجلسة"), ("اليوم السابق", "اليوم السابق"),
                      ("الجلسة الليلية", "الجلسة الليلية")):
        if key in z:
            return kind
    return z or "بلا منطقة"


def intraday_stats(min_sample: int = 30, c=None, mode: str | None = None) -> list[dict]:
    """نتائج إشارات الارتداد مجمّعة حسب النمط ونوع المنطقة ونوع العقد."""
    c = c or db()
    q = "SELECT pattern, zone, contract_mode, result_r FROM intraday_signals WHERE result_r IS NOT NULL"
    args = ()
    if mode:
        q += " AND mode=?"
        args = (mode,)
    groups = defaultdict(list)
    for r in c.execute(q, args):
        groups[(r["pattern"] or "؟", zone_kind(r["zone"]), r["contract_mode"])].append(r["result_r"])
    out = []
    for (pattern, kind, cmode), rs in sorted(groups.items()):
        out.append(dict(pattern=pattern, zone_kind=kind, contract_mode=cmode, n=len(rs),
                        win_rate=round(sum(x > 0 for x in rs) / len(rs) * 100, 1),
                        avg_r=round(sum(rs) / len(rs), 2), enough=len(rs) >= min_sample))
    return out


def print_stats(s: dict):
    if not s:
        print("لا عقود محسومة بعد.")
        return
    for mode, m in s.items():
        print(f"\n## النمط: {'العادي' if mode == 'standard' else 'العقود الرخيصة'}")
        for label, v in (("المقبولة", m["eligible"]), ("المرشحة فعلًا", m["selected"])):
            print(f"{label}: {v['n']} عقد، نسبة الربح {v['win_rate']}%، متوسط العائد {v['avg_return_pct']}%")
        print("\n| الفلتر | رفض (الكل) | رفض وحده | متوسط عائد ما رفضه وحده | الحكم |")
        print("|---|---|---|---|---|")
        for k, f in m["filters"].items():
            print(f"| {REASONS_AR.get(k, k)} | {f['all']['n']} | {f['sole']['n']} | "
                  f"{f['sole']['avg_return_pct']}% | {f['verdict']} |")


def main():
    _utf8_console()
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sp = ap.add_subparsers(dest="cmd", required=True)
    p = sp.add_parser("add"); p.add_argument("report", type=Path); p.add_argument("--fees", type=float, default=0.0)
    p = sp.add_parser("resolve"); p.add_argument("--prices", type=Path, required=True)
    p.add_argument("--as-of", help="وقت الحسم (للمحاكاة التاريخية)، بصيغة ISO مع المنطقة الزمنية")
    p = sp.add_parser("pending"); p.add_argument("--as-of")
    p = sp.add_parser("stats"); p.add_argument("--min-sample", type=int, default=30)
    p = sp.add_parser("intraday-stats"); p.add_argument("--min-sample", type=int, default=30)
    p.add_argument("--mode", choices=["replay", "paper", "live"])
    a = ap.parse_args()
    at = datetime.fromisoformat(a.as_of) if getattr(a, "as_of", None) else None
    if a.cmd == "add":
        print(f"سُجّل {add(json.loads(a.report.read_text(encoding='utf-8')), a.fees)} عقد في سجل الظل")
    elif a.cmd == "resolve":
        print(f"حُسم {resolve(json.loads(a.prices.read_text(encoding='utf-8')), at)} عقد")
    elif a.cmd == "pending":
        keys = pending(at)
        print("\n".join(keys) if keys else "لا عقود تنتظر الحسم.")
    elif a.cmd == "intraday-stats":
        rows = intraday_stats(a.min_sample, mode=a.mode)
        if not rows:
            print("لا إشارات محسومة بعد.")
        print("| النمط | نوع المنطقة | العقد | العدد | نسبة الربح | متوسط العائد بوحدة المخاطرة | الحكم |")
        print("|---|---|---|---|---|---|---|")
        for r in rows:
            print(f"| {r['pattern']} | {r['zone_kind']} | {r['contract_mode']} | {r['n']} | {r['win_rate']}% | "
                  f"{r['avg_r']:+} | {'كافية' if r['enough'] else 'عينة صغيرة'} |")
    else:
        print_stats(stats(a.min_sample))


if __name__ == "__main__":
    main()
