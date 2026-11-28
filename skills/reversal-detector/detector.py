#!/usr/bin/env python3
"""كاشف الارتداد اللحظي على SPX بأربع طبقات:

1. خريطة المناطق: مستويات ثابتة (قمة وقاع وإغلاق اليوم السابق، الجلسة الليلية، الأرقام المستديرة،
   وحدود الحركة المتوقعة من التذبذب الضمني يوميًا وأسبوعيًا، ومستويات ملف الحجم للجلسة السابقة)
   ومستوى متحرك (قاع أو قمة الجلسة السابق)،
   مع تقدير سعر العقد عند كل مستوى.
2. إشارة الدخول بنمطين:
   - الكسر الكاذب: نطاق ضيق، ثم كسر لطرفه بحجم مرتفع قرب منطقة، ثم إغلاق خلف الطرف الآخر.
   - القاع الأعلى: عودة إلى قاع الجلسة السابق دون كسره، في نطاق ضيق، ثم إغلاق فوق النطاق
     (والعكس للقمة الأدنى).
   الصعود = Call، والهبوط = Put.
3. قواعد الخروج: إلغاء خلف طرف الكسر، هدفان من المناطق، بيع جزئي عند الأول ونقل الإلغاء
   إلى سعر الدخول، ووقف زمني وخروج قبل الإغلاق.
4. السجل: كل منطقة وإشارة وخروج تُكتب في JSON، وتُضاف الصفقات إلى سجل المنظومة عند --journal.

يعمل على أي دقة شموع (ثانية، ١٠ ثوانٍ، دقيقة): المدد في الإعدادات بالثواني، ويُستنتج حجم الشمعة من الطوابع.
الواجهة Detector.feed(bar) تصلح للبث المباشر وإعادة العرض معًا.

    python3 skills/reversal-detector/detector.py <ملف_الشموع.json> [--out تقرير.json] [--journal]
"""
from __future__ import annotations

import argparse
import bisect
from collections import deque
import json
import math
import sys
from datetime import datetime, timedelta
from pathlib import Path
from statistics import median

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

sys.path.insert(0, str(ROOT / "skills" / "option-selector"))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from analyze import option_value, stamp  # noqa: E402

DEFAULTS = {
    "range_seconds": 300,          # مدة النطاق قبل الكسر
    "min_range_bars": 3,
    "max_range_pts": 8.0,          # أقصى اتساع للنطاق حتى يُعد ضيقًا
    "reclaim_seconds": 120,        # مهلة الاستعادة بعد الكسر
    "volume_ratio": 1.2,           # حجم شمعة الكسر ÷ متوسط حجم النطاق (يُتجاهل إن غاب الحجم)
    "zone_tolerance_pts": 4.0,     # قرب طرف الكسر من منطقة
    "zone_required": True,
    "alert_cooldown_seconds": 600,
    "retest_min_age_seconds": 600,  # عمر القاع (أو القمة) السابق قبل أن يُعد اختباره قاعًا أعلى
    "signal_cooldown_seconds": 600,  # لا يتكرر تنبيه المنطقة في الاتجاه نفسه قبل هذه المدة
    "round_step": 25,
    "round_span_pts": 150,
    "stop_buffer_pts": 1.0,
    "min_reward_risk": 2.0,        # العائد المخطط المرجّح بالبيع الجزئي ÷ المخاطرة
    "partial_at_t1": 0.5,
    "fallback_targets_r": [2.0, 3.0],
    "max_hold_seconds": 7200,
    "flat_before_close_seconds": 900,
    "rate": 0.04,
    "dividend_yield": 0.012,
}
YEAR = 365 * 86400


class OptionMap:
    """يترجم مستوى المؤشر إلى سعر عقد تقديري بتذبذب ضمني ثابت (معايَر من سعر مرصود إن وُجد)."""

    def __init__(self, spec: dict | None, cfg: dict):
        self.spec = spec
        self.cfg = cfg
        if not spec:
            return
        self.strike = spec["strike"]
        self.right = spec["right"]
        self.expiry = stamp(spec["expiry"])
        self.iv = spec.get("iv")
        cal = spec.get("calibrate")
        if cal:
            self.iv = self._solve_iv(cal["spot"], cal["price"], stamp(cal["at"]))
        if not self.iv:
            raise ValueError("option needs iv or calibrate")

    def _value(self, spot, at, iv):
        years = max((self.expiry - at).total_seconds(), 0) / YEAR
        return option_value(spot, self.strike, self.right, years, iv,
                            self.cfg["rate"] if years else 0, self.cfg["dividend_yield"] if years else 0)

    def _solve_iv(self, spot, price, at):
        lo, hi = 0.01, 3.0
        for _ in range(80):
            mid = (lo + hi) / 2
            if self._value(spot, at, mid) > price:
                hi = mid
            else:
                lo = mid
        return round((lo + hi) / 2, 5)

    def price(self, spot, at, side=None):
        """side: اتجاه الإشارة. العقد المرتبط Call أو Put واحد، فإشارة الاتجاه المعاكس لا تُسعَّر به."""
        if not self.spec or (side and side != self.right):
            return None
        return round(self._value(spot, at, self.iv), 2)


class Detector:
    def __init__(self, ctx: dict, cfg: dict | None = None):
        self.cfg = {**DEFAULTS, **(cfg or {}), **ctx.get("config", {})}
        self.ctx = ctx
        self.close_at = stamp(ctx["session_close"])
        self.option = OptionMap(ctx.get("option"), self.cfg)
        self.bars: list[dict] = []
        # فهارس تُحدَّث مع كل شمعة لتبقى الكلفة ثابتة تقريبًا على شموع الثانية
        self._ts: list[datetime] = []
        self._lo_idx: list[int] = []   # فهرس أدنى قاع حتى كل موضع (الأول عند التساوي)
        self._hi_idx: list[int] = []   # فهرس أعلى قمة حتى كل موضع
        # إحصاءات نافذة النطاق لكل شمعة (الشموع السابقة خلال range_seconds)، تُحسب مرة واحدة عند وصول الشمعة
        self._wstats: list[tuple] = []     # (عدد، أدنى قاع، أعلى قمة، مجموع الحجم، عدد الأحجام الموجودة)
        self._dq_lo: deque = deque()
        self._dq_hi: deque = deque()
        self._left = 0
        self._vsum = [0.0]
        self._vcnt = [0]
        self.events: list[dict] = []
        self.pending = None
        self.position = None
        self.used_sweeps: set[int] = set()
        self.alerted: set[tuple] = set()
        self.last_alert: dict[str, datetime] = {}
        self.last_signal: dict[tuple, datetime] = {}
        self.static = self._static_levels()

    # ---------- الطبقة ١: المناطق ----------
    def _static_levels(self):
        names = {"prev_high": "قمة اليوم السابق", "prev_low": "قاع اليوم السابق",
                 "prev_close": "إغلاق اليوم السابق", "overnight_high": "قمة الجلسة الليلية",
                 "overnight_low": "قاع الجلسة الليلية"}
        levels = [(float(self.ctx["levels"][k]), v) for k, v in names.items()
                  if self.ctx.get("levels", {}).get(k) is not None]
        levels += self._expected_move_levels()
        levels += self._volume_profile_levels()
        ref = self.ctx.get("reference_price")
        if ref:
            step, span = self.cfg["round_step"], self.cfg["round_span_pts"]
            base = math.floor((ref - span) / step) * step
            levels += [(float(x), "رقم مستدير") for x in range(int(base), int(ref + span) + 1, int(step))]
        return levels

    def _expected_move_levels(self):
        """حدود الحركة المتوقعة: المرجع ± مضاعف × الانحراف المتوقع.
        من التذبذب الضمني السنوي: الانحراف = المرجع × التذبذب × جذر(الأيام ÷ 252).
        أو من سعر عقد Straddle عند المال لليوم: الانحراف اليومي ≈ سعره، والأسبوعي ≈ سعره × جذر 5."""
        em = self.ctx.get("expected_move")
        if not em:
            return []
        ref = float(em["reference"])
        out = []
        for label, days in (("اليومية", 1), ("الأسبوعية", 5)):
            if label == "الأسبوعية" and not em.get("weekly", True):
                continue
            if em.get("iv"):
                sd = ref * float(em["iv"]) * math.sqrt(days / 252)
            elif em.get("daily_straddle_pts"):
                sd = float(em["daily_straddle_pts"]) * math.sqrt(days)
            else:
                raise ValueError("expected_move needs iv or daily_straddle_pts")
            for m in em.get("multiples", [1.0]):
                out.append((round(ref + m * sd, 2), f"الحد العلوي للحركة المتوقعة {label} ({m:g}σ)"))
                out.append((round(ref - m * sd, 2), f"الحد السفلي للحركة المتوقعة {label} ({m:g}σ)"))
        return out

    def _volume_profile_levels(self):
        """مستويات ملف الحجم للجلسة السابقة: نقطة التحكم وحدّا منطقة القيمة وعقد الحجم المرتفع،
        ونقاط التحكم غير المختبرة. تُعطى جاهزة في levels أو تُبنى من prior_bars."""
        vp = self.ctx.get("volume_profile")
        if not vp:
            return []
        from volume_profile import build, levels_for_detector
        prior = vp.get("levels")
        if prior is None:
            prior = build(vp["prior_bars"], vp["bin_size"], vp.get("value_area_pct", 0.70),
                          vp.get("price_scale", 1.0))
        prior = dict(prior, hvn=prior.get("hvn", []))
        return [(float(lv), name) for lv, name in
                levels_for_detector(prior, vp.get("naked_pocs"), vp.get("include_hvn", True))]

    def _session_extreme(self, before: datetime, side: str):
        n = bisect.bisect_left(self._ts, before)
        if n < self.cfg["min_range_bars"]:
            return None
        return (self.bars[self._lo_idx[n - 1]]["l"], "قاع الجلسة السابق") if side == "call" else \
               (self.bars[self._hi_idx[n - 1]]["h"], "قمة الجلسة السابقة")

    def _near_zone(self, price, before, side):
        tol = self.cfg["zone_tolerance_pts"]
        cands = list(self.static)
        ext = self._session_extreme(before, side)
        if ext:
            cands.append(ext)
        hits = [(abs(price - lv), lv, name) for lv, name in cands if abs(price - lv) <= tol]
        return min(hits)[1:] if hits else None

    def _zone_alerts(self, bar):
        start = bar["t"] - timedelta(seconds=self.cfg["range_seconds"])
        for side, px in (("call", bar["l"]), ("put", bar["h"])):
            z = self._near_zone(px, start, side)
            if not z:
                continue
            key = (side, round(z[0], 0))
            last = self.last_alert.get(side)
            cooling = last and (bar["t"] - last).total_seconds() < self.cfg["alert_cooldown_seconds"]
            if key in self.alerted or cooling:
                continue
            self.alerted.add(key)
            self.last_alert[side] = bar["t"]
            self._emit("zone", bar["t"], side=side, level=z[0], zone=z[1],
                       option_est=self.option.price(z[0], bar["t"], side))

    # ---------- الطبقة ٢: الإشارة ----------
    def _window(self, i):
        t = self.bars[i]["t"]
        start = t - timedelta(seconds=self.cfg["range_seconds"])
        return self.bars[bisect.bisect_left(self._ts, start, 0, i):i], start

    def _check_signal(self):
        j = len(self.bars) - 1
        now = self.bars[j]["t"]
        cfg = self.cfg
        for i in range(j, -1, -1):
            if (now - self.bars[i]["t"]).total_seconds() > cfg["reclaim_seconds"]:
                break
            if i in self.used_sweeps:
                continue
            n_w, r_lo, r_hi, v_sum, v_cnt = self._wstats[i]
            if n_w < cfg["min_range_bars"]:
                continue
            if r_hi - r_lo > cfg["max_range_pts"]:
                continue
            start = self.bars[i]["t"] - timedelta(seconds=cfg["range_seconds"])
            sweep = self.bars[i]
            vol_ok = True
            if sweep.get("v") is not None and v_cnt == n_w:
                avg = v_sum / n_w
                vol_ok = avg == 0 or sweep["v"] >= cfg["volume_ratio"] * avg
            seg = self.bars[i:j + 1]
            for side, swept, reclaimed, extreme in (
                ("call", sweep["l"] < r_lo, self.bars[j]["c"] > r_hi, min(b["l"] for b in seg)),
                ("put", sweep["h"] > r_hi, self.bars[j]["c"] < r_lo, max(b["h"] for b in seg)),
            ):
                if not (swept and reclaimed):
                    continue
                zone = self._near_zone(extreme, start, side)
                if not vol_ok or (cfg["zone_required"] and not zone):
                    self.used_sweeps.add(i)
                    self._emit("rejected_signal", now, side=side, sweep_at=sweep["t"],
                               reason="volume" if not vol_ok else "no_zone")
                    continue
                self.used_sweeps.add(i)
                self.pending = dict(side=side, pattern="failed_breakdown" if side == "call" else "failed_breakout",
                                    sweep_at=sweep["t"], signal_at=now, extreme=extreme,
                                    range_low=r_lo, range_high=r_hi,
                                    zone=zone[1] if zone else None, zone_level=zone[0] if zone else None,
                                    sweep_volume=sweep.get("v"))
                self._emit("signal", now, **{k: v for k, v in self.pending.items() if k != "signal_at"})
                return
        self._check_retest()

    def _check_retest(self):
        """قاع أعلى (أو قمة أدنى): اختبار طرف الجلسة السابق دون كسره، ثم إغلاق خارج النطاق الضيق."""
        cfg = self.cfg
        bar = self.bars[-1]
        now = bar["t"]
        n = len(self.bars) - 1
        w0 = bisect.bisect_left(self._ts, now - timedelta(seconds=cfg["range_seconds"]), 0, n)
        win = self.bars[w0:n]
        if len(win) < cfg["min_range_bars"]:
            return
        k = bisect.bisect_left(self._ts, win[0]["t"])
        if k < cfg["min_range_bars"]:
            return
        w_lo, w_hi = min(b["l"] for b in win), max(b["h"] for b in win)
        if w_hi - w_lo > cfg["max_range_pts"]:
            return
        tol, age = cfg["zone_tolerance_pts"], cfg["retest_min_age_seconds"]
        lo_bar, hi_bar = self.bars[self._lo_idx[k - 1]], self.bars[self._hi_idx[k - 1]]
        cases = (
            ("call", "higher_low", "قاع الجلسة السابق", lo_bar["l"], w_lo,
             lo_bar["l"] < w_lo <= lo_bar["l"] + tol and bar["l"] > lo_bar["l"]
             and (now - lo_bar["t"]).total_seconds() >= age and bar["c"] > w_hi),
            ("put", "lower_high", "قمة الجلسة السابقة", hi_bar["h"], w_hi,
             hi_bar["h"] > w_hi >= hi_bar["h"] - tol and bar["h"] < hi_bar["h"]
             and (now - hi_bar["t"]).total_seconds() >= age and bar["c"] < w_lo),
        )
        for side, pattern, zone, level, extreme, ok in cases:
            if not ok:
                continue
            last = self.last_signal.get((pattern, side))
            if last and (now - last).total_seconds() < cfg["signal_cooldown_seconds"]:
                continue
            self.last_signal[(pattern, side)] = now
            self.pending = dict(side=side, pattern=pattern, sweep_at=None, signal_at=now, extreme=extreme,
                                range_low=w_lo, range_high=w_hi, zone=zone, zone_level=level, sweep_volume=None)
            self._emit("signal", now, **{k: v for k, v in self.pending.items() if k != "signal_at"})
            return

    # ---------- الطبقة ٣: الدخول والخروج ----------
    def _targets(self, side, entry, risk):
        sign = 1 if side == "call" else -1
        levels = sorted({lv for lv, _ in self.static if sign * (lv - entry) >= risk}, key=lambda x: sign * x)
        names = {lv: n for lv, n in self.static}
        picked = [(lv, names[lv]) for lv in levels[:2]]
        for r in self.cfg["fallback_targets_r"][len(picked):]:
            picked.append((round(entry + sign * r * risk, 2), f"{r:g} ضعف المخاطرة"))
        return picked[:2]

    def _open(self, bar):
        p, self.pending = self.pending, None
        side, sign = p["side"], (1 if p["side"] == "call" else -1)
        entry = bar["o"]
        stop = round(p["extreme"] - sign * self.cfg["stop_buffer_pts"], 2)
        risk = sign * (entry - stop)
        if risk <= 0:
            self._emit("skip", bar["t"], side=side, reason="entry_beyond_stop")
            return
        (t1, n1), (t2, n2) = self._targets(side, entry, risk)
        part = self.cfg["partial_at_t1"]
        planned = (part * sign * (t1 - entry) + (1 - part) * sign * (t2 - entry)) / risk
        deadline = min(bar["t"] + timedelta(seconds=self.cfg["max_hold_seconds"]),
                       self.close_at - timedelta(seconds=self.cfg["flat_before_close_seconds"]))
        info = dict(side=side, entry=entry, stop=stop, risk_pts=round(risk, 2),
                    t1=t1, t1_zone=n1, t2=t2, t2_zone=n2, planned_r=round(planned, 2),
                    deadline=deadline, zone=p["zone"], pattern=p["pattern"],
                    option_entry_est=self.option.price(entry, bar["t"], side),
                    option_stop_est=self.option.price(stop, bar["t"], side))
        if planned < self.cfg["min_reward_risk"]:
            self._emit("skip", bar["t"], reason="reward_risk", **info)
            return
        self.position = dict(info, opened_at=bar["t"], remaining=1.0, fills=[], signal=p)
        self._emit("entry", bar["t"], **info)

    def _fill(self, t, price, qty, why):
        pos = self.position
        est = self.option.price(price, t, pos["side"])
        pos["fills"].append(dict(at=t, price=price, qty=qty, reason=why, option_est=est))
        pos["remaining"] = round(pos["remaining"] - qty, 6)
        self._emit("exit", t, price=price, qty=qty, reason=why, option_est=est)
        if pos["remaining"] <= 1e-9:
            self._close()

    def _manage(self, bar):
        pos = self.position
        sign = 1 if pos["side"] == "call" else -1
        hit_stop = (bar["l"] <= pos["stop"]) if sign > 0 else (bar["h"] >= pos["stop"])
        if hit_stop:  # افتراض متحفظ: الإلغاء يسبق الهدف داخل الشمعة نفسها
            self._fill(bar["t"], pos["stop"], pos["remaining"],
                       "breakeven" if pos.get("t1_done") else "stop")
            return
        reach = (lambda lv: bar["h"] >= lv) if sign > 0 else (lambda lv: bar["l"] <= lv)
        if not pos.get("t1_done") and reach(pos["t1"]):
            pos["t1_done"] = True
            self._fill(bar["t"], pos["t1"], self.cfg["partial_at_t1"], "target1")
            if not self.position:
                return
            pos["stop"] = pos["entry"]
        if reach(pos["t2"]):
            self._fill(bar["t"], pos["t2"], pos["remaining"], "target2")
            return
        if bar["t"] >= pos["deadline"]:
            self._fill(bar["t"], bar["c"], pos["remaining"], "time")

    def _close(self):
        pos, self.position = self.position, None
        sign = 1 if pos["side"] == "call" else -1
        pts = sum(f["qty"] * sign * (f["price"] - pos["entry"]) for f in pos["fills"])
        opt = None
        if pos["option_entry_est"] is not None:
            opt = round(sum(f["qty"] * (f["option_est"] - pos["option_entry_est"]) for f in pos["fills"]) * 100, 2)
        self._emit("trade_closed", pos["fills"][-1]["at"], side=pos["side"], entry=pos["entry"],
                   result_pts=round(pts, 2), result_r=round(pts / pos["risk_pts"], 2),
                   option_pnl_usd_est=opt, fills=pos["fills"], planned_r=pos["planned_r"],
                   zone=pos["zone"], pattern=pos["pattern"], opened_at=pos["opened_at"])

    # ---------- الواجهة ----------
    def _emit(self, kind, t, **data):
        self.events.append(dict(type=kind, at=t, **data))

    def feed(self, bar: dict) -> list[dict]:
        bar = dict(bar, t=stamp(bar["t"]) if isinstance(bar["t"], str) else bar["t"])
        start = len(self.events)
        if self.bars and bar["t"] <= self.bars[-1]["t"]:
            raise ValueError("bars must be in time order")
        i = len(self.bars)
        start_w = bar["t"] - timedelta(seconds=self.cfg["range_seconds"])
        while self._dq_lo and self._ts[self._dq_lo[0]] < start_w:
            self._dq_lo.popleft()
        while self._dq_hi and self._ts[self._dq_hi[0]] < start_w:
            self._dq_hi.popleft()
        while self._left < i and self._ts[self._left] < start_w:
            self._left += 1
        self._wstats.append((i - self._left,
                             self.bars[self._dq_lo[0]]["l"] if self._dq_lo else None,
                             self.bars[self._dq_hi[0]]["h"] if self._dq_hi else None,
                             self._vsum[i] - self._vsum[self._left], self._vcnt[i] - self._vcnt[self._left]))
        self.bars.append(bar)
        while self._dq_lo and self.bars[self._dq_lo[-1]]["l"] >= bar["l"]:
            self._dq_lo.pop()
        self._dq_lo.append(i)
        while self._dq_hi and self.bars[self._dq_hi[-1]]["h"] <= bar["h"]:
            self._dq_hi.pop()
        self._dq_hi.append(i)
        v = bar.get("v")
        self._vsum.append(self._vsum[-1] + (v or 0.0))
        self._vcnt.append(self._vcnt[-1] + (v is not None))
        self._ts.append(bar["t"])
        self._lo_idx.append(i if i == 0 or bar["l"] < self.bars[self._lo_idx[-1]]["l"] else self._lo_idx[-1])
        self._hi_idx.append(i if i == 0 or bar["h"] > self.bars[self._hi_idx[-1]]["h"] else self._hi_idx[-1])
        if self.pending:
            self._open(bar)
        if self.position:
            self._manage(bar)
        elif not self.pending:
            self._zone_alerts(bar)
            self._check_signal()
        return self.events[start:]

    def finish(self):
        if self.position:
            last = self.bars[-1]
            self._fill(last["t"], last["c"], self.position["remaining"], "end_of_data")
        return self.events


def bar_seconds(bars):
    ts = [stamp(b["t"]) for b in bars[:50]]
    diffs = [(b - a).total_seconds() for a, b in zip(ts, ts[1:])]
    return median(diffs) if diffs else None


def run(data: dict, cfg: dict | None = None) -> dict:
    det = Detector(data, cfg)
    for b in data["bars"]:
        det.feed(b)
    events = det.finish()
    trades = [e for e in events if e["type"] == "trade_closed"]
    return dict(source=data.get("source"), symbol=data.get("symbol", "SPX"),
                asset_class=data.get("asset_class", "index"),
                bar_seconds=bar_seconds(data["bars"]), config=det.cfg,
                option_iv=det.option.iv if det.option.spec else None,
                events=events, summary=dict(
                    signals=sum(e["type"] == "signal" for e in events),
                    rejected=sum(e["type"] == "rejected_signal" for e in events),
                    trades=len(trades), total_r=round(sum(t["result_r"] for t in trades), 2)))


LABELS = {"zone": "منطقة", "signal": "إشارة", "rejected_signal": "إشارة مرفوضة", "entry": "دخول",
          "skip": "تخطٍّ", "exit": "خروج", "trade_closed": "إغلاق الصفقة"}
SIDES = {"call": "صعود (شراء Call)", "put": "هبوط (شراء Put)"}


def describe(e) -> str:
    t = e["at"].strftime("%H:%M:%S")
    k = e["type"]
    if k == "zone":
        opt = f" — العقد تقديريًا {e['option_est']}$" if e.get("option_est") is not None else ""
        return f"{t} منطقة: {e['zone']} عند {e['level']:.2f} (احتمال {SIDES[e['side']]}){opt}"
    if k == "signal":
        if e.get("sweep_at") is None:
            word = "قاع أعلى" if e["side"] == "call" else "قمة أدنى"
            return (f"{t} إشارة {SIDES[e['side']]}: {word} عند {e['extreme']:.2f} فوق {e['zone']} "
                    f"{e['zone_level']:.2f}، ثم إغلاق خارج النطاق {e['range_low']:.2f}–{e['range_high']:.2f}")
        return (f"{t} إشارة {SIDES[e['side']]}: كسر النطاق {e['range_low']:.2f}–{e['range_high']:.2f} "
                f"إلى {e['extreme']:.2f} ثم استعادة، قرب {e['zone'] or 'بلا منطقة'}")
    if k == "rejected_signal":
        return f"{t} إشارة مرفوضة ({e['reason']})"
    if k in ("entry", "skip"):
        opt = (f" | العقد تقديريًا {e['option_entry_est']}$ وإلغاؤه {e['option_stop_est']}$"
               if e.get("option_entry_est") is not None else "")
        head = "دخول" if k == "entry" else f"تخطٍّ ({e.get('reason')})"
        if "entry" not in e:
            return f"{t} {head}"
        return (f"{t} {head}: {e['entry']:.2f} | إلغاء {e['stop']:.2f} | هدف١ {e['t1']:.2f} ({e['t1_zone']}) "
                f"| هدف٢ {e['t2']:.2f} ({e['t2_zone']}) | العائد المخطط {e['planned_r']} ضعف{opt}")
    if k == "exit":
        opt = f" — العقد تقديريًا {e['option_est']}$" if e.get("option_est") is not None else ""
        return f"{t} خروج {int(e['qty'] * 100)}% عند {e['price']:.2f} ({e['reason']}){opt}"
    if k == "trade_closed":
        opt = f" | ربح العقد تقديريًا {e['option_pnl_usd_est']}$" if e.get("option_pnl_usd_est") is not None else ""
        return f"{t} النتيجة: {e['result_pts']:+.2f} نقطة = {e['result_r']:+.2f} ضعف المخاطرة{opt}"
    return f"{t} {k}"


def to_journal(report: dict, mode: str, contract_mode: str = "standard"):
    sys.path.insert(0, str(ROOT / "skills" / "journal"))
    from journal import conn
    c = conn()
    cols = {r[1] for r in c.execute("PRAGMA table_info(intraday_signals)")}
    for col, ddl in (("pattern", "TEXT"), ("contract_mode", "TEXT NOT NULL DEFAULT 'standard'")):
        if col not in cols:
            c.execute(f"ALTER TABLE intraday_signals ADD COLUMN {col} {ddl}")
    n = 0
    for e in report["events"]:
        if e["type"] != "trade_closed":
            continue
        c.execute("INSERT INTO intraday_signals(created_at,symbol,asset_class,side,zone,entry,result_pts,result_r,"
                  "option_pnl_usd_est,closed_at,mode,pattern,contract_mode,payload_json) "
                  "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                  (e["opened_at"].isoformat(), report["symbol"], report.get("asset_class", "index"), e["side"],
                   e["zone"], e["entry"], e["result_pts"], e["result_r"], e["option_pnl_usd_est"],
                   e["at"].isoformat(), mode, e.get("pattern"), contract_mode,
                   json.dumps(e, ensure_ascii=False, default=str)))
        n += 1
    c.commit()
    c.close()
    return n


def main():
    _utf8_console()
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("bars", type=Path)
    ap.add_argument("--out", type=Path)
    ap.add_argument("--journal", action="store_true", help="أضف الصفقات المحاكاة إلى سجل المنظومة")
    ap.add_argument("--mode", default="replay", choices=["replay", "paper", "live"])
    ap.add_argument("--contract-mode", default="standard", choices=["standard", "cheap"],
                    help="نوع العقود المرتبطة بالإشارات، لتُقاس كل فئة في السجل منفصلة")
    a = ap.parse_args()
    report = run(json.loads(a.bars.read_text(encoding="utf-8")))
    report["mode"] = a.mode
    for e in report["events"]:
        print(describe(e))
    s = report["summary"]
    print(f"\nالملخص: إشارات {s['signals']}، مرفوضة {s['rejected']}، صفقات {s['trades']}، "
          f"المجموع {s['total_r']:+} ضعف المخاطرة. دقة الشمعة {report['bar_seconds']} ثانية.")
    if a.out:
        a.out.parent.mkdir(parents=True, exist_ok=True)
        a.out.write_text(json.dumps(report, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    if a.journal:
        print(f"أُضيفت {to_journal(report, a.mode, a.contract_mode)} صفقة إلى السجل")


if __name__ == "__main__":
    main()
