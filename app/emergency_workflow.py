"""Durable decisions for the nurse's emergency session; no implicit DB paths."""
from __future__ import annotations

import os
import sqlite3
from contextlib import closing
from datetime import datetime

from rem_card.app.emergency_metadata import atomic_write_json
from rem_card.app.emergency_paths import active_session_dir
from rem_card.app.sqlite_uri import build_sqlite_file_uri


RESUMABLE_STATUSES = frozenset({"active", "waiting", "merge_pending", "merging", "merge_failed"})


def validate_emergency_patient_source(path: str) -> str:
    """Absence of a patient is never interpreted as a request to erase patients."""
    try:
        with closing(sqlite3.connect(build_sqlite_file_uri(path, mode="ro"), uri=True)) as conn:
            patient = conn.execute(
                "SELECT 1 FROM admissions a JOIN patients p ON p.id=a.patient_id LIMIT 1"
            ).fetchone()
        if patient is None:
            return "В исходной аварийной копии нет пациентов. Работа с пустой копией запрещена."
    except (OSError, sqlite3.Error) as exc:
        return f"Не удалось проверить пациентов исходной копии: {exc}"
    return ""


def database_cycle(path: str) -> str:
    with closing(sqlite3.connect(build_sqlite_file_uri(path, mode="ro"), uri=True)) as conn:
        row = conn.execute("SELECT value FROM meta WHERE key='db_cycle_started_at'").fetchone()
        return str(row[0] or "") if row else ""


def authorization_path(store, session_id: str) -> str:
    return os.path.join(active_session_dir(store.resolve_root(), session_id), "merge_authorization.json")


def authorize_patient_merge(store, session_id: str, selection: dict) -> str:
    if selection.get("blockers"):
        raise ValueError("Выбор пациентов содержит нерешённые зависимости")
    ids = sorted({int(value) for value in selection.get("selected_admission_ids", [])})
    digest = str(selection.get("plan_digest") or "")
    if not ids or not digest:
        raise ValueError("Не выбраны пациенты или отсутствует проверенный план переноса")
    payload = {
        "schema_version": 1, "session_id": str(session_id),
        "selected_admission_ids": ids, "plan_digest": digest,
        "approved_at": datetime.now().astimezone().isoformat(timespec="seconds"),
    }
    path = authorization_path(store, session_id)
    atomic_write_json(path, payload)
    return path


def resume_local_work(store, session_id: str) -> None:
    # Any new local work requires a fresh review, even after a failed merge.
    from rem_card.app.emergency_restore_probe import clear_merge_ready_marker

    reader = getattr(store, "read_active_session", None)
    if callable(reader):
        metadata = reader(session_id)
        if (getattr(metadata, "merge_recovery_required", False)
                or getattr(metadata, "status", "") in {"merge_pending", "merging"}):
            raise ValueError("Сначала необходимо установить результат предыдущего переноса")
    try:
        os.remove(authorization_path(store, session_id))
    except FileNotFoundError:
        pass
    clear_merge_ready_marker(store.resolve_root(), session_id)
    store.mark_session_status(session_id, "active")
