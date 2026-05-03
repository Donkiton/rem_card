import sqlite3

from rem_card.app.paths import (
    BAZA_DIR,
    BACKUPS_VALID_DIR,
    CLIENT_POLICY_PATH,
    DB_LOCK_PATH,
    INVALID_BACKUPS_DIR,
    JOURNAL_DB_PATH,
    ensure_directories,
)
from rem_card.app.schema_migration_guard import ensure_unified_schema_with_migration_backup
from rem_card.app.sqlite_shared import FileWriteLock, SQLiteWriteController, configure_connection, run_integrity_check


def update_db():
    ensure_directories()

    print(f"[DB PATH] unified_runtime.db -> {JOURNAL_DB_PATH}")

    profile_lock = FileWriteLock(DB_LOCK_PATH, stale_timeout_sec=10 * 60)
    if not profile_lock.acquire(owner_id="update_db_script", source="connection_profile"):
        raise RuntimeError("Could not acquire db lock for connection profile")

    conn = sqlite3.connect(JOURNAL_DB_PATH, check_same_thread=False, isolation_level=None, timeout=5.0)
    try:
        configure_connection(conn)
    finally:
        profile_lock.release()

    try:
        ok, result = run_integrity_check(conn)
        if not ok:
            raise RuntimeError(f"Integrity check failed: {result}")

        controller = SQLiteWriteController(
            db_path=JOURNAL_DB_PATH,
            lock_path=DB_LOCK_PATH,
            owner_id="update_db_script",
        )
        ensure_unified_schema_with_migration_backup(
            conn,
            db_path=JOURNAL_DB_PATH,
            backup_dir=BACKUPS_VALID_DIR,
            invalid_dir=INVALID_BACKUPS_DIR,
            policy_path=CLIENT_POLICY_PATH,
            baza_dir=BAZA_DIR,
            controller=controller,
            source="update_db_script",
        )

        print("Unified schema migration successful")
    finally:
        conn.close()


if __name__ == "__main__":
    update_db()
