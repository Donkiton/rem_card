#!/usr/bin/env python
"""
Restore-drill check for RemCard SQLite backups.

This script restores a validated backup into a separate test directory and runs
quick_check + integrity_check on the restored copy. It never replaces the live
database.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import sqlite3
import sys
from datetime import datetime
from pathlib import Path
from typing import Optional


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))
try:
    from _local_rem_card_bootstrap import bootstrap_local_rem_card

    bootstrap_local_rem_card()
except Exception:
    pass

from rem_card.app.paths import BACKUP_HEALTH_DIR, BACKUP_VALIDATION_REPORTS_DIR, BACKUPS_RC_DIR, ensure_directories
from rem_card.app.sqlite_shared import list_backup_candidates, restore_database, run_integrity_check, run_quick_check, validate_sqlite_file


def _sha256_file(path: str) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _select_backup(backup_dir: str, explicit_source: Optional[str], max_files: int) -> tuple[Optional[str], list[dict]]:
    if explicit_source:
        ok, reason = validate_sqlite_file(explicit_source)
        return (explicit_source if ok else None), [
            {
                "path": explicit_source,
                "status": "ok" if ok else "invalid",
                "reason": reason,
            }
        ]

    checked: list[dict] = []
    candidates = list_backup_candidates(backup_dir=backup_dir)
    if max_files > 0:
        candidates = candidates[:max_files]
    for path in candidates:
        ok, reason = validate_sqlite_file(path)
        checked.append(
            {
                "path": path,
                "status": "ok" if ok else "invalid",
                "reason": reason,
            }
        )
        if ok:
            return path, checked
    return None, checked


def _validate_restored_db(restored_db_path: str) -> tuple[bool, str]:
    conn = sqlite3.connect(restored_db_path, check_same_thread=False, isolation_level=None, timeout=5.0)
    try:
        ok, reason = run_quick_check(conn)
        if not ok:
            return False, f"quick_check failed: {reason}"
        ok, reason = run_integrity_check(conn)
        if not ok:
            return False, f"integrity_check failed: {reason}"
        return True, "quick_check and integrity_check passed"
    finally:
        conn.close()


def run_restore_drill(
    *,
    backup_dir: str,
    report_dir: str,
    restore_root: str,
    source: Optional[str],
    max_files: int,
    cleanup_restored: bool,
) -> dict:
    os.makedirs(report_dir, exist_ok=True)
    os.makedirs(restore_root, exist_ok=True)

    selected, checked = _select_backup(backup_dir, source, max_files)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    drill_dir = os.path.join(restore_root, f"restore_drill_{stamp}")
    restored_db_path = os.path.join(drill_dir, "rao_journal_restored.db")
    os.makedirs(drill_dir, exist_ok=True)

    result = "failed"
    validation_status = "not_run"
    restored_hash = None
    error = None

    if not selected:
        error = "No valid backup source found"
    else:
        try:
            restore_database(restored_db_path, selected)
            ok, reason = _validate_restored_db(restored_db_path)
            validation_status = "ok" if ok else "invalid"
            if ok:
                restored_hash = _sha256_file(restored_db_path)
                result = "ok"
            else:
                error = reason
        except Exception as exc:
            error = str(exc)

    report = {
        "timestamp": datetime.now().isoformat(),
        "restore_drill_result": result,
        "last_successful_restore_test_at": datetime.now().isoformat() if result == "ok" else None,
        "backup_dir": backup_dir,
        "backup_source_path": selected,
        "restore_root": restore_root,
        "restored_db_path": restored_db_path,
        "restored_db_hash": restored_hash,
        "validation_status": validation_status,
        "checked_sources": checked,
        "error": error,
    }

    report_path = os.path.join(report_dir, f"restore_drill_{stamp}.json")
    latest_path = os.path.join(report_dir, "restore_drill_latest.json")
    with open(report_path, "w", encoding="utf-8") as handle:
        json.dump(report, handle, ensure_ascii=False, indent=2)
    with open(latest_path, "w", encoding="utf-8") as handle:
        json.dump(report, handle, ensure_ascii=False, indent=2)
    report["report_path"] = report_path
    report["latest_path"] = latest_path

    if cleanup_restored:
        shutil.rmtree(drill_dir, ignore_errors=True)
        report["restored_copy_removed"] = True
    else:
        report["restored_copy_removed"] = False
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description="Restore a validated backup into a test folder and verify it")
    parser.add_argument("--backup-dir", default=BACKUPS_RC_DIR, help="Directory with backup .db files")
    parser.add_argument("--source", default=None, help="Explicit backup file to restore")
    parser.add_argument("--report-dir", default=BACKUP_VALIDATION_REPORTS_DIR, help="Directory for JSON reports")
    parser.add_argument("--restore-root", default=os.path.join(BACKUP_HEALTH_DIR, "restore_drills"), help="Test restore directory")
    parser.add_argument("--max-files", type=int, default=20, help="Newest backup files to scan when --source is omitted")
    parser.add_argument("--cleanup-restored", action="store_true", help="Remove restored test DB after validation")
    args = parser.parse_args()

    ensure_directories()
    report = run_restore_drill(
        backup_dir=args.backup_dir,
        report_dir=args.report_dir,
        restore_root=args.restore_root,
        source=args.source,
        max_files=max(1, int(args.max_files)),
        cleanup_restored=bool(args.cleanup_restored),
    )
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report.get("restore_drill_result") == "ok" else 1


if __name__ == "__main__":
    raise SystemExit(main())
