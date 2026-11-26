# تجهيز المنظومة على ويندوز (PowerShell). شغّله من جذر المستودع:  .\scripts\setup.ps1
$ErrorActionPreference = "Stop"
Set-Location (Split-Path -Parent $PSScriptRoot)

Write-Host "1) تجهيز الوحدات الفرعية"
git submodule update --init --depth 1

Write-Host "2) تثبيت المتطلبات"
python -m pip install --user pyyaml | Out-Null
if (-not (Get-Command uv -ErrorAction SilentlyContinue)) {
    Write-Warning "ثبّت uv أولًا: https://docs.astral.sh/uv/getting-started/installation/ ثم أعد التشغيل"
    exit 1
}
uv tool install --python 3.13 tradingview-mcp-server

Write-Host "3) تجهيز المهارات (نسخ حقيقية على ويندوز)"
python scripts/link_skills.py

Write-Host "4) السجل"
python skills/journal/journal.py init

if (Get-Command claude -ErrorAction SilentlyContinue) {
    Write-Host "5) إضافات Claude Code"
    foreach ($m in "vendor/jake-trader", "vendor/jake-day-trader", "vendor/anthropic-fs") {
        claude plugin marketplace add "./$m"
    }
    claude plugin install trader-skill@trader-skill
    claude plugin install day-trader-skill@day-trader-skill
    claude plugin install equity-research@claude-for-financial-services
} else {
    Write-Warning "أمر claude غير موجود في هذه الصدفة؛ نفّذ خطوة الإضافات من طرفية فيها Claude Code."
}

if (-not $env:SEC_USER_AGENT) { Write-Warning '$env:SEC_USER_AGENT = "اسمك بريدك"' }
if (-not $env:FMP_API_KEY)    { Write-Warning '$env:FMP_API_KEY = "مفتاحك"' }
Write-Host "جاهز. افتح Claude Code داخل المستودع."
