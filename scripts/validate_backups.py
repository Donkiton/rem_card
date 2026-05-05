#!/usr/bin/env python
"""
Regular backup validation script for RemCard/Journal SQLite backups.

Usage examples:
  set PYTHONPATH=C:\Project
  python %REMCARD_PROJECT_ROOT%\scripts\validate_backups.py
  python %REMCARD_PROJECT_ROOT%\scripts\validate_backups.py --move-invalid
  python %REMCARD_PROJECT_ROOT%\scripts\validate_backups.py --max-files 20
"""

from __future__ import annotations

import argparse
import json
import os
from datetime import datetime

from rem_card.app.paths import (
    BACKUPS_RC_DIR,
    BACKUP_VALIDATION_REPORTS_DIR,
    INVALID_BACKUPS_DIR,
    ensure_directories,
)
from rem_card.app.sqlite_shared import list_backup_candidates, validate_sqlite_file


def _iso_from_ts(ts: float) -> str:
    return datetime.fromtimestamp(ts).isoformat()


def _move_invalid_backup(path: str, invalid_dir: str) -> str:
    os.makedirs(invalid_dir, exist_ok=True)
    name = os.path.basename(path)
    target = os.path.join(invalid_dir, name)
    if os.path.exists(target):
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        target = os.path.join(invalid_dir, f"{stamp}_{name}")
    os.replace(path, target)
    return target


def run_validation(
    *,
    backup_dir: str,
    report_dir: str,
    invalid_dir: str,
    max_files: int,
    move_invalid: bool,
) -> dict:
    os.makedirs(report_dir, exist_ok=True)
    os.makedirs(invalid_dir, exist_ok=True)

    candidates = list_backup_candidates(backup_dir=backup_dir)
    if max_files > 0:
        candidates = candidates[:max_files]

    checked = []
    healthy = 0
    invalid = 0
    newest_healthy = None

    for path in candidates:
        stat = os.stat(path)
        ok, reason = validate_sqlite_file(path)
        status = "ok" if ok else "invalid"
        item = {
            "path": path,
            "name": os.path.basename(path),
            "size_bytes": int(stat.st_size),
            "mtime": _iso_from_ts(stat.st_mtime),
            "status": status,
            "reason": reason,
        }

        if ok:
            healthy += 1
            if newest_healthy is None:
                newest_healthy = path
        else:
            invalid += 1
            if move_invalid:
                moved_to = _move_invalid_backup(path, invalid_dir=invalid_dir)
                item["moved_to"] = moved_to

        checked.append(item)

    report = {
        "timestamp": datetime.now().isoformat(),
        "backup_dir": backup_dir,
        "report_dir": report_dir,
        "invalid_dir": invalid_dir,
        "checked_files": len(checked),
        "healthy_files": healthy,
        "invalid_files": invalid,
        "newest_healthy_backup": newest_healthy,
        "move_invalid": move_invalid,
        "items": checked,
    }

    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    report_path = os.path.join(report_dir, f"backup_validation_{stamp}.json")
    latest_path = os.path.join(report_dir, "backup_validation_latest.json")
    with open(report_path, "w", encoding="utf-8") as fh:
        json.dump(report, fh, ensure_ascii=False, indent=2)
    with open(latest_path, "w", encoding="utf-8") as fh:
        json.dump(report, fh, ensure_ascii=False, indent=2)

    report["report_path"] = report_path
    report["latest_path"] = latest_path
    return report


def main():
    parser = argparse.ArgumentParser(description="Validate SQLite backups and write a health report")
    parser.add_argument("--backup-dir", default=BACKUPS_RC_DIR, help="Directory with backup .db files")
    parser.add_argument("--report-dir", default=BACKUP_VALIDATION_REPORTS_DIR, help="Directory for JSON reports")
    parser.add_argument("--invalid-dir", default=INVALID_BACKUPS_DIR, help="Directory where invalid backups are moved")
    parser.add_argument("--max-files", type=int, default=0, help="Limit number of newest files to validate (0 = all)")
    parser.add_argument("--move-invalid", action="store_true", help="Move invalid backups to invalid-dir")
    args = parser.parse_args()

    ensure_directories()
    report = run_validation(
        backup_dir=args.backup_dir,
        report_dir=args.report_dir,
        invalid_dir=args.invalid_dir,
        max_files=max(0, args.max_files),
        move_invalid=args.move_invalid,
    )
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()

