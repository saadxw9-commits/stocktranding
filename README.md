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

## المكونات

- **الأساسي:** حزمتا Jake (`trader-skill` و`day-trader-skill`).
- **الرديف:** tradermonty.
- **العدسات:** investorskills.
- **أبحاث الأسهم:** مستودع Anthropic للخدمات المالية.
- **البيانات:** tradingview-mcp مبدئيًا، ثم خدمة TradingView الرسمية بعد الاشتراك.
- **طبقتنا:** راصد السوق، ماسح الفرص، محلل السهم، طبقة التحقق، حارس المخاطر، السجل.

التفاصيل في `CLAUDE.md`. مقارنة الأدوات المتداخلة في `docs/overlap-map.md`.

## التحديث

المستودعات الخارجية تتبع آخر تحديث من أصحابها. كل سبت يعمل فحص تلقائي في `.github/workflows/vendor-update.yml`، ويعتمد التحديث إذا سلم. وإذا وجد شيئًا مريبًا يوقف التحديث ويفتح بلاغًا. للتحديث يدويًا: `./scripts/update_vendor.sh`.

## الإعدادات التي تخصك

- `config/risk-rules.yaml`: رأس المال وحدود المخاطرة وحد الثقة.
- `config/watchlist.yaml`: قائمة المتابعة.
- `config/data-sources.yaml`: مصادر البيانات.
- `config/lenses.yaml`: عدسات المستثمرين المستخدمة.
- `config/verification.yaml`: عتبات قياس التفاوت.
