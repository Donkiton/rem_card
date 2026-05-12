# Data flows

## 1. Startup flow

```mermaid
flowchart TD
  A["run_doctor.py / run_nurse.py"] --> B["app.main.main(forced_role)"]
  B --> C["resolve BAZA_DIR"]
  C --> D["compiled? run_startup_db_guard"]
  D --> E["client_policy + quick_check + db profile"]
  E --> F["bootstrap()"]
  F --> G["DatabaseManager"]
  G --> H["ensure unified schema/migration"]
  H --> I["MainWindow + role widget"]
  I --> J["DataUpdateMonitor starts"]
```

Ключевые файлы: `run_doctor.py:14-18`, `run_nurse.py:14-18`, `app/main.py:659-850`, `app/bootstrap.py:70-89`, `data/dao/db_manager.py:175-245`.

Ошибки/guards: old client/update (`app/main.py:484-539`), single instance QLocalServer (`app/main.py:659-850`), role lock (`app/main.py:595-619`, `app/role_session_lock.py:15-307`), startup guard (`app/startup_db_guard.py:975-1125`).

## 2. DB validation flow

```text
open readonly SQLite
  → configure_connection(readonly=True)
  → PRAGMA quick_check
  → if needed PRAGMA integrity_check
  → ok / unavailable / locked / corrupt
```

Код: `run_quick_check()` (`app/sqlite_shared.py:152-168`), `run_integrity_check()` (`app/sqlite_shared.py:134-150`), `validate_sqlite_file()` (`app/sqlite_shared.py:395-423`), startup quick check (`app/startup_db_guard.py:441-484`).

Safety: busy/locked/unavailable не равны corruption (`app/startup_db_guard.py:147-171`, `1071-1083`). Confirmed quick_check failure запускает recovery только под locks (`app/startup_db_guard.py:1085-1097`).

## 3. Migration flow

```mermaid
flowchart TD
  A["DatabaseManager._init_unified_schema"] --> B["ensure_unified_schema_with_migration_backup"]
  B --> C["acquire SQLiteWriteController file lock / FileWriteLock"]
  C --> D{"is_unified_schema_ready?"}
  D -- yes --> E["sync client_policy min version, no backup"]
  D -- no --> F["SQLite Backup API validated pre-migration backup"]
  F --> G["update client_policy min_client_version"]
  G --> H["run unified schema migration in transaction"]
  H --> I["verify fastpath schema"]
```

Код: `data/dao/db_manager.py:820-832`, `app/schema_migration_guard.py:88-224`, `app/unified_db_schema.py:1541-1542`.

Ошибки: invalid backup blocks DDL (`app/schema_migration_guard.py:88-104`, regression `scripts/regression_safety_checks.py:1397-1435`); failed migration сохраняет pre-migration backup (`scripts/regression_safety_checks.py:1458-1481`).

## 4. Recovery flow

```text
startup quick_check fails as corruption/missing
  → acquire locks/recovery.lock
  → acquire archiv/db.lock
  → recheck current DB
  → if locked/unavailable: stop, no restore
  → check active other role locks
  → select newest valid backup/snapshot
  → quarantine invalid candidates
  → quarantine current corrupt DB
  → restore temp copy
  → quick_check + integrity_check
  → apply network profile
```

Код: `app/startup_db_guard.py:732-972`, `app/sqlite_shared.py:488-546`.

Safety guards: `recovery.lock`, `db.lock`, active second client protection (`app/startup_db_guard.py:732-758`), invalid backup quarantine (`564-599`), current DB quarantine (`616-646`).

## 5. Write flow

```mermaid
sequenceDiagram
  participant UI
  participant DS as DataService
  participant Q as LocalWriteQueue
  participant WC as SQLiteWriteController
  participant DAO
  participant DB as SQLite

  UI->>UI: pending state
  UI->>DS: enqueue_write(description, operation)
  DS->>Q: submit task
  Q->>WC: transaction()
  WC->>WC: FileWriteLock.acquire()
  WC->>DB: BEGIN IMMEDIATE
  WC->>DAO: execute operation
  DAO->>DB: INSERT/UPDATE/DELETE
  WC->>DB: COMMIT
  WC->>WC: release lock
  Q->>UI: success callback via Qt signal
```

Код: `services/data_service.py:76-113`, `app/sqlite_shared.py:828-949`, `app/sqlite_shared.py:732-810`, `data/dao/db_manager.py:1580-1648`.

Ошибки: rollback on exception (`app/sqlite_shared.py:797-798`), retry busy/locked in queue (`app/sqlite_shared.py:912-949`), conflict exceptions (`services/concurrency.py`, `data/dao/exceptions.py`).

## 6. Read/snapshot flow

```text
UI requests current context
  → ReadCoordinator builds SnapshotContext
  → check in-memory/persistent cache
  → build partial/full snapshot through RemCardService
  → add version/content_hash/dedup_signature
  → UI stale guard checks request_id/admission/date/context
  → UI applies only changed payload
```

Код: `services/read_coordinator.py:39-115`, `412-605`, `607-1079`, `1607-1651`; `services/remcard_facade.py:204-536`; UI apply guards `ui/doctor_view/doctor_remcard_widget.py:627-680`, `ui/nurse_view/nurse_main_widget.py:637-704`.

Ошибки/guards: stale request discard, unchanged snapshot dedupe, persistent cache corrupt file remove (`services/persistent_snapshot_cache.py:67-102`).

## 7. Sync flow between doctor and nurse

```mermaid
flowchart TD
  A["Client A commit"] --> B["SQLite triggers write change_log"]
  B --> C["Client B DataUpdateMonitor polls get_latest_change_id"]
  C --> D["fetch_changes_since"]
  D --> E["payload: changed_entities, admission_ids, forced/gap/reason"]
  E --> F["SyncCoordinator.classify"]
  F --> G["targeted UI refresh or full refresh"]
```

Код: `app/unified_db_schema.py:331-419`, `services/data_update_monitor.py:67-189`, `services/sync_coordinator.py:60-96`, `services/data_service.py:164-168`.

Ошибки: cursor moved backwards/empty change rows set forced gap (`services/data_update_monitor.py:89-118`); full refresh reasons in `SyncCoordinator.FULL_REFRESH_REASONS` (`services/sync_coordinator.py:7-13`).

## 8. W1a upcoming orders flow

```text
SectorW1a.refresh_data()
  → AsyncCallThread(loader)
  → RemCardService.build_w1a_upcoming_orders_snapshot()
  → OrderDomainService.get_upcoming_orders_across_active_admissions()
  → SQL over active beds/orders/administrations/status
  → content_hash + change_id
  → SectorW1a applies if content changed
```

Код: `ui/rem_card_sectors/sector_w1a.py:250-308`, `services/remcard_facade.py:1068-1100`, `services/order_domain_service.py:1014-1098`.

Safety: не открывает все карточки; regression check ищет W1a read-model and forbids full-card markers (`scripts/regression_safety_checks.py:7239-7359`).

## 9. PatientForm open/save flow

```mermaid
sequenceDiagram
  participant W as PatientBedManagementWidget
  participant F as PatientForm
  participant S as PatientBedManagementService
  participant DS as DataService

  W->>W: _open_patient_card_by_number guard
  W->>F: QTimer.singleShot -> dialog.open()
  F->>F: validate input
  F->>F: _begin_write_pending
  F->>S: enqueue_write(description, operation)
  S->>DS: enqueue_write
  DS-->>F: success/error callback
  F->>F: success accept / error warning
  W->>W: refresh bed statuses after accepted
```

Код: `ui/patient_bed_management/management_widget.py:165-245`, `ui/patient_bed_management/patient_form.py:230-387`.

Guards: `_is_closing`, `_opening_patient_form`, `_active_patient_form`, stale dialog check, reject ignored while pending (`management_widget.py:64-68`, `207-235`; `patient_form.py:404-449`).

## 10. PDF/analytics/graph worker flow

```text
UI click report/graphs
  → DataCollectorWorker / AnalyticsWorker
  → build data/html off UI thread
  → PdfBuildWorker / HtmlPdfWorker
  → completed signal
  → open PDF / update preview
```

Код: `ui/shared/report_controller.py:43-130`, `144-240`; `ui/shared/pdf_build_worker.py:10-29`; `ui/shared/analytics_worker.py:6-32`; `ui/shared/html_pdf_worker.py:9-40`; `ui/analytics/graphs_dialog.py:287-390`; `services/analytics/graphs_service.py:27-100`.

Guards: worker already running checks, `_closing` checks, cancel on close (`ui/analytics/graphs_dialog.py:287-390`).

## 11. Theme load/apply/save flow

```text
ThemeManager()
  → ThemeStorage.load()
  → if missing/corrupt: default + quarantine broken
  → tokens_for_role()
  → build_global_style()
  → QApplication.setStyleSheet()
  → optional ThemeSettingsDialog save/reset
```

Код: `ui/styles/theme_storage.py:52-118`, `ui/styles/theme_manager.py:17-133`, `ui/styles/qss_builder.py:7-120`, `ui/styles/theme_settings_dialog.py:100-132`, `1085-1105`.

Ошибки: bad JSON -> `*.broken` and defaults (`theme_storage.py:78-118`).

## 12. Shutdown flow

```text
MainWindow.closeEvent
  → role widget shutdown
  → snapshot workers/timers/W1a/orders shutdown
  → DataService.set_shutting_down + shutdown monitor/queue
  → DatabaseManager.close: stop background threads + shutdown backup
  → release role lock
  → optional update check/launch
```

Код: `ui/main_window.py:624-660`, `ui/doctor_view/doctor_remcard_widget.py:2717-2734`, `ui/nurse_view/nurse_main_widget.py:1946-1963`, `services/data_service.py:146-158`, `data/dao/db_manager.py:1872-1895`, `app/main.py:841-850`.

Safety: no late callbacks after `_is_closing`; queue shutdown timeout; role lock release.
