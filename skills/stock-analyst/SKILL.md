---
name: stock-analyst
description: محلل السهم. يبني لكل سهم مرشح رأي الأساسي (Jake) ورأي الرديف (tradermonty) وعدسات investorskills ومهارات Anthropic، ثم ثلاثة سيناريوهات (صاعد، أساسي، هابط) لكل منها احتمال ومستوى ونقطة إبطال، ويرسل الآراء لطبقة التحقق. استخدمه عند "حلل سهم"، "وش رأيك في"، "سيناريوهات"، "أهداف سهم".
---

# محلل السهم

## ١. رأي الأساسي (Jake)، من داخل `workspaces/swing/` أو `workspaces/day/`

- الإشارة: مخرجات `trader-scanner`.
- الأخبار: `/day-trader-skill:trader-news`.
- الإفصاحات والأرباح: `/day-trader-skill:trader-analyst`.
- صفقات أعضاء الكونغرس: `/day-trader-skill:trader-pelosi`. هذا الأمر معلومة إضافية فقط، ولا يكون سببًا للدخول.
- كبار المستثمرين: `/trader-skill:trader-copycat`.
- الوقف والهدف: `/day-trader-skill:trader-exit`. هذه الأداة تعمل على الإطار اليومي داخل الجلسة. لصفقات الأيام والأسابيع، احسب الوقف من أدنى قاع مهم على الشمعة اليومية، أو بضعفي متوسط المدى الحقيقي. اذكر الطريقة التي استخدمتها.
- الاحتمالات: `/day-trader-skill:trader-timemachine` للصفقات اليومية. نسخته على الإطار اليومي ضمن خطة التطوير.

حوّل الناتج إلى رأي بالدور `primary` والمصدر `jake`.

## ٢. رأي الرديف (tradermonty)

`technical-analyst` و`breakout-trade-planner` و`us-stock-analysis`، و`earnings-trade-analyzer` قرب موعد الأرباح. الناتج رأي بالدور `secondary` والمصدر `tradermonty`.

## ٣. العدسات (investorskills)

اقرأ ملف `SKILL.md` لكل عدسة في `config/lenses.yaml`، مثل `default_per_analysis.swing` و`always`، من المسار `vendor/investorskills/skills/<الاسم>/`. طبّق منهجها كما هو مكتوب. الناتج رأي بالدور `lens` والمصدر `lens:<الاسم>`، ومستوياته اختيارية.

## ٤. مهارات Anthropic

- في أسبوع الأرباح: `equity-research:earnings-preview` قبل الأرباح، و`equity-research:earnings-analysis` بعدها.
- لصفقات الأسابيع فأكثر: `comps-analysis` و`competitive-analysis`.
- لكل اقتراح يُعتمد: `equity-research:thesis-tracker` لكتابة الأطروحة وشروط إبطالها.

## ٥. السيناريوهات

| السيناريو | الاحتمال | المستوى | الأفق | يبطله |
|---|---|---|---|---|
| صاعد | ٪ | | | |
| أساسي | ٪ | | | |
| هابط | ٪ | | | |

مجموع الاحتمالات ١٠٠. كل رقم من بيانات حقيقية أو محسوب منها. لا تفترض سعرًا.

## ٦. التسليم

احفظ الآراء في `reports/opinions/<السهم>-YYYY-MM-DD.json`، ثم شغّل `verification-layer` ثم `risk-guard`. لا تعرض أي اقتراح قبل هاتين الخطوتين.
