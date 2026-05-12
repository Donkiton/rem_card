# Current status and open items

## 1. Что уже закрыто

DB safety:

- Network profile fixed to `DELETE/EXTRA/mmap=0` (`app/sqlite_shared.py:44-52`, `103-107`).
- `FileWriteLock`, `SQLiteWriteController`, `BEGIN IMMEDIATE` present (`app/sqlite_shared.py:549-815`).
- Backup API uses SQLite `backup()` and validation (`app/sqlite_shared.py:253-347`).
- Startup recovery guarded by `recovery.lock`, `db.lock`, active second client protection (`app/startup_db_guard.py:732-972`).
- Migration guard creates validated pre-migration backup (`app/schema_migration_guard.py:88-224`).
- `client_policy.json.min_client_version` exists and blocks old client (`app/startup_db_guard.py:227-293`).

Write queue/conflicts:

- `DataService.enqueue_write` + `LocalWriteQueue` implemented (`services/data_service.py:76-113`, `app/sqlite_shared.py:828-949`).
- Optimistic lock exists for beds/admissions/vitals/fluids/orders/admin/status/IVL/diet (`app/unified_db_schema.py:40-99`; DAO/services refs in `06_WRITE_PATH_AND_CONFLICTS.md`).

Sync/read:

- `change_log` triggers and `DataUpdateMonitor` polling implemented (`app/unified_db_schema.py:331-419`, `services/data_update_monitor.py:12-189`).
- `SyncCoordinator` targeted refresh implemented (`services/sync_coordinator.py:6-138`).
- `ReadCoordinator` partial snapshots/cache/dedup implemented (`services/read_coordinator.py:337-1651`).

Performance A-D style items:

- lazy ChartWidget init present (`ui/doctor_view/doctor_remcard_widget.py:1871-1885`, nurse `1399-1413`);
- chart snapshot dedupe present (`doctor_remcard_widget.py:1887-1908`, nurse `1415-1425`);
- repeated card snapshot dedupe present (`doctor_remcard_widget.py:661-680`, nurse `669-688`);
- tab-level orders cache/delta present (`services/read_coordinator.py:838-1079`);
- quick_check TTL and connection_profile retry present (`data/dao/db_manager.py:107-115`, `348-383`).

PatientForm lifecycle:

- deferred open and active dialog guards present (`ui/patient_bed_management/management_widget.py:165-235`);
- pending/success/error save path present (`ui/patient_bed_management/patient_form.py:278-387`).

Theme system:

- `ThemeStorage`, `ThemeManager`, `ThemePresets`, `qss_builder` present (`ui/styles/*`);
- corrupt JSON quarantine present (`ui/styles/theme_storage.py:71-118`);
- display settings W1a/W1b present (`ui/shared/display_settings_storage.py:115-120`, `261-274`).

W1a/W1b:

- W1a upcoming orders read-model implemented (`ui/rem_card_sectors/sector_w1a.py:250-308`; `services/order_domain_service.py:1014-1098`);
- W1a display sleep/targeted checks in regression (`scripts/regression_safety_checks.py:7018-7359`);
- W1b components exist for doctor/nurse (`ui/rem_card_sectors/sector_w1b.py`, `sector_w1b_nurse.py`).

Medical audit:

- `medical_audit_log` table/triggers implemented (`app/unified_db_schema.py:430-520`, `920-932`, `1412-1540`).

## 2. Что вероятно закрыто, но требует ручной проверки

- Multi-client real network behavior: code/gates exist, but checkpoint не запускал `network_acceptance_runner.py`.
- Performance targets: code optimizations present, but актуальные цифры не измерены.
- Updater in production folder: code present, but manual rollout rehearsal нужен.
- Old client block: code present, standalone test script НЕ НАЙДЕН; manual test нужен.
- Archive readonly external DB: code raises on writes, but manual archive scenario нужен.
- Graph/PDF workers: code present, but output/render quality требует ручной проверки.

## 3. Что открыто

P1:

- Не включать local-first/outbox без отдельного аудита. Код есть, но disabled by env (`data/dao/db_manager.py:186-190`, `855-998`).
- `medical_audit_log` не содержит `role`/`client_id`. Если это требование аудита, нужна новая задача (`app/unified_db_schema.py:920-932`).

P2:

- `WriteCoordinator` class НЕ НАЙДЕН; будущая документация должна не ссылаться на него как на реальный класс.
- W1b выглядит placeholder; нужна продуктовая проверка ожиданий (`ui/rem_card_sectors/sector_w1b.py:10-42`, `sector_w1b_nurse.py:10-42`).
- `gray_compact`/`high_contrast` темы НЕ НАЙДЕНЫ; реализованы только `remcard_light`, `remcard_dark` (`ui/styles/theme_presets.py:19-35`).
- Private helper access in UI print data: `oral_service._calculate_totals` (`ui/rem_card_sectors/s_print/full_report_data.py:129`).

## 4. Что опасно трогать

- `app/sqlite_shared.py` network profile, lock, transaction, backup.
- `app/startup_db_guard.py` recovery classification/locks.
- `app/schema_migration_guard.py` pre-migration backup.
- `app/unified_db_schema.py` triggers/revisions/migrations.
- `data/dao/db_manager.py` write/read/recovery/quick_check TTL/local-first disabled logic.
- `services/read_coordinator.py` stale/dedup/cache logic.
- `services/sync_coordinator.py` classification flags.
- `ui/*_remcard_widget.py` snapshot apply guards.
- `ui/patient_bed_management/patient_form.py` pending/callback lifecycle.
- `app/updater_main.py` update lock/rollback.

## 5. Что можно оптимизировать только по метрикам

- Startup quick_check TTL/retry intervals.
- `ReadCoordinator` cache sizes (`READ_CACHE_MAX_PATIENTS`, `READ_CACHE_MAX_TABS`, `READ_MAX_CONCURRENT_LOADS` in `services/read_coordinator.py:20-33`).
- Orders delta fallback thresholds (`READ_ORDERS_DELTA_STALE_THRESHOLD_SEC`, `services/read_coordinator.py:31-33`).
- Chart redraw behavior.
- W1a polling/refresh frequency.
- Backup rotation intervals.
- Local replica/outbox, если когда-либо включать.

## 6. Что отложено

- Local-first/outbox production enablement.
- Rich W1b behavior, если требуется.
- Additional themes beyond light/dark.
- Separate role/client_id medical audit fields.
- Standalone old-client block test.
- Full automated UI smoke suite.

## 7. Что не делать

- Не включать WAL.
- Не снижать `synchronous`.
- Не включать mmap для network DB.
- Не копировать live DB как backup через `shutil.copy`.
- Не писать из UI напрямую в SQLite.
- Не обходить `DataService.enqueue_write`.
- Не показывать success до commit callback.
- Не строить W1/W1a через full snapshots всех пациентов.
- Не включать local-first/outbox без отдельного threat/safety audit.
- Не ослаблять regression/safety gates ради прохождения build.
