# منظومة تحليل الأسهم الأمريكية

منظومة تعمل على Claude Code. تتابع السوق، وتكتشف الفرص، وتقدّر مستويات الدخول والخروج من مصادر متعددة، ثم تتحقق من اتفاق هذه المصادر قبل أن تعرض أي اقتراح. وتسجل كل توقع ونتيجته لتقيس جودة عملها مع الوقت.

**القرار والتنفيذ لك.** لا تنفذ المنظومة أي أمر شراء أو بيع.

## التشغيل لأول مرة

```bash
git clone --recurse-submodules https://github.com/saadxw9-commits/stocktranding.git
cd stocktranding
export SEC_USER_AGENT="اسمك بريدك"
export FMP_API_KEY="مفتاحك المجاني"
./scripts/setup.sh
claude
```

**على ويندوز (PowerShell):**

```powershell
git clone --recurse-submodules https://github.com/saadxw9-commits/stocktranding.git
cd stocktranding
$env:SEC_USER_AGENT = "اسمك بريدك"
$env:FMP_API_KEY = "مفتاحك المجاني"
.\scripts\setup.ps1
```

مجلد `.claude/skills` مولَّد ولا يُرفع إلى git. على ويندوز يُنشأ بنسخ حقيقية، فأعد تشغيل `python scripts/link_skills.py` بعد كل تحديث للمستودعات الخارجية. إذا نقص أي مستودع خارجي عندك: `git submodule update --init`.

## المكونات

- **الأساسي:** حزمتا Jake (`trader-skill` و`day-trader-skill`).
- **الرديف:** tradermonty.
- **العدسات:** investorskills.
- **أبحاث الأسهم:** مستودع Anthropic للخدمات المالية.
- **البيانات:** tradingview-mcp مبدئيًا، ثم خدمة TradingView الرسمية بعد الاشتراك.
- **طبقتنا:** راصد السوق، ماسح الفرص، محلل السهم، طبقة التحقق، حارس المخاطر، السجل.
- **عقود الخيارات:** اختيار العقد، جسر الاقتراح إلى العقد، كاشف الارتداد اللحظي، ملخص ما قبل الافتتاح، سجل الظل.

التفاصيل في `CLAUDE.md`. مقارنة الأدوات المتداخلة في `docs/overlap-map.md`.

## التحديث

المستودعات الخارجية تتبع آخر تحديث من أصحابها. كل سبت يعمل فحص تلقائي في `.github/workflows/vendor-update.yml`، ويعتمد التحديث إذا سلم. وإذا وجد شيئًا مريبًا يوقف التحديث ويفتح بلاغًا. للتحديث يدويًا: `./scripts/update_vendor.sh`.

## الإعدادات التي تخصك

- `config/risk-rules.yaml`: رأس المال وحدود المخاطرة وحد الثقة.
- `config/watchlist.yaml`: قائمة المتابعة.
- `config/data-sources.yaml`: مصادر البيانات.
- `config/lenses.yaml`: عدسات المستثمرين المستخدمة.
- `config/verification.yaml`: عتبات قياس التفاوت.
- `config/options-universe.yaml`: أصول عقود الخيارات المعتمدة وحدود السيولة والحساسية والفارق ونمط العقود الرخيصة.
- `config/country-etfs.yaml`: صناديق الدول وأسواقها وعملاتها لملخص ما قبل الافتتاح.

## مقارنة عقود الخيارات

اطلب «قارن عقود SPXW أو QQQ أو TSLA لهدفي ووقت خروجي». الأصول المعتمدة وقواعدها في `config/options-universe.yaml`. [مهارة اختيار العقد](skills/option-selector/SKILL.md) تقرأ لقطة سلسلة من الوسيط وتحسب تكلفة عقد واحد والتعادل والنتيجة عند الهدف والثبات والإلغاء. لتجربة المثال الورقي من جذر المستودع:

```bash
python skills/option-selector/analyze.py skills/option-selector/example.json --replay
```

## كاشف الارتداد اللحظي

[المهارة](skills/reversal-detector/SKILL.md) تقرأ شموع SPX بأي دقة (ثانية أو دقيقة) وتطلق تنبيه المنطقة ثم إشارة الدخول وقواعد الخروج. لإعادة عرض يوم 27 نوفمبر 2026 (بيانات تقريبية من لقطات):

```bash
python skills/reversal-detector/detector.py skills/reversal-detector/fixtures/spx-2026-11-27-approx.json
```

## ملخص ما قبل الافتتاح

[المهارة](skills/premarket-gap-brief/SKILL.md) تقدّر فجوة افتتاح صناديق الدول (EWZ وFXI وEWJ وغيرها) من حركة أسواقها وعملاتها أثناء إغلاق السوق الأمريكية:

```bash
python skills/premarket-gap-brief/gap_brief.py skills/premarket-gap-brief/fixtures/2026-11-27-approx.json
```

## من الاقتراح إلى العقد

[الجسر](skills/option-bridge/SKILL.md) يأخذ حكم طبقة التحقق ويختار عقد الخيارات عبر عدة تواريخ انتهاء (مثال اصطناعي):

```bash
python skills/option-bridge/bridge.py plan skills/option-bridge/fixtures/verify-TSLA-synthetic.json --out plan.json
python skills/option-bridge/bridge.py select plan.json skills/option-bridge/fixtures/chain-TSLA-synthetic.json --cash 5000 --budget 3000 --replay
```
