---
name: journal
description: سجل المنظومة وتقييمها. يحفظ كل رأي وكل حكم تحقق وكل اقتراح عُرض، وما نفّذه المالك فعلًا بالسعر والكمية، ثم يحسم الاقتراحات ويقيس نسبة الإصابة لكل درجة ثقة والربح والخسارة الفعلية. استخدمه عند "سجّل"، "اشتريت"، "بعت"، "كيف أداء المنظومة"، "تقرير الأداء"، وبعد كل تحقق.
---

# السجل

```bash
python3 skills/journal/journal.py init                         # مرة واحدة
python3 skills/journal/journal.py add-opinions <ملف_الآراء>
python3 skills/journal/journal.py add-verification <ملف_الحكم>
python3 skills/journal/journal.py add-suggestion --verification <رقم> --horizon <أيام> --shares <عدد> --amount <مبلغ> --max-loss <أقصى خسارة>
python3 skills/journal/journal.py add-execution --suggestion <رقم> --symbol <السهم> --side buy|sell --qty <عدد> --price <سعر>
python3 skills/journal/journal.py evaluate --prices <ملف_الأسعار>
python3 skills/journal/journal.py stats
```

## القواعد

1. **لا اقتراح بلا سجل.** يُسجَّل الاقتراح قبل عرضه على المالك.
2. **تنفيذ المالك.** عندما يقول المالك «اشتريت» أو «بعت»، اسأله عن السعر والكمية إن لم يذكرهما، ثم سجّل العملية واربطها برقم الاقتراح إن وُجد. العملية التي نفّذها دون اقتراح تُسجَّل بلا رقم.
3. **الحسم اليومي.** بعد الإغلاق، اجلب القمم والقيعان اليومية لكل اقتراح مفتوح بصيغة `{"السهم": [{"date","high","low","close"}]}`، ثم شغّل `evaluate`. إذا لمس السعر الوقف والهدف في اليوم نفسه يُحتسب الوقف.
4. **التقرير الأسبوعي.** يوم السبت: `stats`، ومعه `weekly-performance-digest` و`signal-postmortem` من tradermonty على الاقتراحات المحسومة، ويُحفظ في `reports/performance/`.
5. **المعيار.** المنظومة تُعد ناضجة لدرجة ثقة معيّنة إذا تحقق شرطان: ٣٠ اقتراحًا محسومًا على الأقل في تلك الدرجة، ومتوسط عائد موجب بوحدة المخاطرة. قبل ذلك، أي نسبة إصابة هي مؤشر أولي فقط.

## الانتقال إلى Supabase

المخطط في `db/schema.sql` يعمل على Postgres كما هو. النقل إلى Supabase خطوة لاحقة بعد استقرار الاستخدام المحلي.
