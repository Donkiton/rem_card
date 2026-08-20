from __future__ import annotations

import json
import sqlite3
import sys
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

import pytest


PROJECT_DIR = Path(__file__).resolve().parents[1]
PACKAGE_PARENT = PROJECT_DIR.parent
if str(PACKAGE_PARENT) not in sys.path:
    sys.path.insert(0, str(PACKAGE_PARENT))

from rem_card.services.database_info_service import (  # noqa: E402
    DatabaseInfoCollectionCancelled,
    DatabaseInfoService,
)


def _create_medical_db(path: Path, *, cycle_started_at: int, admission_date: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(path) as conn:
        conn.executescript(
            """
            CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT);
            CREATE TABLE admissions (
                id INTEGER PRIMARY KEY,
                patient_id INTEGER,
                admission_datetime TEXT,
                outcome TEXT
            );
            CREATE TABLE patients (id INTEGER PRIMARY KEY);
            CREATE TABLE beds (id INTEGER PRIMARY KEY, status TEXT, current_admission_id INTEGER);
            """
        )
        conn.execute("INSERT INTO meta VALUES ('db_cycle_started_at', ?)", (cycle_started_at,))
        conn.execute("INSERT INTO meta VALUES ('schema_fastpath_revision', '17')")
        conn.execute("INSERT INTO patients VALUES (1)")
        conn.execute(
            "INSERT INTO admissions VALUES (1, 1, ?, '')",
            (admission_date,),
        )


def _create_settings_db(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(path) as conn:
        conn.execute(
            "CREATE TABLE settings_meta (key TEXT PRIMARY KEY, value TEXT, updated_at TEXT)"
        )
        conn.execute(
            "INSERT INTO settings_meta VALUES ('schema_version', '9', '2026-01-01')"
        )
        conn.execute(
            "INSERT INTO settings_meta VALUES "
            "('settings_db_created_at', '2026-01-02T03:04:05+10:00', '2026-01-02')"
        )


def _create_reference_db(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(path) as conn:
        conn.execute("CREATE TABLE codes (code TEXT PRIMARY KEY)")


def _runtime_context(root: Path):
    return SimpleNamespace(
        medical_db_path=str(root / "archiv" / "rao_journal.db"),
        settings_db_path=str(root / "settings" / "remcard_settings.db"),
        medical_backups_root_dir=str(root / "backups"),
        medical_invalid_backups_dir=str(root / "backup_health" / "invalid_backups"),
        settings_backups_dir=str(root / "settings" / "backups"),
        settings_readonly=False,
        source_label="test",
    )


def _write_backup(path: Path, metadata: dict | None = None) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"SQLite backup fixture")
    if metadata is not None:
        Path(f"{path}.meta.json").write_text(
            json.dumps(metadata, ensure_ascii=False),
            encoding="utf-8",
        )


def test_collects_working_databases_cycles_backups_and_history(tmp_path):
    context = _runtime_context(tmp_path)
    current_path = Path(context.medical_db_path)
    archive_path = current_path.parent / "rao_journal_archived_20260101_000000.db"
    settings_path = Path(context.settings_db_path)
    reference_path = tmp_path / "mkb10.db"
    _create_medical_db(
        current_path,
        cycle_started_at=int(datetime(2026, 7, 1, 8, 0).timestamp()),
        admission_date="2026-07-02 09:30:00",
    )
    _create_medical_db(
        archive_path,
        cycle_started_at=int(datetime(2026, 1, 1, 8, 0).timestamp()),
        admission_date="2026-01-03 10:00:00",
    )
    _create_settings_db(settings_path)
    _create_reference_db(reference_path)

    primary_backup = tmp_path / "backups" / "valid" / "pre_rotation_manual_rao_journal_20260701_080000.db"
    _write_backup(
        primary_backup,
        {
            "created_at": "2026-07-01T08:01:00+10:00",
            "size_bytes": 21,
            "quick_check": "ok",
            "integrity_check": "ok",
            "sha256": "abc123",
            "rotation": {"source": "manual"},
        },
    )
    settings_backup = tmp_path / "settings" / "backups" / "settings_manual_20260702_090000.db"
    _write_backup(
        settings_backup,
        {
            "created_at": "2026-07-02T09:00:00+10:00",
            "size_bytes": 21,
            "quick_check": "ok",
            "integrity_check": "ok",
            "backup_kind": "settings_manual",
            "settings_source": "manual_settings_backup",
        },
    )
    no_meta_backup = tmp_path / "backups" / "shutdown_rao_journal_20260703_100000.db"
    _write_backup(no_meta_backup)

    tracked_files = [current_path, archive_path, settings_path, reference_path, primary_backup]
    before = {path: (path.stat().st_size, path.stat().st_mtime_ns) for path in tracked_files}
    snapshot = DatabaseInfoService(
        context,
        reference_db_path=str(reference_path),
    ).collect()
    after = {path: (path.stat().st_size, path.stat().st_mtime_ns) for path in tracked_files}

    assert before == after
    assert [(item.key, item.status) for item in snapshot.databases] == [
        ("medical", "Доступна"),
        ("settings", "Доступна"),
        ("reference", "Доступна"),
    ]
    assert snapshot.databases[0].schema_version == "17"
    assert snapshot.databases[1].schema_version == "9"
    assert snapshot.databases[1].created_at == datetime(2026, 1, 2, 3, 4, 5)
    assert len(snapshot.cycles) == 2
    assert snapshot.cycles[0].is_current
    assert snapshot.cycles[1].path == str(archive_path)
    assert [item.path for item in snapshot.backups] == [
        str(no_meta_backup),
        str(settings_backup),
        str(primary_backup),
    ]
    assert snapshot.backups[1].kind == "Ручной"
    assert snapshot.backups[1].scope == "Настройки"
    assert snapshot.backups[2].kind == "Перед ротацией"
    assert snapshot.backups[2].source == "manual"
    assert snapshot.backups[2].validation_status == "Проверен"
    assert snapshot.backups[0].validation_status == "Без метаданных"
    rotation_event = next(event for event in snapshot.events if event.event_type == "Ротация")
    assert rotation_event.title == "БД перенесена в архив"
    assert rotation_event.occurred_at == datetime(2026, 1, 1, 0, 0)
    assert "Цикл начат 01.01.2026 08:00" in rotation_event.description
    assert any(event.title == "Перед ротацией: Основная БД" for event in snapshot.events)
    assert snapshot.warnings == ()


def test_marks_invalid_and_broken_metadata_without_opening_backup(tmp_path):
    context = _runtime_context(tmp_path)
    invalid_backup = Path(context.medical_invalid_backups_dir) / "broken.db"
    _write_backup(invalid_backup)
    broken_meta_backup = tmp_path / "settings" / "backups" / "settings_pre_test.db"
    _write_backup(broken_meta_backup)
    Path(f"{broken_meta_backup}.meta.json").write_text("{not-json", encoding="utf-8")
    migration_invalid = tmp_path / "settings" / "migration_backups" / "invalid" / "migration.db"
    _write_backup(migration_invalid)
    changed_backup = tmp_path / "backups" / "valid" / "changed.db"
    _write_backup(
        changed_backup,
        {
            "size_bytes": 999,
            "quick_check": "ok",
            "integrity_check": "ok",
        },
    )

    snapshot = DatabaseInfoService(context, reference_db_path="").collect()
    by_name = {Path(item.path).name: item for item in snapshot.backups}

    assert by_name["broken.db"].validation_status == "Невалиден"
    assert by_name["settings_pre_test.db"].validation_status == "Ошибка метаданных"
    assert by_name["settings_pre_test.db"].metadata_error
    assert by_name["migration.db"].scope == "Настройки"
    assert by_name["migration.db"].validation_status == "Невалиден"
    assert by_name["changed.db"].validation_status == "Изменён после проверки"
    assert "фактический размер" in by_name["changed.db"].metadata_error
    assert len(snapshot.databases) == 2
    assert all(item.status == "Не найдена" for item in snapshot.databases)


def test_missing_backup_directories_return_empty_inventory(tmp_path):
    snapshot = DatabaseInfoService(
        _runtime_context(tmp_path),
        reference_db_path="",
    ).collect()

    assert snapshot.backups == ()
    assert snapshot.cycles == ()
    assert snapshot.events == ()
    assert snapshot.warnings == ()


def test_nested_settings_backup_directory_keeps_settings_scope(tmp_path):
    context = _runtime_context(tmp_path)
    context.medical_backups_root_dir = str(tmp_path / "backups")
    context.settings_backups_dir = str(tmp_path / "backups" / "settings")
    settings_backup = Path(context.settings_backups_dir) / "settings_manual_20260702_090000.db"
    _write_backup(settings_backup)

    snapshot = DatabaseInfoService(context, reference_db_path="").collect()

    assert len(snapshot.backups) == 1
    assert snapshot.backups[0].scope == "Настройки"


def test_legacy_cycle_filename_supplies_rotation_event_time(tmp_path):
    context = _runtime_context(tmp_path)
    cycle_path = tmp_path / "archiv" / "rao_journal_cycle_20260203_040506.db"
    _create_medical_db(
        cycle_path,
        cycle_started_at=int(datetime(2026, 1, 1, 8, 0).timestamp()),
        admission_date="2026-01-02 09:00:00",
    )

    snapshot = DatabaseInfoService(context, reference_db_path="").collect()
    rotation_event = next(event for event in snapshot.events if event.event_type == "Ротация")

    assert rotation_event.occurred_at == datetime(2026, 2, 3, 4, 5, 6)


def test_collection_honours_cancellation(tmp_path):
    service = DatabaseInfoService(_runtime_context(tmp_path), reference_db_path="")

    with pytest.raises(DatabaseInfoCollectionCancelled):
        service.collect(cancel_check=lambda: True)
