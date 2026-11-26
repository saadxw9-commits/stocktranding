#!/usr/bin/env python3
"""يربط المهارات المسجّلة في config/skills-registry.yaml داخل .claude/skills/ بروابط رمزية.

الروابط تشير إلى vendor/ و skills/، فأي تحديث في المستودع الأصلي يظهر مباشرة.
"""
import os
import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
TARGET = ROOT / ".claude" / "skills"
SKIP_GROUPS = {"crypto_reserved"}


def main() -> int:
    reg = yaml.safe_load((ROOT / "config" / "skills-registry.yaml").read_text(encoding="utf-8"))
    TARGET.mkdir(parents=True, exist_ok=True)
    wanted: dict[str, Path] = {}
    for group, names in reg.get("tradermonty", {}).items():
        if group in SKIP_GROUPS:
            continue
        for n in names:
            wanted[n] = ROOT / "vendor" / "tradermonty" / "skills" / n
    for plugin, names in reg.get("anthropic_fs", {}).items():
        for n in names:
            wanted[n] = ROOT / "vendor" / "anthropic-fs" / "plugins" / "vertical-plugins" / plugin / "skills" / n
    for n in reg.get("ours", []):
        wanted[n] = ROOT / "skills" / n

    missing = [n for n, p in wanted.items() if not (p / "SKILL.md").exists()]
    for n in missing:
        print(f"مفقودة: {n} ({wanted[n]})", file=sys.stderr)

    # احذف الروابط القديمة التي لم تعد مسجلة
    for link in TARGET.iterdir():
        if link.is_symlink() and link.name not in wanted:
            link.unlink()
            print(f"أُزيل: {link.name}")

    for n, src in wanted.items():
        if n in missing:
            continue
        link = TARGET / n
        rel = os.path.relpath(src, TARGET)
        if link.is_symlink() and os.readlink(link) == rel:
            continue
        if link.exists() or link.is_symlink():
            link.unlink()
        link.symlink_to(rel, target_is_directory=True)
        print(f"رُبط: {n}")
    print(f"المسجّل: {len(wanted) - len(missing)} مهارة، المفقود: {len(missing)}")
    return 1 if missing else 0


if __name__ == "__main__":
    sys.exit(main())
