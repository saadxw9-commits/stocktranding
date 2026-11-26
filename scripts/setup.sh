#!/usr/bin/env bash
# تجهيز المنظومة على جهازك لأول مرة.
set -euo pipefail
cd "$(dirname "$0")/.."
git submodule update --init --remote --depth 1
python3 -m pip install --user pyyaml >/dev/null
command -v uv >/dev/null || { echo "ثبّت uv أولًا: https://docs.astral.sh/uv/getting-started/installation/"; exit 1; }
uv tool install --python 3.13 tradingview-mcp-server
python3 scripts/link_skills.py
if command -v claude >/dev/null; then
  for m in vendor/jake-trader vendor/jake-day-trader vendor/anthropic-fs; do
    claude plugin validate "./$m" >/dev/null && claude plugin marketplace add "./$m" || true
  done
  claude plugin install trader-skill@trader-skill || true
  claude plugin install day-trader-skill@day-trader-skill || true
  claude plugin install equity-research@claude-for-financial-services || true
fi
python3 skills/journal/journal.py init
[ -n "${SEC_USER_AGENT:-}" ] || echo "تنبيه: عرّف SEC_USER_AGENT باسمك وبريدك لأدوات الإفصاحات، مثال: export SEC_USER_AGENT=\"Saad Ghanem you@example.com\""
[ -n "${FMP_API_KEY:-}" ] || echo "تنبيه: عرّف FMP_API_KEY (مجاني من financialmodelingprep.com) لمعظم مهارات tradermonty"
echo "جاهز. افتح Claude Code داخل المستودع ووافق على الإضافات والخادم عند السؤال."
