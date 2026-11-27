#!/usr/bin/env python3
"""سجل المنظومة: يحفظ كل رأي وحكم تحقق واقتراح وتنفيذ، ويقيس جودة الاقتراحات.

قاعدة البيانات المحلية: db/journal.sqlite (لا تُرفع إلى المستودع). المخطط في db/schema.sql.

أمثلة:
    python3 journal.py init
    python3 journal.py add-opinions reports/opinions/NVDA-2026-11-27.json
    python3 journal.py add-verification reports/verification/verify-NVDA-2026-11-27.json
    python3 journal.py add-suggestion --verification 3 --shares 4 --amount 480 --max-loss 20 --horizon 10
    python3 journal.py add-execution --suggestion 1 --symbol NVDA --side buy --qty 4 --price 120.5
    python3 journal.py evaluate --prices prices.json     # يحسم الاقتراحات المفتوحة من القمم والقيعان اليومية
    python3 journal.py resolve 1 --status hit_target --price 132
    python3 journal.py stats
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from collections import defaultdict
from datetime import datetime, timedelta
from pathlib import Path

def _find_root() -> Path:
    """جذر المستودع: أول مجلد أعلى يحوي config/risk-rules.yaml. يعمل من skills/ ومن النسخ في .claude/skills/."""
    here = Path(__file__).resolve()
    for p in here.parents:
        if (p / "config" / "risk-rules.yaml").exists():
            return p
    return here.parents[2]


ROOT = _find_root()

def _utf8_console() -> None:
    """طرفية ويندوز قد تكون بترميز cp1252 عند تحويل المخرجات؛ النص العربي يحتاج UTF-8."""
    for s in (sys.stdout, sys.stderr):
        if hasattr(s, "reconfigure"):
            s.reconfigure(encoding="utf-8")

DB = ROOT / "db" / "journal.sqlite"


def now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def conn() -> sqlite3.Connection:
    DB.parent.mkdir(parents=True, exist_ok=True)
    c = sqlite3.connect(DB)
    c.row_factory = sqlite3.Row
    c.executescript((ROOT / "db" / "schema.sql").read_text(encoding="utf-8"))
    return c


def cmd_init(a):
    conn().close()
    print(f"السجل جاهز: {DB}")


def cmd_add_opinions(a):
    ops = json.loads(Path(a.file).read_text(encoding="utf-8"))
    c = conn()
    for o in ops:
        c.execute(
            "INSERT INTO opinions(created_at,symbol,asset_class,source,role,direction,entry,stop,target1,"
            "horizon_days,confidence,method,payload_json) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (o.get("as_of") or now(), o["symbol"], o.get("asset_class", "equity"), o["source"], o["role"],
             o["direction"], o.get("entry"), o.get("stop"), (o.get("targets") or [None])[0],
             o.get("horizon_days"), o.get("confidence"), o.get("method"), json.dumps(o, ensure_ascii=False)))
    c.commit()
    print(f"سُجّل {len(ops)} رأي")


def cmd_add_verification(a):
    r = json.loads(Path(a.file).read_text(encoding="utf-8"))
    lv = r.get("conservative_levels") or {}
    c = conn()
    cur = c.execute(
        "INSERT INTO verifications(created_at,symbol,asset_class,decision,worst_severity,worst_size,"
        "consensus_confidence,c_entry,c_stop,c_target,reward_risk,payload_json) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
        (r["verified_at"], r["symbol"], r.get("asset_class", "equity"), r["decision"], r["worst_severity"],
         r["worst_size"], r["consensus_confidence"], lv.get("entry"), lv.get("stop"), lv.get("target"),
         lv.get("reward_risk"), json.dumps(r, ensure_ascii=False)))
    c.commit()
    print(f"سُجّل حكم التحقق رقم {cur.lastrowid}: {r['decision']}")


def cmd_add_suggestion(a):
    c = conn()
    v = c.execute("SELECT * FROM verifications WHERE id=?", (a.verification,)).fetchone()
    if not v:
        sys.exit("حكم التحقق غير موجود")
    if v["decision"] != "صالح للعرض" and not a.force:
        sys.exit(f"حكم التحقق «{v['decision']}»؛ لا يُسجّل كاقتراح إلا بالخيار --force وبطلب المالك")
    payload = json.loads(v["payload_json"])
    cur = c.execute(
        "INSERT INTO suggestions(created_at,verification_id,symbol,asset_class,direction,entry,stop,target,"
        "confidence,horizon_days,shares,amount_usd,max_loss_usd,notes) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (now(), v["id"], v["symbol"], v["asset_class"], payload["primary"]["direction"], v["c_entry"], v["c_stop"],
         v["c_target"], v["consensus_confidence"], a.horizon, a.shares, a.amount, a.max_loss, a.notes))
    c.commit()
    print(f"سُجّل الاقتراح رقم {cur.lastrowid}")


def cmd_add_execution(a):
    c = conn()
    cur = c.execute(
        "INSERT INTO executions(suggestion_id,symbol,asset_class,side,qty,price,fees,executed_at,notes)"
        " VALUES(?,?,?,?,?,?,?,?,?)",
        (a.suggestion, a.symbol.upper(), a.asset_class, a.side, a.qty, a.price, a.fees, a.at or now(), a.notes))
    c.commit()
    print(f"سُجّل التنفيذ رقم {cur.lastrowid}")


def cmd_resolve(a):
    c = conn()
    c.execute("UPDATE suggestions SET status=?, resolved_at=?, resolved_price=? WHERE id=?",
              (a.status, now(), a.price, a.id))
    c.commit()
    print(f"حُسم الاقتراح {a.id}: {a.status}")


def cmd_evaluate(a):
    """prices.json: {"SYMBOL": [{"date":"YYYY-MM-DD","high":..,"low":..,"close":..}, ...]}

    إذا لمس السعر الوقف والهدف في اليوم نفسه، يُحتسب الوقف (افتراض متحفظ).
    """
    prices = json.loads(Path(a.prices).read_text(encoding="utf-8"))
    c = conn()
    n = 0
    for s in c.execute("SELECT * FROM suggestions WHERE status='open'").fetchall():
        bars = sorted(prices.get(s["symbol"], []), key=lambda b: b["date"])
        start = s["created_at"][:10]
        deadline = (datetime.fromisoformat(s["created_at"]) + timedelta(days=s["horizon_days"] * 1.5)).date().isoformat()
        status = price = when = None
        for b in bars:
            if b["date"] < start:
                continue
            long = s["direction"] == "long"
            hit_stop = b["low"] <= s["stop"] if long else b["high"] >= s["stop"]
            hit_tgt = b["high"] >= s["target"] if long else b["low"] <= s["target"]
            if hit_stop:
                status, price, when = "hit_stop", s["stop"], b["date"]
                break
            if hit_tgt:
                status, price, when = "hit_target", s["target"], b["date"]
                break
            if b["date"] > deadline:
                status, price, when = "expired", b["close"], b["date"]
                break
        if status:
            c.execute("UPDATE suggestions SET status=?, resolved_at=?, resolved_price=? WHERE id=?",
                      (status, when, price, s["id"]))
            n += 1
    c.commit()
    print(f"حُسم {n} اقتراح")


def bucket(conf: float) -> str:
    if conf >= 80:
        return "٨٠ فأعلى"
    if conf >= 70:
        return "٧٠–٧٩"
    if conf >= 60:
        return "٦٠–٦٩"
    return "أقل من ٦٠"


def cmd_stats(a):
    c = conn()
    rows = c.execute("SELECT * FROM suggestions").fetchall()
    print(f"# أداء الاقتراحات ({len(rows)} اقتراح)\n")
    by = defaultdict(lambda: {"n": 0, "win": 0, "loss": 0, "exp": 0, "open": 0, "r": []})
    for s in rows:
        b = by[bucket(s["confidence"])]
        b["n"] += 1
        risk = abs(s["entry"] - s["stop"]) or None
        if s["status"] == "open":
            b["open"] += 1
            continue
        if s["status"] == "hit_target":
            b["win"] += 1
        elif s["status"] == "hit_stop":
            b["loss"] += 1
        elif s["status"] == "expired":
            b["exp"] += 1
        if risk and s["resolved_price"] is not None:
            sign = 1 if s["direction"] == "long" else -1
            b["r"].append(sign * (s["resolved_price"] - s["entry"]) / risk)
    print("| الثقة | العدد | أصاب الهدف | ضرب الوقف | انتهى أفقه | مفتوح | نسبة الإصابة | متوسط العائد بوحدة المخاطرة |")
    print("|---|---|---|---|---|---|---|---|")
    for k in ["٨٠ فأعلى", "٧٠–٧٩", "٦٠–٦٩", "أقل من ٦٠"]:
        if k not in by:
            continue
        b = by[k]
        closed = b["win"] + b["loss"] + b["exp"]
        hit = f"{b['win'] / closed * 100:.0f}٪" if closed else "—"
        avg_r = f"{sum(b['r']) / len(b['r']):.2f}" if b["r"] else "—"
        print(f"| {k} | {b['n']} | {b['win']} | {b['loss']} | {b['exp']} | {b['open']} | {hit} | {avg_r} |")

    ex = c.execute("SELECT * FROM executions ORDER BY executed_at").fetchall()
    if ex:
        pos = defaultdict(lambda: {"qty": 0.0, "cost": 0.0, "realized": 0.0})
        followed = sum(1 for e in ex if e["suggestion_id"])
        for e in ex:
            p = pos[e["symbol"]]
            if e["side"] == "buy":
                p["cost"] += e["qty"] * e["price"] + e["fees"]
                p["qty"] += e["qty"]
            else:
                avg = p["cost"] / p["qty"] if p["qty"] else 0
                p["realized"] += e["qty"] * (e["price"] - avg) - e["fees"]
                p["cost"] -= avg * e["qty"]
                p["qty"] -= e["qty"]
        total = sum(p["realized"] for p in pos.values())
        print(f"\n# تنفيذك الفعلي\n\nعدد العمليات {len(ex)}، منها {followed} مرتبطة باقتراح.")
        print(f"الربح أو الخسارة المحققة: {total:.2f} دولار\n")
        print("| السهم | الكمية المفتوحة | متوسط التكلفة | المحقق |\n|---|---|---|---|")
        for sym, p in pos.items():
            avg = p["cost"] / p["qty"] if p["qty"] else 0
            print(f"| {sym} | {p['qty']:g} | {avg:.2f} | {p['realized']:.2f} |")


def main() -> int:
    _utf8_console()
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sp = ap.add_subparsers(dest="cmd", required=True)
    sp.add_parser("init").set_defaults(f=cmd_init)
    p = sp.add_parser("add-opinions"); p.add_argument("file"); p.set_defaults(f=cmd_add_opinions)
    p = sp.add_parser("add-verification"); p.add_argument("file"); p.set_defaults(f=cmd_add_verification)
    p = sp.add_parser("add-suggestion")
    p.add_argument("--verification", type=int, required=True)
    p.add_argument("--horizon", type=int, required=True)
    p.add_argument("--shares", type=float)
    p.add_argument("--amount", type=float)
    p.add_argument("--max-loss", type=float)
    p.add_argument("--notes")
    p.add_argument("--force", action="store_true")
    p.set_defaults(f=cmd_add_suggestion)
    p = sp.add_parser("add-execution")
    p.add_argument("--suggestion", type=int)
    p.add_argument("--symbol", required=True)
    p.add_argument("--asset-class", default="equity")
    p.add_argument("--side", choices=["buy", "sell"], required=True)
    p.add_argument("--qty", type=float, required=True)
    p.add_argument("--price", type=float, required=True)
    p.add_argument("--fees", type=float, default=0)
    p.add_argument("--at")
    p.add_argument("--notes")
    p.set_defaults(f=cmd_add_execution)
    p = sp.add_parser("resolve")
    p.add_argument("id", type=int)
    p.add_argument("--status", choices=["hit_target", "hit_stop", "expired", "cancelled"], required=True)
    p.add_argument("--price", type=float)
    p.set_defaults(f=cmd_resolve)
    p = sp.add_parser("evaluate"); p.add_argument("--prices", required=True); p.set_defaults(f=cmd_evaluate)
    sp.add_parser("stats").set_defaults(f=cmd_stats)
    a = ap.parse_args()
    a.f(a)
    return 0


if __name__ == "__main__":
    sys.exit(main())
