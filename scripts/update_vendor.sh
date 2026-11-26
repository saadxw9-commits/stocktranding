#!/usr/bin/env bash
# يجلب آخر تحديثات المستودعات الخارجية، ويفحصها، ثم يعتمدها إذا سلمت.
set -euo pipefail
cd "$(dirname "$0")/.."
git submodule update --init --remote --depth 1 vendor/*
if python3 scripts/vendor_guard.py; then
  python3 scripts/link_skills.py
  if ! git diff --quiet -- vendor; then
    git add vendor
    git commit -m "تحديث المستودعات الخارجية بعد الفحص"
    echo "اعتُمد التحديث. ارفعه بـ: git push"
  fi
else
  echo "أُوقف التحديث. أُعيدت الوحدات الفرعية إلى النسخ المعتمدة."
  git submodule update --init --depth 1 vendor/*
  exit 1
fi
