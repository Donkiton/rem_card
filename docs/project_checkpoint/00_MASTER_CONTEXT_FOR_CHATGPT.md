# Рем Карта — master context

Документ собран по текущему коду репозитория `C:\Test\rem_card` на 2026-05-12. Если утверждение не подтверждено кодом, оно помечено как `НЕ НАЙДЕНО В КОДЕ`, `ТРЕБУЕТ УТОЧНЕНИЯ` или `ПРЕДПОЛОЖЕНИЕ, НУЖНА ПРОВЕРКА`.

Исходное состояние git перед созданием checkpoint:

- Ветка: `master` (`git branch --show-current`).
- HEAD: `cf158d24a8368d80c0522e9744fb48f3a19bf2b1` (`git rev-parse HEAD`).
- `git status --short --branch`: `## master...origin/master [ahead 1]`.
- Незакоммиченных изменений до начала не было.

## 1. Что это за программа

`Рем Карта` — desktop-приложение на PySide6 для ведения карты пациента в отделении реанимации/интенсивной терапии. Основные пользователи: врач и медсестра. В коде роли запускаются отдельными entrypoint-файлами: `run_doctor.py:14-18` вызывает `main(forced_role="doctor")`, `run_nurse.py:14-18` вызывает `main(forced_role="nurse")`.

Карта пациента — набор данных активной госпитализации: пациент, койка, диагноз, витальные показатели, назначения и выполнения, баланс жидкости, питание, ИВЛ, события/статус, печать и архив. Схема таблиц задаётся в `app/unified_db_schema.py`: `patients`, `admissions`, `beds`, `vitals`, `fluids`, `orders`, `administrations`, `patient_status_events`, `ivl_episodes`, `clinical_events`, `diet_plan`, `oral_intake_events` и другие (`app/unified_db_schema.py:12-38`, `533-981`).

W1 в текущем коде — режим выбора коек/пациентов, а не открытая карта. Врач и медсестра видят W1-список коек через `BedsSelectionWidget`/`NurseBedsSelectionWidget` (`ui/shared/remcard_layout.py:222-236`, `ui/nurse_view/nurse_remcard_layout.py:232-237`). W1a — левый верхний сектор ближайших назначений, реализован как `SectorW1a` (`ui/rem_card_sectors/sector_w1a.py:44`). W1b — нижний левый сектор W1: у врача `SectorW1b`, у медсестры `SectorW1bNurse` (`ui/shared/layout_components.py:66-71`, `148-149`). `SectorW1b`/`SectorW1bNurse` сейчас выглядят как placeholder-секторы (`ui/rem_card_sectors/sector_w1b.py:10-42`, `ui/rem_card_sectors/sector_w1b_nurse.py:10-42`).

Сектора карты — визуальные блоки `sector_1a`, `sector_1b`, `sector_2a`, `sector_2b`, `sector_2v`, `sector_3a`, `sector_3b`, `sector_4a`, `sector_5`, `sector_6`, `sector_7*`, `sector_8`. Их создаёт `SectorFactory.create_all_sectors()` (`ui/shared/layout_components.py:40-149`) и раскладывают `RemCardLayoutManager`/`NurseRemCardLayoutManager` (`ui/shared/remcard_layout.py`, `ui/nurse_view/nurse_remcard_layout.py`).

Основные операции:

- открыть карту пациента из W1 или архива (`DoctorRemCardWidget`, `NurseMainWidget`);
- создать/обновить пациента и госпитализацию через `PatientForm` (`ui/patient_bed_management/patient_form.py:230-292`);
- переместить пациента между койками (`PatientBedManagementWidget.move_patient`, `ui/patient_bed_management/management_widget.py:268-337`);
- вести витальные, назначения, выполнения, баланс, питание, ИВЛ, статус;
- строить PDF/аналитику/графики (`ui/shared/report_controller.py`, `ui/analytics/graphs_dialog.py`, `services/analytics/graphs_service.py`);
- запускать обновление программы через `RemCardUpdater.exe` (`app/updater_main.py`).

## 2. Главный архитектурный принцип проекта

Главная схема работы: `ПК врача ↔ сетевая папка Baza_rao3_jurnal со SQLite ↔ ПК медсестры`.

Фактическая рабочая БД: `BAZA_DIR\archiv\rao_journal.db`. Путь задаётся в `app/paths.py:122-166`: `BAZA_DIR`, `ARCHIV_DIR`, `JOURNAL_DB_PATH`, `REMCARD_DB_PATH`, `DB_LOCK_PATH`, `RECOVERY_LOCK_PATH`. В dev-режиме база обычно лежит относительно проекта, в compiled-режиме путь выбирается через `app/runtime_paths.py` и `run_path_setup.py`.

Приоритет проекта — сохранность БД, надёжный commit и честное состояние UI. Это видно по:

- network-safe SQLite profile: `journal_mode=DELETE`, `synchronous=EXTRA`, `mmap_mb=0` (`app/sqlite_shared.py:44-52`, `103-107`);
- централизованной записи через `FileWriteLock`, `SQLiteWriteController`, `BEGIN IMMEDIATE`, `COMMIT` (`app/sqlite_shared.py:549-815`);
- очереди записи `LocalWriteQueue` (`app/sqlite_shared.py:828-949`);
- startup/recovery guard с `recovery.lock`, `db.lock`, quick_check и запретом auto-restore при lock/unavailable без подтверждённой corruption (`app/startup_db_guard.py:975-1125`);
- архитектурным gate `scripts/architecture_safety_check.py`.

Скорость важна, но не ценой safety: локальная реплика/outbox существуют, но выключены по умолчанию env-переменными `REMCARD_LOCAL_FIRST_SYNC=0`, `REMCARD_LOCAL_OUTBOX_SYNC=0` (`data/dao/db_manager.py:186-190`, `855-876`, `970-998`).

## 3. Самые важные красные линии

- Не включать WAL для сетевой БД: network profile принудительно держит `DELETE`, а `checkpoint_wal()` логирует skip (`app/sqlite_shared.py:44-52`, `103-107`; `data/dao/db_manager.py:1869-1870`).
- Не отключать `synchronous=EXTRA`: network override сбрасывается обратно на `EXTRA` (`app/sqlite_shared.py:103-107`).
- Не менять `journal_mode=DELETE` без отдельного доказанного аудита сетевой среды (`app/sqlite_shared.py:44-52`; `docs/db_safety_contract.md:9-14`).
- Не использовать простой file copy для живой БД вместо SQLite Backup API: `backup_connection()` использует `conn.backup()` и validation (`app/sqlite_shared.py:253-347`). Restore copy допустим только из уже выбранного backup и с проверками (`app/sqlite_shared.py:350-364`).
- Не обходить `FileWriteLock` / `SQLiteWriteController` / `DataService.enqueue_write`: они держат межпроцессную блокировку и commit path (`app/sqlite_shared.py:549-815`; `services/data_service.py:76-113`).
- Не писать из UI напрямую в SQLite: `scripts/architecture_safety_check.py:77-84` блокирует `sqlite3.connect`, write SQL и `run_write` в `ui/`.
- Не показывать "сохранено" до commit: `PatientForm` ставит pending до `enqueue_write`, а закрывает форму только в success callback (`ui/patient_bed_management/patient_form.py:278-292`, `340-373`).
- Не делать full snapshot всех пациентов на старте: чтение строится по выбранному пациенту/сектору через `ReadCoordinator`, а W1a имеет отдельный read-model (`services/read_coordinator.py`, `services/order_domain_service.py:1014-1098`).
- Не строить W1a через открытие всех карточек: W1a вызывает `build_w1a_upcoming_orders_snapshot()` (`ui/rem_card_sectors/sector_w1a.py:250-256`), а сервис читает upcoming orders одним SQL/read-model (`services/order_domain_service.py:1014-1098`).
- Не имитировать UI-клик из W1a: W1a должен идти через общий service/use-case/write-command. В коде W1a работает со snapshot и ревизиями (`ui/rem_card_sectors/sector_w1a.py:650-660`); прямое имитирование клика НЕ НАЙДЕНО В КОДЕ.
- Не включать local-first/outbox без отдельного аудита: код есть, но выключен по умолчанию (`data/dao/db_manager.py:186-190`); `docs/local_first_sync_plan.md` описывает план/статус, но это не стандартный режим.

## 4. Архитектура одним взглядом

Запись:

```text
UI
↓
controllers/widgets/use-cases
↓
DataService.enqueue_write / domain services
↓
LocalWriteQueue
↓
SQLiteWriteController.transaction
↓
FileWriteLock + BEGIN IMMEDIATE
↓
DAO / SQL
↓
SQLite в сетевой папке
```

Реальные классы/файлы: `DataService` (`services/data_service.py:11-158`), `LocalWriteQueue` (`app/sqlite_shared.py:828-949`), `SQLiteWriteController` (`app/sqlite_shared.py:688-815`), `FileWriteLock` (`app/sqlite_shared.py:549-686`), `DatabaseManager.remcard_transaction()` (`data/dao/db_manager.py:1580-1607`). Класс с именем `WriteCoordinator` НЕ НАЙДЕН В КОДЕ.

Чтение/sync:

```text
SQLite
↓
change_log + triggers
↓
DataUpdateMonitor polling
↓
SyncCoordinator.classify
↓
ReadCoordinator / partial snapshots / tab cache
↓
UI widgets
```

Реальные классы/файлы: triggers `change_log` (`app/unified_db_schema.py:331-419`, `724-736`), `DataUpdateMonitor` (`services/data_update_monitor.py:12-189`), `SyncCoordinator` (`services/sync_coordinator.py:6-138`), `ReadCoordinator` (`services/read_coordinator.py:337-1651`).

## 5. Как работает запуск приложения

1. Entry point выбирает роль: `run_doctor.py`, `run_nurse.py`, `run_path_setup.py`, `run_updater.py`.
2. `_main_impl()` в `app/main.py:659-850` готовит Qt, роль, startup guard, single-instance и role lock.
3. Путь к БД определяется в `app/paths.py`: `BAZA_DIR` через `REMCARD_BAZA_DIR`, compiled `resolve_baza_dir()` или dev fallback (`app/paths.py:122-131`).
4. В compiled doctor/nurse запускается `run_startup_db_guard()` (`app/main.py:484-539`; `app/startup_db_guard.py:975-1125`).
5. Startup guard создаёт/читает `client_policy.json`, проверяет `min_client_version`, required profile и `wal_allowed_on_shared_db` (`app/startup_db_guard.py:227-293`).
6. `quick_check` делается readonly с retry при busy/locked (`app/startup_db_guard.py:441-484`). Если quick_check OK, под `db.lock` применяется network-safe profile (`app/startup_db_guard.py:487-535`).
7. `DatabaseManager.__init__()` открывает центральную connection, проверяет quick_check TTL, запускает schema migration, monitor и background tasks (`data/dao/db_manager.py:175-245`).
8. Quick_check TTL-state хранится в `backup_health/startup_quick_check_state.json`; fingerprint учитывает путь, размер, mtime, profile, schema markers (`data/dao/db_manager.py:107-115`, `543-691`).
9. Connection-profile lock/retry использует `DB_LOCK_PATH` и таймаут `REMCARD_CONNECTION_PROFILE_LOCK_TIMEOUT_SEC` (`data/dao/db_manager.py:95-106`, `320-383`).
10. При busy/locked/unavailable startup guard НЕ считает это corruption и не восстанавливает БД автоматически (`app/startup_db_guard.py:1071-1083`; `data/dao/db_manager.py:771-777`).
11. При подтверждённой corruption recovery идёт через `recover_shared_db_with_locks()` с `recovery.lock`, `db.lock`, проверкой active role locks и выбором валидного backup (`app/startup_db_guard.py:732-972`).
12. Нельзя считать corruption обычный сетевой lock, недоступную сетевую папку, busy database или timeout lock: это отдельно классифицируется в `app/startup_db_guard.py:147-171`, `1071-1083`.

## 6. Как работает запись

Типовой путь:

1. UI собирает действие и expected revision/version, если домен это поддерживает.
2. UI ставит pending state: пример `PatientForm._begin_write_pending()` отключает форму и меняет кнопку на `СОХРАНЕНИЕ...` (`ui/patient_bed_management/patient_form.py:340-350`).
3. UI вызывает `patient_bed_service.enqueue_write(...)` или `data_service.enqueue_write(...)` (`ui/patient_bed_management/patient_form.py:287-292`, `management_widget.py:331-336`).
4. `DataService.enqueue_write()` передаёт task в `LocalWriteQueue` и регистрирует success/error callbacks (`services/data_service.py:76-113`).
5. `LocalWriteQueue` выполняет task в daemon thread, retry для SQLite busy/locked, затем вызывает callback (`app/sqlite_shared.py:828-949`).
6. Внутри task доменный сервис/DAO использует `DatabaseManager.remcard_transaction()` или `execute_remcard()` (`data/dao/db_manager.py:1580-1648`).
7. `SQLiteWriteController.transaction()` берёт `FileWriteLock`, делает `BEGIN IMMEDIATE`, `COMMIT`, `ROLLBACK` при ошибке (`app/sqlite_shared.py:732-810`).
8. После commit `DatabaseManager._after_write_committed()` чистит/обновляет read path (`data/dao/db_manager.py:1180-1186`).
9. Success callback обновляет UI; error callback снимает pending и показывает ошибку/делает refresh.

Optimistic lock/revision/version:

- `DataConflictError` и сообщение конфликта: `services/concurrency.py:1-20`.
- `OptimisticLockError`: `data/dao/exceptions.py:1-2`.
- Таблицы с `revision`/`version` перечислены в schema fastpath: `admissions`, `beds`, `ivl_episodes`, `clinical_events`, `vitals`, `fluids`, `orders`, `administrations`, `patient_status_events`, `diet_templates`, `diet_plan`, `oral_intake_events` (`app/unified_db_schema.py:40-99`).
- Примеры rowcount=0/!=1 -> conflict: vitals (`data/dao/vitals_dao.py:69-75`, `237-245`), fluids (`data/dao/fluids_dao.py:26-56`), diet (`data/dao/diet_dao.py:71-111`, `144-214`, `278-345`), patient bed update (`services/patient_bed_management/service.py:188-249`), orders (`services/order_service.py:63-121`, `505-522`, `599-674`), nurse administrations (`services/order_domain_service.py:877-934`).
- UI должен показывать конфликт как ошибку и обновлять данные. В `PatientForm` error callback снимает pending и показывает warning (`ui/patient_bed_management/patient_form.py:375-387`); orders widgets при ошибках делают forced refresh (`ui/doctor_view/orders_widget.py:416`, `495`, `1429`, аналогично nurse).

## 7. Как работает чтение и snapshot

Snapshot в проекте — иммутабельный набор данных для конкретного UI-контекста: пациент/госпитализация, дата смены, роль, режим live/archive, scope. `ReadCoordinator._finalize_snapshot()` добавляет `version`, `last_change_id`, `content_hash`, `dedup_signature`, `cache_key`, `context_hash`, `load_trace_id`, `source` и возвращает `MappingProxyType` (`services/read_coordinator.py:1607-1651`).

`full_card_snapshot` собирает карту пациента шире: vitals + patient/status/balance/orders-adjacent context в `RemCardService.build_full_card_snapshot()` (`services/remcard_facade.py:418-456`). Он разрешён при открытии карты пациента или forced full refresh для текущей карты. Он запрещён как способ построить W1/W1a или стартовый список всех пациентов.

Partial snapshots:

- `build_vitals_snapshot()` (`services/remcard_facade.py:234-292`);
- `build_patient_header_snapshot()` (`294-319`);
- `build_status_snapshot()` (`321-341`);
- `build_balance_snapshot()` (`343-375`);
- `build_diet_snapshot()` (`377-393`);
- `build_ivl_snapshot()` (`395-416`);
- `build_beds_snapshot()` (`204-227`);
- `build_orders_snapshot()` (`478-536`);
- W1a `build_w1a_upcoming_orders_snapshot()` (`1082-1100`).

`ReadCoordinator` умеет:

- card/vitals full-ish snapshots (`services/read_coordinator.py:412-605`);
- partial scope snapshots (`607-836`);
- orders tab cache/delta через `ChangeLogApplier`, только safe delta for `administrations` (`162-220`, `838-1079`, `1402-1464`);
- persistent cache в `%LOCALAPPDATA%\RemCard\cache\patient_snapshots` через `services/persistent_snapshot_cache.py:14-144`;
- content hash/dedup (`services/read_coordinator.py:1540-1651`);
- stale guard: UI discard по `request_id`, `admission_id`, `shift_date`, `context_key` (`ui/doctor_view/doctor_remcard_widget.py:627-680`, `ui/nurse_view/nurse_main_widget.py:637-704`).

## 8. Как работает синхронизация врача и медсестры

Триггеры `app/unified_db_schema.py:_create_change_triggers()` пишут `change_log` при INSERT/UPDATE/DELETE (`app/unified_db_schema.py:331-419`). Таблица `change_log` имеет `entity_name`, `entity_id`, `admission_id`, `action`, `changed_at`, `changed_by`, `version` (`app/unified_db_schema.py:724-736`).

`DataUpdateMonitor` — QThread polling loop (`services/data_update_monitor.py:12-189`):

- период по умолчанию 2 сек;
- хранит last_seen change id;
- при cursor gap/backwards/empty rows ставит `forced`, `gap_detected`, `reason`;
- отдаёт `changed_entities`, `admission_ids`, `changes`, `last_change_id`.

`DataService._emit_coordinated_changes()` классифицирует payload через `SyncCoordinator.classify()` (`services/data_service.py:164-168`). `SyncCoordinator` переводит entities/source prefixes в scopes и flags: `orders_refresh`, `diet_refresh`, `balance_refresh`, `beds_refresh`, `patient_header_refresh`, `status_refresh`, `ivl_refresh`, `full_refresh_required`, `card_snapshot_required`, `vitals_snapshot_required` (`services/sync_coordinator.py:15-96`).

Второй клиент видит изменения после commit через change_log polling. Собственные изменения не должны вызывать лишний full redraw: doctor/nurse widgets распознают local orders force и пропускают card snapshot, отдавая событие в orders widget и планируя balance update (`ui/doctor_view/doctor_remcard_widget.py:1081-1097`, `ui/nurse_view/nurse_main_widget.py:1074-1090`).

Forced full refresh включается при gap reasons: `gap_detected`, `cursor_moved_backwards`, `empty_change_rows`, `recovery`, `manual_refresh` (`services/sync_coordinator.py:7-13`, `60-96`).

## 9. База данных и защита

DB config/profile:

- `app/paths.py:122-177` — корень базы, DB paths, lock paths, local cache paths.
- `app/sqlite_shared.py:20-132` — network/local SQLite profiles и PRAGMA.
- Network PRAGMA: `foreign_keys=ON`, `journal_mode=DELETE`, `synchronous=EXTRA`, `temp_store=MEMORY`, `cache_size=-8192`, `mmap_size=0`, `busy_timeout=10000` (`app/sqlite_shared.py:44-52`, `111-132`).
- `locking_mode` для network profile явно НЕ НАЙДЕНО В КОДЕ.
- Backup API: `backup_connection()` (`app/sqlite_shared.py:253-347`).
- Restore: `restore_database()` и `restore_from_best_available_source()` (`app/sqlite_shared.py:350-364`, `488-546`), startup recovery (`app/startup_db_guard.py:779-972`).
- Quarantine invalid backup/current corrupt DB: `app/startup_db_guard.py:564-646`.
- `recovery.lock`: `app/startup_db_guard.py:931-972`; `db.lock`: `app/paths.py:164`, `app/sqlite_shared.py:549-686`.
- Active client/session/lease: `RoleSessionLock` (`app/role_session_lock.py:15-307`), recovery checks active other role locks (`app/startup_db_guard.py:732-758`).
- `schema_migrations`: table and markers (`app/unified_db_schema.py:749-756`, `1195-1205`).
- `client_policy.json`, `min_client_version`: `app/startup_db_guard.py:227-347`, migration updates through `app/schema_migration_guard.py:110-224`.
- Pre-migration backup: `ensure_unified_schema_with_migration_backup()` (`app/schema_migration_guard.py:88-224`).

## 10. UI и роли

Вход врача: `run_doctor.py`; вход медсестры: `run_nurse.py`. Главное окно: `MainWindow` (`ui/main_window.py:104-188`) лениво загружает роль (`ui/main_window.py:250-252`). Врач: `DoctorMainWidget` (`ui/doctor_view/doctor_main_widget.py:60-210`), `DoctorRemCardWidget` (`ui/doctor_view/doctor_remcard_widget.py:59`). Медсестра: `NurseMainWidget` (`ui/nurse_view/nurse_main_widget.py:83`).

W1/W1a/W1b:

- doctor layout: `ui/shared/remcard_layout.py:222-300`;
- nurse layout: `ui/nurse_view/nurse_remcard_layout.py:229-299`;
- W1a: `SectorW1a` (`ui/rem_card_sectors/sector_w1a.py:44`, `250-308`);
- W1b doctor/nurse placeholders: `ui/rem_card_sectors/sector_w1b.py`, `sector_w1b_nurse.py`.

PatientForm:

- `PatientForm` (`ui/patient_bed_management/patient_form.py:72`);
- deferred open via `QTimer.singleShot(0, ...)` and nonblocking `dialog.open()` (`ui/patient_bed_management/management_widget.py:165-199`);
- guards `_is_closing`, `_opening_patient_form`, `_active_patient_form`, context (`management_widget.py:64-68`, `165-235`, `377-390`);
- pending write and callbacks (`patient_form.py:278-387`).

## 11. Workers/threading/lifecycle

Workers:

- `AsyncCallThread` — не `QThread`, а `QObject` + Python `threading.Thread`, сигналы `succeeded/failed/finished` (`ui/shared/async_call.py:7-82`). Используется для snapshot loads, W1a, beds, BARS.
- `DataUpdateMonitor(QThread)` (`services/data_update_monitor.py:12-189`).
- `PdfBuildWorker(QThread)` (`ui/shared/pdf_build_worker.py:10-29`).
- `HtmlPdfWorker(QThread)` (`ui/shared/html_pdf_worker.py:9-40`).
- `AnalyticsWorker(QThread)` (`ui/shared/analytics_worker.py:6-32`).
- `FullReportWorker`/`DataCollectorWorker` в print sectors (`ui/rem_card_sectors/sector_print.py`).
- `LocalWriteQueue` worker thread (`app/sqlite_shared.py:828-949`).
- Optional local replica/outbox threads (`app/local_replica_sync.py`, `app/durable_sql_outbox.py`, `data/dao/db_manager.py:855-1008`).

Qt thread affinity rules по коду: тяжёлое чтение/аналитика/PDF уходит в worker, а UI обновляется через сигналы/callbacks. Нельзя менять Qt widgets из worker напрямую. Late callbacks guard: `_is_closing`, `_closing`, context keys и weakrefs (`patient_form.py:297-338`; `doctor_remcard_widget.py:627-680`; `nurse_main_widget.py:637-704`; `graphs_dialog.py:313-390`).

Shutdown:

- `DataService.shutdown()` останавливает monitor и queue (`services/data_service.py:146-158`);
- `DatabaseManager.close()` останавливает background threads и делает shutdown backup (`data/dao/db_manager.py:1872-1895`);
- role widgets выключают snapshot workers, timers, W1a, orders (`doctor_remcard_widget.py:2717-2734`, `nurse_main_widget.py:1946-1963`);
- `MainWindow.closeEvent()` выставляет `_is_closing` и вызывает shutdown (`ui/main_window.py:624-660`).

## 12. Производительность

Закрытые/реализованные элементы в текущем коде:

- lazy chart init: `DoctorRemCardWidget._ensure_chart_initialized()` (`ui/doctor_view/doctor_remcard_widget.py:1871-1885`), nurse (`ui/nurse_view/nurse_main_widget.py:1399-1413`);
- chart redraw dedupe: `_chart_snapshot_signature` + `_last_applied_chart_signature` (`doctor_remcard_widget.py:1887-1908`, nurse `1415-1425+`);
- repeated snapshot apply dedupe: `_last_applied_card_snapshot_signature` (`doctor_remcard_widget.py:661-680`, nurse `669-688`);
- partial snapshots and tab-level cache: `ReadCoordinator` (`services/read_coordinator.py:607-1079`);
- orders delta/cache telemetry: `services/read_coordinator.py:1792-1920`;
- startup metrics/trace: `app/main.py:21-100`, `scripts/startup_benchmark.py:34-90`;
- quick_check TTL-state: `data/dao/db_manager.py:107-115`, `543-691`;
- connection_profile lock retry/wait metrics: `data/dao/db_manager.py:95-106`, `348-383`;
- metrics go to local JSONL via `app/local_metrics.py:74-85`.

Цифры: исторические baseline/targets есть в `docs/performance_a_baseline.md:37-39` и рядом, но актуальные измерения текущей машины НЕ ЗАПУСКАЛИСЬ в рамках checkpoint.

## 13. Тема/стили

Фактический путь темы: `settings/color_scheme/style_settings.json`, а не просто `settings/style_settings.json`. Его задаёт `STYLE_SETTINGS_RELATIVE_PATH` (`ui/styles/theme_storage.py:23-24`). В dev путь ищется от project root, в compiled — от executable dir (`theme_storage.py:52-63`).

Структура settings: `active.doctor`, `active.nurse`, `preset_id`, `mode`, `density`, `overrides` (`settings/color_scheme/style_settings.json:1-20`; defaults `ui/styles/theme_tokens.py:25-42`). Встроенные presets в текущем коде только `remcard_light` и `remcard_dark` (`ui/styles/theme_presets.py:19-35`). `gray_compact` и `high_contrast` НЕ НАЙДЕНЫ В КОДЕ.

`ThemeStorage` при повреждённом JSON quarantines файл как `*.broken` и создаёт defaults (`ui/styles/theme_storage.py:71-118`). `ThemeManager` применяет QSS к `QApplication`, tooltip palette и runtime container styles (`ui/styles/theme_manager.py:17-133`, `135-176`). QSS строится в `ui/styles/qss_builder.py:7-120`.

Настройки отображения W1a/W1b/кнопок хранятся отдельно: `settings/display_settings/display_settings.json`, `DisplaySettingsStorage` (`ui/shared/display_settings_storage.py:14-18`, `83-120`, `277-321`). Мини-игры есть как bonus UI: `ui/shared/minigames/*`, сервисы `services/minigames/*`; пути scores/users идут в data root `minigames` (`services/minigames/minigame_paths.py:25-33`).

## 14. Тесты, проверки, acceptance gates

Основные gates:

- `python scripts/architecture_safety_check.py` — блокирует UI direct sqlite/write SQL/run_write/private service, profile mismatch, live DB copy bypass, проверяет backup/recovery artifacts (`scripts/architecture_safety_check.py:77-160`).
- `python scripts/regression_safety_checks.py` — большой набор regression checks: DB safety, migrations, recovery, optimistic locks, sync/read coordinator, W1a/W1b, workers, PatientForm, chart, medical audit (`scripts/regression_safety_checks.py:8538-8675`).
- `python scripts/code_quality_checks.py` — F821, BOM, complexity baseline (`scripts/code_quality_checks.py:56-180`).
- `python scripts/style_audit_check.py` — inline style/color audit с baseline (`scripts/style_audit_check.py:11-174`, `322+`).
- `python scripts/network_acceptance_runner.py --operations 24 --benchmark-clicks 3` — isolated temp Baza, multi-client stress, orders click benchmark, restore drill (`scripts/network_acceptance_runner.py:1-8`, `283-405`).
- `python scripts/restore_drill.py --max-files 20` — restore backup в отдельную папку и quick/integrity (`scripts/restore_drill.py:1-8`, `158-178`).
- `python scripts/validate_backups.py --max-files 20 --move-invalid` — проверка backup и перенос invalid (`scripts/validate_backups.py:1-10`, `43-111`).
- `python scripts/startup_benchmark.py --mode remcard --role doctor` / `--role nurse` — startup metrics (`scripts/startup_benchmark.py:1-9`, `34-90`).
- `python -m compileall app data services ui scripts` и `git diff --check` — стандартные sanity checks; `compileall` не найден как отдельный project gate, но это безопасная команда Python.

## 15. Сборка, обновление, релиз

PyInstaller spec `RemCard.spec` собирает четыре EXE: `RemCardDoctor`, `RemCardNurse`, `RemCardPathSetup`, `RemCardUpdater` (`RemCard.spec:116-170`). `COLLECT` кладёт сборку в `Prog` (`RemCard.spec:172-183`). Post-build копирует в target update package, создаёт `manifest.json`, затем последним пишет `ready.ok` (`RemCard.spec:214-252`).

Updater:

- `update_checker.py` ищет release dirs с `ready.ok`, `manifest.json`, required exe (`app/update_checker.py:15-28`, `192-228`);
- `update_launcher.py` в compiled режиме запускает `RemCardUpdater.exe`, ставит starting/update lock (`app/update_launcher.py:174-230`);
- `updater_main.py` валидирует source, ждёт закрытия локальных doctor/nurse session locks, копирует новую версию через staging и backup, rollback на ошибке (`app/updater_main.py:338-351`, `314-335`, `405-505`, `520-567`).

Перед обновлением по документам/gates: valid backup → migration → quick_check → integrity_check → запуск второго клиента. Старый клиент блокируется через `client_policy.json.min_client_version` (`app/startup_db_guard.py:277-279`; `docs/release_update_regламент.md:28`).

## 16. Medical audit / change audit

`change_log` — механизм синхронизации UI. Его пишут change triggers, читает `DataUpdateMonitor`.

`medical_audit_log` — отдельная история медицинских действий. Таблица: `id`, `operation_id`, `table_name`, `row_id`, `admission_id`, `action_type`, `changed_at`, `changed_by`, `before_json`, `after_json` (`app/unified_db_schema.py:920-932`). Триггеры создаёт `_create_medical_audit_triggers()` (`app/unified_db_schema.py:430-520`) и подключает для `orders`, `administrations`, `vitals`, `fluids`, `admissions`, `beds`, `patient_status_events`, `ivl_episodes`, `clinical_events`, `diet_plan`, `oral_intake_events` (`app/unified_db_schema.py:1412-1540`).

Колонки `role` и `client_id` в `medical_audit_log` НЕ НАЙДЕНЫ В КОДЕ. Есть `changed_by` и `operation_id`.

## 17. Текущие незакрытые задачи / риски

P0:

- НЕ НАЙДЕНО открытых P0 по DB safety в коде/gates; safety contracts и guards присутствуют.

P1 / нужна проверка:

- local-first/outbox код существует, но выключен. Не включать без аудита конфликтов/идемпотентности/операционного rollback (`data/dao/db_manager.py:186-190`, `docs/local_first_sync_plan.md`).
- `WriteCoordinator` как класс НЕ НАЙДЕН; документация/разговоры должны использовать реальные `LocalWriteQueue` + `SQLiteWriteController`.
- `medical_audit_log` не хранит `role/client_id`; если аудит должен различать рабочие места, нужна отдельная задача.
- Фактические production performance numbers требуют отдельного benchmark запуска; checkpoint не запускал heavy tests.

P2 / technical debt:

- `ui/rem_card_sectors/s_print/full_report_data.py:129` обращается к приватному `_calculate_totals` у `oral_service`; это не DB write path, но нарушает общий стиль "не лезть в private".
- `gray_compact`/`high_contrast` темы не реализованы, хотя могут встречаться в старых планах.
- W1b выглядит как placeholder; если требовалась богатая функциональность, нужна продуктовая проверка.

## 18. Как объяснить проект новому разработчику за 10 минут

Открой сначала:

1. `app/main.py` — запуск, startup guard, role locks.
2. `app/sqlite_shared.py` — SQLite profile, locks, write controller, queue, backup.
3. `data/dao/db_manager.py` — центральный DB manager, read/write, quick_check TTL, backup, local-first disabled.
4. `services/data_service.py` + `services/read_coordinator.py` + `services/sync_coordinator.py` — write queue callbacks, snapshots, sync classification.
5. `ui/doctor_view/doctor_remcard_widget.py` и `ui/nurse_view/nurse_main_widget.py` — как UI открывает карту и применяет snapshots.

Пять правил:

1. UI не пишет в SQLite напрямую.
2. Network SQLite profile: DELETE/EXTRA/mmap=0, WAL нельзя.
3. "Сохранено" только после success callback commit path.
4. W1/W1a не строятся через full snapshots всех карт.
5. Recovery только через locks, active-client guard и валидный backup.

Потоки:

- запись: UI → `enqueue_write` → `LocalWriteQueue` → `SQLiteWriteController` → DAO → commit;
- чтение: UI → `ReadCoordinator`/service snapshot → DAO read-only connection;
- sync: triggers → `change_log` → `DataUpdateMonitor` → `SyncCoordinator` → targeted UI refresh.

Перед релизом прогнать gates из раздела 14.

## 19. Глоссарий

- `snapshot` — read-only набор данных для UI-контекста с `version/content_hash/dedup_signature`.
- `full_card_snapshot` — широкий snapshot карты пациента из `RemCardService.build_full_card_snapshot()`.
- `partial snapshot` — snapshot отдельного scope: balance, diet, status, ivl, header, beds, orders.
- `read-model` — специально собранное представление для UI, например W1a upcoming orders.
- `change_log` — таблица событий изменений для sync UI.
- `DataUpdateMonitor` — polling QThread, читает change cursor.
- `SyncCoordinator` — классифицирует changes в targeted refresh flags.
- `ReadCoordinator` — строит/caches snapshots и orders deltas.
- `SQLiteWriteController` — контролирует transaction + lock + BEGIN IMMEDIATE.
- `FileWriteLock` — межпроцессный JSON lock-файл.
- `LocalWriteQueue` — локальная очередь write tasks.
- `quick_check` / `integrity_check` — SQLite проверки здоровья.
- `recovery` — восстановление shared DB из валидного backup под `recovery.lock` и `db.lock`.
- `migration` — доведение схемы до unified schema с pre-migration backup.
- `optimistic lock` — проверка `revision/version` перед update/delete.
- `revision` — integer counter в медицинских/операционных таблицах.
- `stale guard` — discard позднего snapshot callback при смене пациента/даты/context.
- `pending/success/error` — UI-состояния записи до commit, после commit и при ошибке.
- `W1` — режим списка коек/пациентов.
- `W1a` — сектор ближайших назначений.
- `W1b` — нижний сектор W1.
- `sector 1a/1b` — левые сектора карты; в W1 заменяются на W1a/W1b.
