# Write path and conflicts

## 1. Единый путь записи

Целевой путь записи:

```text
UI action
  → pending state
  → DataService.enqueue_write()
  → LocalWriteQueue worker
  → DatabaseManager.remcard_transaction()/execute_remcard()
  → SQLiteWriteController.transaction()
  → FileWriteLock
  → BEGIN IMMEDIATE
  → DAO SQL
  → COMMIT
  → success/error callback
```

Ключевые файлы: `services/data_service.py`, `app/sqlite_shared.py`, `data/dao/db_manager.py`.

## 2. DataService.enqueue_write

`DataService.enqueue_write()` (`services/data_service.py:76-113`):

- reject during shutdown (`83-92`);
- wraps success callback: emit `write_finished`, request immediate refresh, dispatch UI callback (`94-100`);
- wraps error callback: emit `write_failed`, dispatch error callback (`102-105`);
- submits to queue (`107-112`).

Callbacks доставляются через Qt queued signals + `QTimer.singleShot(0)` (`services/data_service.py:115-141`).

## 3. LocalWriteQueue

`LocalWriteQueue` (`app/sqlite_shared.py:828-949`):

- daemon worker thread `SQLiteLocalWriteQueue` (`838`);
- `submit()` creates task and increments pending counter (`841-868`);
- `shutdown()` waits for queue/thread (`869-889`);
- worker executes task, then success/error callback (`890-939`);
- retryable markers: `locked`, `busy`, `database is locked`, `database table is locked`, etc. (`940-949`).

## 4. SQLiteWriteController

`SQLiteWriteController.transaction()` (`app/sqlite_shared.py:732-810`):

- gets connection_guard;
- supports nested transaction;
- acquires `FileWriteLock`;
- `BEGIN IMMEDIATE`;
- yields cursor;
- `COMMIT`;
- on exception `ROLLBACK`;
- releases lock.

## 5. FileWriteLock

`FileWriteLock` (`app/sqlite_shared.py:549-686`) uses JSON file, atomic create, stale handling, thread reentrancy. It protects cross-process writes/backup/recovery windows.

## 6. DAO

DAO layer performs SQL through `DatabaseManager`:

- `execute_remcard()` for single write (`data/dao/db_manager.py:1613-1648`);
- `remcard_transaction()` for grouped writes (`data/dao/db_manager.py:1580-1607`);
- DAO examples: `data/dao/vitals_dao.py`, `fluids_dao.py`, `diet_dao.py`, `patient_status_dao.py`, `orders_dao.py`.

## 7. Pending/success/error в UI

Example `PatientForm`:

- pending starts before enqueue (`ui/patient_bed_management/patient_form.py:278-292`);
- controls disabled and button `СОХРАНЕНИЕ...` (`340-350`);
- success callback schedules `_on_write_success`, then `accept()` (`297-373`);
- error callback schedules `_on_write_error`, restores controls and warning (`320-387`);
- reject ignored while write pending (`404-412`).

Move patient:

- `_begin_move_pending()` disables bed widgets (`ui/patient_bed_management/management_widget.py:329-348`);
- success/error refreshes statuses and restores pending (`310-338`).

## 8. Ошибки записи

Основные error classes:

- `DataConflictError` (`services/concurrency.py:4-20`);
- `OptimisticLockError` (`data/dao/exceptions.py:1-2`);
- SQLite busy/locked handled by queue/controller retry (`app/sqlite_shared.py:912-949`).

UI reaction:

- conflict/error should show warning and refresh current data;
- orders widgets often call `request_refresh(force=True)` after error or stale state (`ui/doctor_view/orders_widget.py:416`, `495`, `1429`, nurse analogous).

## 9. Retry

Retry layers:

- SQLite busy_timeout PRAGMA (`app/sqlite_shared.py:111-132`);
- `SQLiteWriteController` retry settings (`app/sqlite_shared.py:688-704`);
- `LocalWriteQueue` retry on locked/busy (`app/sqlite_shared.py:912-949`);
- connection profile lock retry/wait (`data/dao/db_manager.py:348-383`).

## 10. Lock timeout

Connection profile lock timeout default 12s: `REMCARD_CONNECTION_PROFILE_LOCK_TIMEOUT_SEC` (`data/dao/db_manager.py:95-106`). Recovery lock stale/wait constants in `app/startup_db_guard.py:30-34`.

Timeout on startup/recovery produces user-facing messages, not silent restore (`app/startup_db_guard.py:1098-1110`).

## 11. DataConflictError

`DATA_CONFLICT_MESSAGE = "Данные изменены другим рабочим местом. Обновите карточку."` (`services/concurrency.py:1`).

`assert_revision_matches()` normalizes expected revision and raises `DataConflictError` if current != expected (`services/concurrency.py:14-20`).

## 12. Optimistic lock/revision/version

Pattern:

- UI reads current `revision`/`version`;
- passes as `expected_revision`/`expected_version`;
- DAO/service adds `WHERE revision=?` or checks current row;
- `rowcount=0` or mismatch raises conflict;
- UI refreshes.

Domains:

- beds/admissions/patient form/move (`services/patient_bed_management/service.py:188-333`);
- vitals (`data/dao/vitals_dao.py:14-75`, `237-245`);
- fluids/balance (`data/dao/fluids_dao.py:26-56`);
- orders (`services/order_service.py:63-121`, `378-674`);
- administrations (`services/order_domain_service.py:877-934`);
- status/events/admissions outcome (`data/dao/patient_status_dao.py:501-1099`);
- IVL/clinical events (`data/dao/ventilation_dao.py:269-281`);
- diet (`data/dao/diet_dao.py:71-345`).

## 13. Какие таблицы защищены revision/version

Из `app/unified_db_schema.py:40-99`:

- `admissions.revision`;
- `beds.revision`;
- `ivl_episodes.revision`;
- `clinical_events.revision`;
- `vitals.revision`;
- `fluids.revision`;
- `orders.revision`;
- `administrations.version`;
- `patient_status_events.revision`;
- `diet_templates.version`;
- `diet_plan.version`;
- `oral_intake_events.version`.

## 14. Что делать при rowcount=0

Не повторять blind overwrite. Treat as conflict:

- raise `DataConflictError` or `OptimisticLockError`;
- UI removes pending/error state;
- show message "данные изменены другим рабочим местом";
- refresh current snapshot/read-model.

Examples: `data/dao/diet_dao.py:99-111`, `182-214`, `308-345`; `services/order_domain_service.py:899-934`; `data/dao/fluids_dao.py:45-56`.

## 15. Как UI должен rollback/refresh

Rollback БД делает transaction layer. UI rollback means:

- remove optimistic/pending visual state;
- refresh current model/snapshot;
- do not display saved state.

Examples:

- `PatientForm._on_write_error()` restores controls and warns (`ui/patient_bed_management/patient_form.py:375-387`);
- orders stale snapshot queues forced reload (`ui/doctor_view/orders_widget.py:1007-1033`);
- move patient error refreshes bed statuses (`ui/patient_bed_management/management_widget.py:322-338`).

## 16. Где нельзя писать напрямую

Нельзя из:

- `ui/`;
- worker callbacks that operate on widgets;
- W1a direct SQL/click simulation;
- archive read-only services.

`ArchiveReadOnlyDatabaseManager` explicitly raises on writes (`services/archive_readonly_service.py:132-141`).

## 17. Найденные прямые write paths, если ещё есть

Inspect findings:

- `sqlite3.connect` in `ui/`: НЕ НАЙДЕНО.
- Direct write SQL in `ui/`: НЕ НАЙДЕНО через static pattern.
- `DataService.run_write` in `ui/`: НЕ НАЙДЕНО.
- Potential private helper access: `ui/rem_card_sectors/s_print/full_report_data.py:129` calls `oral_service._calculate_totals(...)`. Это не DB write, но private access risk.
- Service layer `services/ventilation_service.py:640` and `services/remcard_facade.py:1330` use `data_service.run_write()` internally. Это service layer, не UI; новые UI writes всё равно должны идти через `enqueue_write`.

## 18. Что проверить architecture_safety_check

`scripts/architecture_safety_check.py` проверяет:

- UI forbidden patterns (`scripts/architecture_safety_check.py:77-84`);
- network profile (`87-102`);
- no live DB copy bypass (`105-112`);
- required safety artifacts (`115-123`);
- migration uses Backup API (`126-130`);
- recovery lock guard (`133-137`).

Команда:

```powershell
python scripts\architecture_safety_check.py
```
