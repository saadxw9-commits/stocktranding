"""تحويل سعر العقد الآجل (ES) إلى سعر SPX المتوقع قبل الافتتاح بالقيمة العادلة.

القيمة العادلة = المؤشر × ((١ + (الفائدة − التوزيعات) × الأيام المتبقية ÷ ٣٦٠) − ١)
تقريب خطي لتكلفة الحمل. الرقم المنشور رسميًا صباح كل يوم أدق، ويُمرَّر بـ --published عند توفره.
"""
import argparse
import json
import sys
from datetime import date


def days_to_expiry(as_of: date, expiry: date) -> int:
    days = (expiry - as_of).days
    if days < 0:
        raise ValueError("futures expiry is before as_of")
    return days


def fair_value_points(index_close, rate, dividend_yield, days, basis_days=360):
    """فرق السعر العادل بين العقد الآجل والمؤشر بالنقاط."""
    if index_close <= 0:
        raise ValueError("index_close must be positive")
    return index_close * (rate - dividend_yield) * days / basis_days


def implied_index(futures_price, fair_value):
    """سعر المؤشر الذي يعكسه العقد الآجل الآن."""
    return futures_price - fair_value


def analyze(futures_price, index_close, as_of, expiry, rate, dividend_yield,
            published_fair_value=None, futures_at_index_close=None):
    days = days_to_expiry(as_of, expiry)
    model_fv = fair_value_points(index_close, rate, dividend_yield, days)
    fv = published_fair_value if published_fair_value is not None else model_fv
    implied = implied_index(futures_price, fv)
    out = dict(
        futures_price=futures_price, index_close=index_close, days_to_expiry=days,
        fair_value_model_pts=round(model_fv, 2),
        fair_value_used_pts=round(fv, 2),
        fair_value_source="published" if published_fair_value is not None else "model",
        implied_index=round(implied, 2),
        implied_change_pts=round(implied - index_close, 2),
        implied_change_pct=round((implied / index_close - 1) * 100, 3),
    )
    if futures_at_index_close is not None:
        # الفرق الفعلي وقت إغلاق المؤشر. زيادته على القيمة العادلة غالبًا من فارق وقت الإغلاق.
        observed = futures_at_index_close - index_close
        out["observed_basis_pts"] = round(observed, 2)
        out["basis_residual_pts"] = round(observed - model_fv, 2)
    return out


def main():
    for s in (sys.stdout, sys.stderr):
        if hasattr(s, "reconfigure"):
            s.reconfigure(encoding="utf-8")
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--futures", type=float, required=True, help="سعر العقد الآجل الآن")
    p.add_argument("--index-close", type=float, required=True, help="آخر إغلاق للمؤشر النقدي")
    p.add_argument("--as-of", type=date.fromisoformat, required=True)
    p.add_argument("--expiry", type=date.fromisoformat, required=True, help="تاريخ انتهاء العقد الآجل")
    p.add_argument("--rate", type=float, default=0.04, help="الفائدة السنوية (فرضية إن لم تُحدَّث)")
    p.add_argument("--dividend-yield", type=float, default=0.012, help="عائد التوزيعات السنوي (فرضية)")
    p.add_argument("--published", type=float, default=None, help="القيمة العادلة المنشورة صباح اليوم")
    p.add_argument("--futures-at-index-close", type=float, default=None,
                   help="سعر العقد الآجل عند إغلاق المؤشر، لقياس الفرق الفعلي")
    a = p.parse_args()
    try:
        result = analyze(a.futures, a.index_close, a.as_of, a.expiry, a.rate, a.dividend_yield,
                         a.published, a.futures_at_index_close)
    except ValueError as exc:
        result = dict(error=str(exc))
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
