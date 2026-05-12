# Evidence index

| Тема | Утверждение | Файлы/классы/функции | Уверенность | Комментарий |
|---|---|---|---|---|
| DB profile | Network DB uses `journal_mode=DELETE`, `synchronous=EXTRA`, `mmap_mb=0`. | `app/sqlite_shared.py:_resolve_sqlite_profile_settings` lines 44-52, 103-107; `scripts/architecture_safety_check.py:87-102` | высокая | Env overrides принудительно сбрасываются для network. |
| DB profile | `locking_mode` не задан явно. | Search по `locking_mode` | не найдено | НЕ НАЙДЕНО В КОДЕ. |
| backup | Живой backup делается SQLite Backup API. | `app/sqlite_shared.py:backup_connection` lines 253-347 | высокая | Использует `conn.backup()`, temp validation, `os.replace`. |
| backup | Restore копирует backup в temp, но не является live backup copy. | `app/sqlite_shared.py:restore_database` lines 350-364 | высокая | Допустимый restore path из уже выбранного backup. |
| recovery | Recovery защищён `recovery.lock`, `db.lock`, active role locks. | `app/startup_db_guard.py:732-972` | высокая | Есть quarantine и valid source selection. |
| recovery | Busy/locked/unavailable не считаются corruption. | `app/startup_db_guard.py:147-171`, `1071-1083`; `data/dao/db_manager.py:771-786` | высокая | Auto-restore не запускается. |
| migrations | Миграция требует validated pre-migration backup. | `app/schema_migration_guard.py:88-224` | высокая | Regression checks cover invalid backup blocks DDL. |
| migrations | `schema_migrations` table tracks versions. | `app/unified_db_schema.py:749-756`, `1195-1205` | высокая | Includes v8-v11 markers. |
| min_client_version | Старый клиент блокируется через `client_policy.json.min_client_version`. | `app/startup_db_guard.py:227-293`; `app/schema_migration_guard.py:110-224` | высокая | Manual old-client test still recommended. |
| write queue | UI writes should use `DataService.enqueue_write`. | `services/data_service.py:76-113`; `ui/patient_bed_management/patient_form.py:287-292` | высокая | `DataService.run_write` exists but UI should not call it. |
| write queue | Local write queue retries locked/busy writes. | `app/sqlite_shared.py:828-949` | высокая | Worker thread with retryable markers. |
| write transaction | Commit path uses `FileWriteLock` + `BEGIN IMMEDIATE`. | `app/sqlite_shared.py:549-815` | высокая | Central invariant. |
| sync | `change_log` table/triggers drive UI sync. | `app/unified_db_schema.py:331-419`, `724-736`; `services/data_update_monitor.py` | высокая | `change_log` is not medical audit. |
| sync | `SyncCoordinator` classifies changed_entities into targeted refresh actions. | `services/sync_coordinator.py:6-138` | высокая | Full refresh reasons explicit. |
| snapshots | `ReadCoordinator` builds final snapshots with hash/dedup/version. | `services/read_coordinator.py:337-1651` | высокая | `_finalize_snapshot()` confirms fields. |
| snapshots | Persistent snapshot cache lives under `%LOCALAPPDATA%\RemCard\cache\patient_snapshots`. | `services/persistent_snapshot_cache.py:14-144` | высокая | Uses pickle with atomic replace and corruption removal. |
| W1a | W1a uses dedicated upcoming orders read-model, not full cards. | `ui/rem_card_sectors/sector_w1a.py:250-308`; `services/remcard_facade.py:1082-1100`; `services/order_domain_service.py:1014-1098` | высокая | Regression checks guard this. |
| W1b | W1b doctor/nurse components exist but appear placeholder. | `ui/rem_card_sectors/sector_w1b.py:10-42`; `ui/rem_card_sectors/sector_w1b_nurse.py:10-42` | средняя | Functional intent needs product confirmation. |
| UI workers | `AsyncCallThread` is QObject + Python thread. | `ui/shared/async_call.py:7-82` | высокая | Regression checks ensure not QThread. |
| UI workers | PDF/analytics/graphs use QThread workers. | `ui/shared/pdf_build_worker.py`; `ui/shared/html_pdf_worker.py`; `ui/shared/analytics_worker.py`; `ui/analytics/graphs_dialog.py:287-390` | высокая | UI updates via signals. |
| PatientForm | PatientForm opens deferred and uses pending/success/error callbacks. | `ui/patient_bed_management/management_widget.py:165-245`; `ui/patient_bed_management/patient_form.py:230-449` | высокая | Good lifecycle guards. |
| theme | Actual theme file is `settings/color_scheme/style_settings.json`. | `ui/styles/theme_storage.py:23-63`; `settings/color_scheme/style_settings.json` | высокая | User-mentioned `settings/style_settings.json` is not actual path. |
| theme | Built-in presets only `remcard_light` and `remcard_dark`. | `ui/styles/theme_presets.py:19-35` | высокая | `gray_compact` and `high_contrast` not found. |
| theme | Corrupt theme JSON is quarantined and defaults restored. | `ui/styles/theme_storage.py:71-118` | высокая | Same pattern for display settings. |
| tests | Architecture safety gate blocks dangerous UI/DB patterns. | `scripts/architecture_safety_check.py:77-160` | высокая | Narrow static checks. |
| tests | Regression suite covers DB/recovery/sync/W1a/workers/medical audit. | `scripts/regression_safety_checks.py:8538-8675` | высокая | Large safety suite. |
| tests | Network acceptance uses temp Baza, not production DB. | `scripts/network_acceptance_runner.py:1-8`, `283-405` | высокая | Creates temp root and isolated env. |
| release/build | PyInstaller builds four EXE and writes manifest/ready.ok. | `RemCard.spec:116-183`, `214-252` | высокая | `ready.ok` written last. |
| updater | Updater validates ready/manifest/exe and rollback old files on failure. | `app/update_checker.py:192-228`; `app/updater_main.py:338-351`, `405-505` | высокая | Waits active local sessions. |
| medical audit | `medical_audit_log` is separate from `change_log`. | `app/unified_db_schema.py:430-520`, `920-932`; `docs/medical_audit_log.md:3` | высокая | Audit triggers cover listed domains. |
| medical audit | `role` and `client_id` columns are not present. | `app/unified_db_schema.py:920-932` | высокая | НЕ НАЙДЕНО В КОДЕ; only `changed_by`, `operation_id`. |
| local-first/outbox | Code exists but disabled by default. | `data/dao/db_manager.py:186-190`, `855-998`; `app/durable_sql_outbox.py`; `app/local_replica_sync.py` | высокая | Do not enable without audit. |
| direct UI writes | No `sqlite3.connect` or direct write SQL found in `ui/` by inspect. | `rg` inspect; `scripts/architecture_safety_check.py` pattern | средняя | Static pattern not proof of absence of every possible dynamic write. |
| private UI access | One private helper access found in print data. | `ui/rem_card_sectors/s_print/full_report_data.py:129` | средняя | P2 technical debt, not DB write. |
