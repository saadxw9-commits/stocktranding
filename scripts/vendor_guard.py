#!/usr/bin/env python3
"""حارس التحديثات: يفحص التغييرات القادمة من المستودعات الخارجية قبل اعتمادها.

يقارن كل وحدة فرعية بين النسخة المعتمدة (في الفهرس) والنسخة الجديدة، ويوقف الاعتماد إذا:
- تغيّر الترخيص أو حُذف.
- ظهر في الكود نطاق شبكي جديد لم يكن موجودًا.
- ظهرت أنماط خطرة في الملفات المتغيرة: تنفيذ أوامر نظام، فك ترميز وتنفيذ، تحميل وتشغيل سكربت،
  أو نصوص توجّه النموذج لتجاهل التعليمات.
- فشلت اختبارات حزمتي Jake.

الاستخدام: python3 scripts/vendor_guard.py   (بعد git submodule update --remote)
"""
from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TESTED = {"vendor/jake-trader", "vendor/jake-day-trader"}
HOST = re.compile(r"https?://([a-zA-Z0-9.-]+\.[a-z]{2,})")
DANGER = [
    (re.compile(r"curl[^\n|]*\|\s*(ba)?sh"), "تحميل سكربت وتشغيله مباشرة"),
    (re.compile(r"\b(eval|exec)\s*\(\s*(base64|codecs|bytes\.fromhex|requests|urllib)"), "تنفيذ كود مُنزَّل أو مُرمَّز"),
    (re.compile(r"base64\.b64decode\([^)]*\)\s*\)?\s*\.?\s*(decode)?.*\b(exec|eval)\b"), "فك ترميز ثم تنفيذ"),
    (re.compile(r"os\.system\(|subprocess\.(Popen|run|call)\([^)]*shell\s*=\s*True"), "تنفيذ أوامر نظام عبر الصدفة"),
    (re.compile(r"(?i)ignore (all |any )?(previous|prior|above) (instructions|rules)"), "نص يوجّه النموذج لتجاهل التعليمات"),
    (re.compile(r"(?i)(\.ssh/|id_rsa|\.aws/credentials|keychain|ANTHROPIC_API_KEY)"), "إشارة إلى ملفات أو مفاتيح سرية"),
]
TEXT_EXT = {".py", ".md", ".sh", ".js", ".ts", ".json", ".yaml", ".yml", ".toml", ".txt"}


def git(*args, cwd=ROOT) -> str:
    return subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, check=True).stdout


def pinned_and_current():
    """(المسار، الالتزام المعتمد، الالتزام الحالي) لكل وحدة فرعية."""
    out = []
    for line in git("ls-files", "-s", "vendor").splitlines():
        mode, sha, _stage, path = line.split(maxsplit=3)
        if mode != "160000":
            continue
        cur = git("rev-parse", "HEAD", cwd=ROOT / path).strip()
        out.append((path, sha, cur))
    return out


def hosts_at(repo: Path, rev: str) -> set[str]:
    try:
        txt = subprocess.run(["git", "grep", "-hoE", r"https?://[a-zA-Z0-9.-]+\.[a-z]{2,}", rev, "--", "*.py", "*.sh", "*.js", "*.ts"],
                             cwd=repo, capture_output=True, text=True).stdout
    except Exception:
        return set()
    return {m.group(1).lower() for m in HOST.finditer(txt)}


def main() -> int:
    problems: list[str] = []
    changed_any = False
    for path, old, new in pinned_and_current():
        repo = ROOT / path
        if old == new:
            continue
        changed_any = True
        print(f"\n== {path}: {old[:8]} ← {new[:8]}")
        try:
            git("cat-file", "-e", old, cwd=repo)
            have_old = True
        except subprocess.CalledProcessError:
            subprocess.run(["git", "fetch", "--depth", "200", "origin"], cwd=repo, capture_output=True)
            have_old = subprocess.run(["git", "cat-file", "-e", old], cwd=repo).returncode == 0
        files = git("diff", "--name-only", old, new, cwd=repo).splitlines() if have_old else \
            git("ls-files", cwd=repo).splitlines()
        print(f"ملفات متغيرة: {len(files)}")

        lic = repo / "LICENSE"
        if not lic.exists():
            problems.append(f"{path}: ملف الترخيص محذوف")
        elif have_old and "LICENSE" in files:
            problems.append(f"{path}: الترخيص تغيّر، راجعه يدويًا")

        if have_old:
            new_hosts = hosts_at(repo, new) - hosts_at(repo, old)
            if new_hosts:
                problems.append(f"{path}: نطاقات شبكية جديدة في الكود: {', '.join(sorted(new_hosts))}")

        for f in files:
            fp = repo / f
            if not fp.is_file() or fp.suffix.lower() not in TEXT_EXT or "/tests/" in f"/{f}":
                continue
            text = fp.read_text(encoding="utf-8", errors="ignore")
            for rx, label in DANGER:
                if rx.search(text):
                    problems.append(f"{path}/{f}: {label}")

        if path in TESTED:
            r = subprocess.run([sys.executable, "-I", "-m", "unittest", "discover", "-s", "tests", "-q"],
                               cwd=repo, capture_output=True, text=True, timeout=900)
            ok = r.returncode == 0
            print("الاختبارات: نجحت" if ok else "الاختبارات: فشلت")
            if not ok:
                problems.append(f"{path}: فشلت الاختبارات\n{r.stderr[-1500:]}")

    if not changed_any:
        print("لا تحديثات جديدة.")
        return 0
    if problems:
        print("\n### التحديث موقوف:")
        for p in problems:
            print(f"- {p}")
        return 1
    print("\nالتحديث سليم ويمكن اعتماده.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
