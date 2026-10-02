"""Explicit central gateway for the local operating-room runtime.

Never derives the destination from app.paths: that module may describe a local
clinical session. Never creates a missing medical database.
"""
from __future__ import annotations

import json
import os
import sqlite3
import uuid
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

from rem_card.app.operblock_offline_store import _atomic_json_write, get_operblock_offline_root
from rem_card.app.sqlite_shared import backup_connection, FileWriteLock


def configure_operblock_destination(central_root: str, *, local_root: str | None = None) -> None:
    root = Path(local_root or get_operblock_offline_root()).resolve()
    target = str(central_root or "").strip()
    if not target:
        return
    target = os.path.abspath(target)
    if os.path.normcase(target) in {os.path.normcase(str(root)), os.path.normcase(str(root / "active"))}:
        raise ValueError("Основная база совпадает с локальным каталогом оперблока.")
    _atomic_json_write(str(root / "destination.json"), {"central_root": target})


def get_operblock_destination(local_root: str | None = None) -> str:
    path = Path(local_root or get_operblock_offline_root()) / "destination.json"
    try:
        return str(json.loads(path.read_text(encoding="utf-8")).get("central_root") or "")
    except FileNotFoundError:
        return ""


class CentralArchiveConnection:
    """Small existing-DB connection, intended only for bounded background jobs."""
    def __init__(self, central_root: str):
        self.root = Path(central_root).absolute()
        self.db_path = str(self.root / "archiv" / "rao_journal.db")
        self.remcard_db_path = self.db_path
        self.runtime_context = SimpleNamespace(mode="network", baza_dir=str(self.root))
        self._remcard_conn = sqlite3.connect(
            Path(self.db_path).as_uri() + "?mode=rw", uri=True, timeout=1.0, isolation_level=None,
        )
        self._remcard_conn.row_factory = sqlite3.Row
        try:
            self._remcard_conn.execute("PRAGMA foreign_keys=ON")
            self._remcard_conn.execute("PRAGMA busy_timeout=1000")
            # A file at the configured path is insufficient: require RemCard's
            # clinical schema before any backup, receipt or case can be written.
            for table in ("patients", "admissions", "operation_cases", "orders", "vitals"):
                if not self._remcard_conn.execute(
                    "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,),
                ).fetchone():
                    raise RuntimeError("Основная база не содержит совместимую схему RemCard.")
        except BaseException:
            self._remcard_conn.close()
            raise

    def fetch_one_remcard(self, query, params=()):
        return self._remcard_conn.execute(query, params).fetchone()

    def fetch_all_remcard(self, query, params=()):
        return self._remcard_conn.execute(query, params).fetchall()

    def run_write_operation(self, operation, source="operblock_local_export", **_kwargs):
        conn = self._remcard_conn
        lock = FileWriteLock(str(self.root / "archiv" / "db.lock"))
        if not lock.acquire(f"operblock-export:{os.getpid()}", source):
            raise sqlite3.OperationalError("Основная база занята; отправка будет повторена.")
        cursor = conn.cursor()
        try:
            conn.execute("BEGIN IMMEDIATE")
            result = operation(cursor)
            conn.execute("COMMIT")
            return result
        except BaseException:
            if conn.in_transaction:
                conn.execute("ROLLBACK")
            raise
        finally:
            cursor.close()
            lock.release()

    def run_read_operation(self, operation, source="operblock_local_read"):
        cursor = self._remcard_conn.cursor()
        try:
            return operation(cursor)
        finally:
            cursor.close()

    def create_validated_backup(self, *, prefix, source):
        folder = self.root / "backups" / "valid"
        folder.mkdir(parents=True, exist_ok=True)
        path = folder / f"{prefix}_{datetime.now():%Y%m%d_%H%M%S}_{uuid.uuid4().hex[:8]}.db"
        backup_connection(
            self._remcard_conn, str(path), invalid_dir=str(self.root / "quarantine"),
            validate=True, source=source, lock_wait_sec=2.0,
        )
        return str(path)

    def close(self):
        self._remcard_conn.close()


@contextmanager
def open_central_for_operblock(central_root: str | None = None, *, local_root: str | None = None):
    target = central_root or get_operblock_destination(local_root)
    if not target:
        raise RuntimeError("Основная база для отправки оперблока ещё не настроена.")
    manager = CentralArchiveConnection(target)
    try:
        yield manager
    finally:
        manager.close()
