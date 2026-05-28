"""
cleanup_temp.py — удаление файлов из workspace/temp/ старше N дней.

Запускается cron'ом (например раз в неделю). Safe-by-default:
не трогает workspace/permanent/, логи, агентов.

AIOS_ROOT берётся из env, иначе вычисляется от расположения скрипта (../../).

Usage:
    python3 cleanup_temp.py [--dry-run] [--days N]
"""

from __future__ import annotations

import argparse
import os
import time
from pathlib import Path


def _temp_dir() -> Path:
    root = os.getenv("AIOS_ROOT")
    base = Path(root) if root else Path(__file__).resolve().parent.parent.parent
    return base / "workspace" / "temp"


def cleanup(days: int, dry_run: bool) -> dict:
    temp_dir = _temp_dir()
    threshold = days * 86400
    now = time.time()
    stats = {"removed": 0, "bytes_freed": 0, "scanned": 0}

    if not temp_dir.exists():
        return stats

    for root, dirs, files in os.walk(temp_dir):
        for f in files:
            path = Path(root) / f
            try:
                age = now - path.stat().st_mtime
            except FileNotFoundError:
                continue
            stats["scanned"] += 1
            if age > threshold:
                try:
                    size = path.stat().st_size
                except OSError:
                    continue
                stats["bytes_freed"] += size
                stats["removed"] += 1
                if not dry_run:
                    try:
                        path.unlink()
                    except OSError:
                        pass
    return stats


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--days", type=int, default=14)
    args = ap.parse_args()

    s = cleanup(args.days, args.dry_run)
    mb = s["bytes_freed"] / (1024 * 1024)
    verb = "would remove" if args.dry_run else "removed"
    print(f"{verb} {s['removed']} files ({mb:.1f} MB), scanned {s['scanned']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
