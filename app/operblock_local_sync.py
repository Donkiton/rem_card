"""Bounded background delivery of completed local operation cases."""
from __future__ import annotations

import multiprocessing
import os
import sqlite3
from contextlib import closing
import time
from dataclasses import asdict
from pathlib import Path

from PySide6.QtCore import QObject, QTimer, Signal

from rem_card.app.operblock_offline_store import _atomic_json_write, get_operblock_offline_root


def completed_pending_count(local_root: str) -> int:
    path = Path(local_root) / "active" / "operblock_local.db"
    if not path.is_file():
        return 0
    with closing(sqlite3.connect(path.absolute().as_uri() + "?mode=ro", uri=True, timeout=0.25)) as conn:
        conn.execute("PRAGMA query_only=ON")
        if not conn.execute("SELECT 1 FROM sqlite_master WHERE name='operation_cases'").fetchone():
            return 0
        return int(conn.execute("""
            SELECT COUNT(*) FROM operation_cases WHERE status='closed'
            AND COALESCE(migration_status,'') NOT IN ('verified','discarded','shadow')
            AND migrated_at IS NULL AND COALESCE(excluded_from_migration,0)=0
        """).fetchone()[0])


def _export_worker(central_root: str, local_root: str, connection) -> None:
    # Spawned worker only. Its lifetime is bounded by the UI scheduler, including
    # a hung SMB open or a lost COMMIT acknowledgement. UUID receipts allow retry.
    try:
        from rem_card.app.operblock_local_destination import open_central_for_operblock
        from rem_card.app.operblock_offline_migration import run_pending_operblock_offline_migration
        from rem_card.app.operblock_local_storage import backup_local_operations, prune_verified_local_cases, retention_due

        backup_local_operations(local_root)
        pending = completed_pending_count(local_root)
        if not central_root or (not pending and not retention_due(local_root)):
            connection.send({"ok": True, "pending": completed_pending_count(local_root)})
            return
        with open_central_for_operblock(central_root, local_root=local_root) as manager:
            if pending:
                result = run_pending_operblock_offline_migration(manager, root=local_root)
                if not result.ok:
                    connection.send(asdict(result))
                    return
            prune_verified_local_cases(manager, local_root)
        connection.send(asdict(result) if pending else {"ok": True})
    except BaseException as exc:
        connection.send({"ok": False, "reason": str(exc), "error_class": type(exc).__name__})
    finally:
        connection.close()


class OperBlockLocalSyncScheduler(QObject):
    status_changed = Signal(dict)
    WORKER_TIMEOUT_SEC = 45.0
    RETRY_DELAYS_SEC = (15, 30, 60, 120, 300)

    def __init__(self, central_root: str, parent=None, local_root: str | None = None):
        super().__init__(parent)
        self.central_root = str(central_root or "")
        self.local_root = os.path.abspath(local_root or get_operblock_offline_root())
        self._process = None
        self._receiver = None
        self._started = 0.0
        self._failures = 0
        self._last_backup_started = 0.0
        self._closing_callback = None
        self._close_deadline = None
        self._stopped = False
        self._retry = QTimer(self)
        self._retry.setSingleShot(True)
        self._retry.timeout.connect(self.request_sync)
        self._poll = QTimer(self)
        self._poll.setInterval(100)
        self._poll.timeout.connect(self._poll_result)
        self._retry.start(1000)

    def request_sync(self):
        if self._stopped or self._process is not None:
            return
        self._retry.stop()
        try:
            count = completed_pending_count(self.local_root)
            local_exists = (Path(self.local_root) / "active" / "operblock_local.db").is_file()
            backup_due = local_exists and time.monotonic() - self._last_backup_started >= 60
            if (not count or not self.central_root) and not backup_due:
                self._publish({"ok": True, "pending": count, "state": "waiting" if count else "idle"})
                self._retry.start(15000)
                return
            context = multiprocessing.get_context("spawn")
            receiver, sender = context.Pipe(duplex=False)
            process = context.Process(target=_export_worker, args=(self.central_root, self.local_root, sender), daemon=True)
            try:
                process.start()
            except BaseException:
                receiver.close()
                sender.close()
                raise
            sender.close()
            self._receiver, self._process = receiver, process
            self._started = time.monotonic()
            self._last_backup_started = self._started
            self._publish({"ok": True, "pending": count, "state": "sending"})
            self._poll.start()
        except Exception as exc:
            self._finish({"ok": False, "state": "waiting", "reason": str(exc)})

    def _poll_result(self):
        if self._process is None:
            return
        now = time.monotonic()
        try:
            if self._receiver.poll():
                self._finish(self._receiver.recv())
                return
        except (EOFError, OSError):
            pass
        if not self._process.is_alive():
            self._finish({"ok": False, "reason": "Процесс отправки завершился без подтверждения."})
        elif now - self._started >= self.WORKER_TIMEOUT_SEC or (
            self._close_deadline is not None and now >= self._close_deadline
        ):
            self._finish({"ok": False, "reason": "Отправка не подтверждена; локальный случай сохранён для повторной попытки."})

    def _dispose_worker(self):
        self._poll.stop()
        process, self._process = self._process, None
        receiver, self._receiver = self._receiver, None
        if receiver is not None:
            receiver.close()
        if process is not None:
            if process.is_alive():
                process.terminate()
            process.join(timeout=0.2)
            if process.is_alive():
                process.kill()
                process.join(timeout=0.2)
            if not process.is_alive():
                process.close()

    def _publish(self, payload):
        self.status_changed.emit(dict(payload))
        # Operational state only; no patient data is recorded here.
        try:
            _atomic_json_write(str(Path(self.local_root) / "delivery_status.json"), dict(payload))
        except OSError:
            pass

    def _finish(self, result):
        self._dispose_worker()
        self._failures = 0 if result.get("ok") else self._failures + 1
        self._publish({**result, "state": "idle" if result.get("ok") else "waiting"})
        if self._closing_callback is not None:
            callback, self._closing_callback = self._closing_callback, None
            self.stop()
            QTimer.singleShot(0, callback)
        elif not self._stopped:
            delay = self.RETRY_DELAYS_SEC[min(max(self._failures - 1, 0), len(self.RETRY_DELAYS_SEC) - 1)]
            self._retry.start(delay * 1000)

    def set_central_root(self, central_root):
        from rem_card.app.operblock_local_destination import configure_operblock_destination
        self.central_root = str(central_root or "")
        configure_operblock_destination(self.central_root, local_root=self.local_root)
        self.request_sync()

    def begin_close(self, callback):
        if self._closing_callback is not None:
            return
        self._closing_callback = callback
        self._close_deadline = time.monotonic() + 3.0
        self.request_sync()
        if self._process is None and self._closing_callback is not None:
            self._closing_callback = None
            self.stop()
            QTimer.singleShot(0, callback)

    def stop(self):
        self._stopped = True
        self._retry.stop()
        self._dispose_worker()
