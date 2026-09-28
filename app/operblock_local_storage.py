"""Local backups and conservative per-case retention for the operation archive."""
from __future__ import annotations

import sqlite3
from contextlib import closing
from datetime import datetime, timedelta
from pathlib import Path

from rem_card.app.operblock_offline_store import get_operblock_offline_root
from rem_card.app.sqlite_shared import backup_connection


def backup_local_operations(local_root=None) -> str | None:
    root = Path(local_root or get_operblock_offline_root())
    source = root / "active" / "operblock_local.db"
    if not source.is_file():
        return None
    directory = root / "backups"
    directory.mkdir(parents=True, exist_ok=True)
    # Avoid producing an identical full copy on every delivery retry.
    wal = Path(str(source) + "-wal")
    stamp = str(max(source.stat().st_mtime_ns, wal.stat().st_mtime_ns if wal.exists() else 0))
    target = directory / f"operations_{stamp}.db"
    if target.is_file():
        return str(target)
    with closing(sqlite3.connect(source.absolute().as_uri() + "?mode=ro", uri=True, timeout=1)) as conn:
        backup_connection(conn, str(target), validate=True,
                          invalid_dir=str(root / "quarantine"), lock_wait_sec=1,
                          source="operblock_local_backup")
    # The live database keeps all pending cases and 90 days of confirmed cases.
    # Keep three validated recovery generations, not one full database per minute.
    generations = sorted(directory.glob("operations_*.db"),
                         key=lambda value: value.stat().st_mtime, reverse=True)
    for old in generations[3:]:
        if old == target:
            continue
        try:
            old.unlink()
            Path(str(old) + ".meta.json").unlink(missing_ok=True)
        except OSError:
            pass
    return str(target)


def latest_valid_local_backup(local_root=None) -> str | None:
    root = Path(local_root or get_operblock_offline_root())
    for path in sorted((root / "backups").glob("operations_*.db"),
                       key=lambda value: value.stat().st_mtime, reverse=True):
        try:
            with closing(sqlite3.connect(path.absolute().as_uri() + "?mode=ro", uri=True, timeout=1)) as conn:
                tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
                if {"operation_cases", "patients", "admissions"} <= tables and conn.execute("PRAGMA integrity_check").fetchall() == [("ok",)]:
                    return str(path)
        except sqlite3.Error:
            continue
    return None


def retention_due(local_root=None) -> bool:
    path = Path(local_root or get_operblock_offline_root()) / "active" / "operblock_local.db"
    if not path.is_file():
        return False
    with closing(sqlite3.connect(path.absolute().as_uri() + "?mode=ro", uri=True, timeout=0.25)) as conn:
        if not conn.execute("SELECT 1 FROM sqlite_master WHERE name='operation_cases'").fetchone():
            return False
        return bool(conn.execute("""SELECT 1 FROM operation_cases
            WHERE status='closed' AND migration_status='verified'
            AND julianday('now') - julianday(migrated_at) >= 90 LIMIT 1""").fetchone())


def prune_verified_local_cases(network_manager, local_root=None) -> int:
    from rem_card.app.operblock_offline_migration import verify_local_case_receipt
    from rem_card.services.operblock.archive import OperBlockArchiveLifecycleMixin

    path = Path(local_root or get_operblock_offline_root()) / "active" / "operblock_local.db"
    if not path.is_file():
        return 0
    deleted = 0
    with closing(sqlite3.connect(path.absolute().as_uri() + "?mode=rw", uri=True,
                         timeout=0.25, isolation_level=None)) as conn:
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        rows = conn.execute("""SELECT * FROM operation_cases
            WHERE status='closed' AND migration_status='verified' AND migrated_at IS NOT NULL""").fetchall()
        for row in rows:
            try:
                sent_at = datetime.fromisoformat(str(row["migrated_at"]).replace("Z", "+00:00"))
                if sent_at.tzinfo is None:
                    sent_at = sent_at.astimezone()
                if datetime.now().astimezone() - sent_at < timedelta(days=90):
                    continue
                # No local write lock while touching a possibly disconnected SMB.
                version = conn.execute("PRAGMA data_version").fetchone()[0]
                if not verify_local_case_receipt(conn, network_manager, int(row["id"])):
                    continue
                conn.execute("BEGIN IMMEDIATE")
                if conn.execute("PRAGMA data_version").fetchone()[0] != version:
                    conn.execute("ROLLBACK")
                    continue
                if conn.execute("SELECT 1 FROM sqlite_master WHERE name='operblock_archive_edit_history'").fetchone():
                    conn.execute("DELETE FROM operblock_archive_edit_history WHERE operation_case_id=?", (row["id"],))
                OperBlockArchiveLifecycleMixin._hard_delete_archived_operation_cases(conn.cursor(), [row])
                conn.execute("COMMIT")
                deleted += 1
            except (sqlite3.Error, ValueError, TypeError):
                if conn.in_transaction:
                    conn.execute("ROLLBACK")
                # Keep any case whose references or confirmation cannot be checked.
                continue
    return deleted
