# Future assistant context

## Always remember

Проект `Рем Карта` — PySide6 desktop app для врача, медсестры и оперблока. Работает с одной SQLite БД в сетевой папке: `BAZA_DIR\archiv\rao_journal.db`. Главная цель архитектуры — сохранность БД и честное UI-состояние, скорость вторична.

Реальные release entrypoints: `run_doctor.py`, `run_nurse.py`, `run_operblock_emergency.py`, `run_operblock_planned.py`, `run_path_setup.py`, `run_updater.py`. `run_operblock.py` остается dev entrypoint общего оперблока. Главные файлы: `app/main.py`, `app/roles.py`, `app/sqlite_shared.py`, `data/dao/db_manager.py`, `services/data_service.py`, `services/read_coordinator.py`, `services/sync_coordinator.py`, `ui/doctor_view/doctor_remcard_widget.py`, `ui/nurse_view/nurse_main_widget.py`, `ui/operblock_view/operblock_main_widget.py`.

## Never suggest

- WAL для сетевой БД.
- `synchronous` ниже `EXTRA`.
- `mmap_size` выше 0 для network profile.
- Backup живой БД через `shutil.copy*`.
- UI direct `sqlite3.connect` или write SQL.
- UI direct `DataService.run_write`.
- Success UI до commit callback.
- Full snapshot всех пациентов на старте.
- W1a через открытие всех карточек.
- Local-first/outbox enablement без отдельного аудита.

## Safe write path

```text
UI → DataService.enqueue_write → LocalWriteQueue
→ SQLiteWriteController.transaction → FileWriteLock
→ BEGIN IMMEDIATE → DAO → COMMIT → success/error callback
```

Код: `services/data_service.py:76-113`, `app/sqlite_shared.py:549-949`, `data/dao/db_manager.py:1580-1648`.

Conflicts: `DataConflictError` (`services/concurrency.py`), `OptimisticLockError` (`data/dao/exceptions.py`). Tables with revision/version: admissions, beds, ivl_episodes, clinical_events, vitals, fluids, orders, administrations, patient_status_events, diet_templates, diet_plan, oral_intake_events (`app/unified_db_schema.py:40-99`).

## Safe read/sync path

```text
SQLite triggers → change_log → DataUpdateMonitor
→ SyncCoordinator.classify → ReadCoordinator snapshots/cache
→ UI targeted refresh
```

Код: `app/unified_db_schema.py:331-419`, `services/data_update_monitor.py`, `services/sync_coordinator.py`, `services/read_coordinator.py`.

Snapshot carries `version/content_hash/dedup_signature/context_hash`. UI must discard stale callbacks when patient/date/context changed.

## DB safety invariants

Network profile is fixed: `journal_mode=DELETE`, `synchronous=EXTRA`, `mmap_mb=0`, busy_timeout default 10000 (`app/sqlite_shared.py:44-52`). Startup guard uses `client_policy.json`, quick_check, `recovery.lock`, `db.lock`, active role locks (`app/startup_db_guard.py`). Migration requires validated pre-migration backup (`app/schema_migration_guard.py`).

## UI/threading invariants

Workers do heavy work; UI updates through signals/callbacks. `AsyncCallThread` is QObject + Python thread, not QThread (`ui/shared/async_call.py`). PDF/analytics workers are QThread (`ui/shared/pdf_build_worker.py`, `html_pdf_worker.py`, `analytics_worker.py`). Guard late callbacks with `_is_closing`, `_closing`, request ids, context keys.

PatientForm opens deferred with `QTimer.singleShot(0)` and `dialog.open()`, not blocking callback (`ui/patient_bed_management/management_widget.py:165-199`). Pending save disables controls until commit callback (`ui/patient_bed_management/patient_form.py:278-387`).

## Performance status

Implemented: lazy ChartWidget, chart snapshot dedupe, repeated snapshot apply dedupe, ReadCoordinator partial snapshots and orders tab cache/delta, startup quick_check TTL, connection_profile lock retry. Current measured numbers were not collected in checkpoint; run benchmarks before performance claims.

## Theme status

Actual style path: `settings/color_scheme/style_settings.json` (`ui/styles/theme_storage.py:23-24`). Built-in presets only `remcard_light` and `remcard_dark`; `gray_compact`/`high_contrast` not found. Corrupt JSON is quarantined as `*.broken` and defaults are saved.

Display settings path: `settings/display_settings/display_settings.json`; includes W1a/W1b enabled flags (`ui/shared/display_settings_storage.py`).

Файлы фонов: загруженные пользователем изображения лежат в `<BAZA_DIR>\settings\backgrounds`, а не в `icon`. `ui/shared/background_settings.py` хранит в settings DB только безопасные имена файлов, сначала ищет общую папку, умеет переносить старые файлы из `icon` в `settings\backgrounds` и восстанавливать отсутствующие файлы из `ui_backgrounds.image_blob` после release snapshot/update. BLOB-страховка для фоновых файлов ограничена 32 МБ. `icon/fon.png` оставлять только как встроенный стандартный фон.

## W1/W1a/W1b status

W1 is bed/patient selection. W1a is upcoming orders panel and uses dedicated read-model: `SectorW1a` → `build_w1a_upcoming_orders_snapshot()` → `get_upcoming_orders_across_active_admissions()` (`ui/rem_card_sectors/sector_w1a.py`, `services/remcard_facade.py:1082-1100`, `services/order_domain_service.py:1014-1098`). Do not rebuild via full cards.

W1b doctor/nurse components exist but look like placeholders (`ui/rem_card_sectors/sector_w1b.py`, `sector_w1b_nurse.py`).

## Before recommending changes, check

- `git status --short --branch`.
- Whether task touches DB profile, migration, recovery, backup, sync, write queue, or UI snapshot guards.
- Existing regression check for that behavior in `scripts/regression_safety_checks.py`.
- Whether a similar helper already exists in `ReadCoordinator`, `DataService`, domain service, or DAO.
- Whether local-first/outbox is accidentally being enabled.

## Standard gates

```powershell
git diff --check
python -m compileall app data services ui scripts
python scripts\architecture_safety_check.py
python scripts\regression_safety_checks.py
python scripts\code_quality_checks.py
python scripts\style_audit_check.py
python scripts\network_acceptance_runner.py --operations 24 --benchmark-clicks 3
python scripts\validate_backups.py --max-files 20 --move-invalid
python scripts\restore_drill.py --max-files 20 --cleanup-restored
```

Domain-specific gates: for emergency-mode changes run `python scripts\emergency_db_acceptance_runner.py`; for operblock offline/local changes run `python scripts\operblock_offline_acceptance_runner.py`.

For doc-only changes, do not run heavy gates unless needed; at minimum verify changed files and `git diff --check`.

## Current open items

- Local-first/outbox disabled by default; do not enable without audit.
- `medical_audit_log` lacks `role`/`client_id`; only `changed_by` and `operation_id` are present.
- `WriteCoordinator` class not found; use real terms `LocalWriteQueue` and `SQLiteWriteController`.
- W1b behavior needs product verification.
- Additional themes beyond light/dark not present.
- Potential private helper use: `ui/rem_card_sectors/s_print/full_report_data.py:129`.
