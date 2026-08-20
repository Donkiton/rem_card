# Snapshots, sync and cache

## 1. Что такое snapshot в проекте

Snapshot — это read-only payload для UI, собранный под конкретный context: patient/admission, shift date, role, live/archive mode, source DB, scope/tab. `ReadCoordinator._finalize_snapshot()` добавляет `version`, `last_change_id`, `content_hash`, `dedup_signature`, `cache_key`, `context_hash`, `load_trace_id`, `source` и возвращает `MappingProxyType` (`services/read_coordinator.py:1607-1651`).

## 2. Какие есть snapshot types

Подтверждённые типы:

- beds: `RemCardService.build_beds_snapshot()` (`services/remcard_facade.py:204-227`);
- vitals: `build_vitals_snapshot()` (`234-292`);
- patient header: `build_patient_header_snapshot()` (`294-319`);
- status: `build_status_snapshot()` (`321-341`);
- balance: `build_balance_snapshot()` (`343-375`);
- diet: `build_diet_snapshot()` (`377-393`);
- ivl: `build_ivl_snapshot()` (`395-416`);
- full card: `build_full_card_snapshot()` (`418-456`);
- orders: `build_orders_snapshot()` (`478-536`);
- W1a upcoming orders: `build_w1a_upcoming_orders_snapshot()` (`1082-1100`).

## 3. Где ReadCoordinator

Файл: `services/read_coordinator.py`.

Ключевые классы/функции:

- `SnapshotContext` (`services/read_coordinator.py:39-115`);
- `ChangeLogApplier` (`162-220+`);
- `ReadCoordinator` (`337+`);
- `load_patient_vitals_snapshot()` (`412-522`);
- `load_patient_card_snapshot()` (`524-605`);
- partial loaders (`607-757`);
- `_load_patient_scope_snapshot()` (`759-836`);
- `load_orders_tab()` (`838-1079`);
- persistent cache read/write (`1150-1539`);
- `_finalize_snapshot()` (`1607-1651`).

## 4. Где SyncCoordinator

Файл: `services/sync_coordinator.py`.

`SyncCoordinator.classify()` берёт payload monitor-а и возвращает `sync_actions`: changed_entities, scopes, full/card/vitals flags and targeted refresh flags (`services/sync_coordinator.py:60-96`). Entity mapping находится в `ENTITY_SCOPES` (`15-34`), source prefixes — `SOURCE_PREFIX_SCOPES` (`36-51`).

## 5. Где DataUpdateMonitor

Файл: `services/data_update_monitor.py`.

`DataUpdateMonitor(QThread)` poll-ит `get_latest_change_id()` и `fetch_changes_since()` (`services/data_update_monitor.py:46-142`). Payload fields формируются в `_emit_payload()` (`143-189`): `previous_change_id`, `last_change_id`, `forced`, `gap_detected`, `reason`, `changes`, `changed_entities`, `admission_ids`.

## 6. Где change_log/triggers

`change_log` table: `app/unified_db_schema.py:724-736`.

Триггеры создаются `_create_change_triggers()` (`app/unified_db_schema.py:331-419`) для таблиц, подключённых в `ensure_unified_schema()` (`1220-1410`).

Индексы: `idx_change_log_admission_id`, `idx_change_log_entity`, `idx_change_log_changed_at` (`app/unified_db_schema.py:1186-1188`).

## 7. Как classified changed_entities

`DataUpdateMonitor._emit_payload()` нормализует rows и строит:

- `changed_entities`: set/table names from rows;
- `admission_ids`: ids from rows.

Код: `services/data_update_monitor.py:143-189`, `_normalize_row()` (`214-238`).

`SyncCoordinator.ENTITY_SCOPES` переводит entities:

- `orders`, `administrations` → orders/administrations;
- `vitals`, `vital_settings` → vitals;
- `fluids`, `oral_intake_events` → balance;
- `diet_*` → diet;
- `beds`, `patients`, `admissions` → beds/header;
- `patient_status_events` → patient_status;
- `ivl_episodes`, `clinical_events`, `devices`, `respiratory_support` → ivl.

Код: `services/sync_coordinator.py:15-34`.

## 8. Как admission_ids используются

`admission_ids` нужны для relevance filtering в UI. Role widgets игнорируют payload, если он не относится к текущей карте, кроме global/full cases. Примеры:

- doctor `_payload_is_relevant()` and `_on_data_changes()` (`ui/doctor_view/doctor_remcard_widget.py:1053-1126`);
- nurse `_on_data_changes()` (`ui/nurse_view/nurse_main_widget.py:1035-1120`).

Точный код `_payload_is_relevant()` не вынесен в этот документ; если меняется relevance logic, читать role widget целиком.

## 9. Как gap/forced/reason обрабатываются

Monitor sets:

- `cursor_moved_backwards` when current cursor < previous (`services/data_update_monitor.py:89-101`);
- `empty_change_rows` when current moved but no rows fetched (`103-118`);
- manual/force reasons via `request_refresh()` (`services/data_update_monitor.py:27-33`).

`SyncCoordinator.FULL_REFRESH_REASONS` содержит `gap_detected`, `cursor_moved_backwards`, `empty_change_rows`, `recovery`, `manual_refresh` (`services/sync_coordinator.py:7-13`). При таких reasons `full_refresh_required=True` (`services/sync_coordinator.py:70-96`).

## 10. Как dedup работает

Уровни dedup:

- `ReadCoordinator._snapshot_content_hash()` строит hash payload (`services/read_coordinator.py:1540-1544`);
- `_finalize_snapshot()` пишет `dedup_signature=(admission_id, scope, version, content_hash)` (`services/read_coordinator.py:1626-1635`);
- role widgets сравнивают `_last_applied_card_snapshot_signature` (`ui/doctor_view/doctor_remcard_widget.py:661-680`, `ui/nurse_view/nurse_main_widget.py:669-688`);
- chart сравнивает chart signature (`doctor_remcard_widget.py:1887-1908`, nurse `nurse_main_widget.py:1415-1425`);
- W1a сравнивает `content_hash` (`ui/rem_card_sectors/sector_w1a.py:292-308`).

## 11. Как stale guard работает

Snapshot request получает:

- `request_id`;
- `admission_id`;
- `shift_date`;
- `load_scope`;
- `context_key`.

UI discard conditions:

- request_id не текущий;
- admission/date поменялись;
- context_key не совпал;
- widget closing.

Код: doctor (`ui/doctor_view/doctor_remcard_widget.py:529-680`), nurse (`ui/nurse_view/nurse_main_widget.py:514-704`), orders stale apply blocked (`ui/doctor_view/orders_widget.py:1007-1033`; nurse `ui/nurse_view/components/nurse_orders_widget.py:692-718`).

## 12. Когда разрешён full_card_snapshot

Разрешён:

- при открытии текущей карты пациента (`load_scope in {"patient_open_card", "full"}`) через `ReadCoordinator.load_patient_card_snapshot()` или fallback (`doctor_remcard_widget.py:593-623`; nurse `603-633`);
- при full refresh current card from sync actions (`doctor_remcard_widget.py:1114-1126`; nurse `1108-1119`);
- в archive mode для конкретной карты (`doctor_remcard_widget.py:576-601`).

## 13. Когда запрещён full_card_snapshot

Запрещён как подход:

- для стартового списка всех пациентов;
- для W1a upcoming orders;
- для W1 beds list;
- для обработки обычного targeted change, где есть partial snapshot.

Подтверждение: W1a использует отдельный read-model (`services/order_domain_service.py:1014-1098`), regression forbids legacy/full-card markers (`scripts/regression_safety_checks.py:7264-7359`).

## 14. Как W1a должен/работает через upcoming orders snapshot

`SectorW1a`:

- sleep behavior if disabled via display settings (`ui/rem_card_sectors/sector_w1a.py`, regression `scripts/regression_safety_checks.py:7018-7091`);
- loader: `self.service.build_w1a_upcoming_orders_snapshot(datetime.now())` (`ui/rem_card_sectors/sector_w1a.py:250`);
- worker: `AsyncCallThread(loader, datetime.now())` (`254`);
- apply snapshot/dedup (`292-308`).

Service:

- `RemCardService.build_w1a_upcoming_orders_snapshot()` adds change_id/content_hash (`services/remcard_facade.py:1082-1100`);
- `OrderDomainService.get_upcoming_orders_across_active_admissions()` reads active beds/orders/admins/status with SQL (`services/order_domain_service.py:1014-1098`).

## 15. Как не допустить full snapshot всех пациентов

Правила:

- для W1 использовать `build_beds_snapshot()`;
- для W1a использовать upcoming orders snapshot;
- для открытой карты использовать `ReadCoordinator`;
- не вызывать `build_full_card_snapshot()` в цикле по active admissions.

Gate: `scripts/regression_safety_checks.py:7239-7359` проверяет W1a layout/read-model markers.

## 16. Где cache invalidation

ReadCoordinator invalidates:

- tabs: `get_cached_tab()`/`invalidate_tab()` (`services/read_coordinator.py:1081-1119`);
- vitals/card/scope caches (`services/read_coordinator.py:1150-1379`);
- stores snapshots with persistent cache (`1466-1539`).

UI invalidates vitals cache from sync payload before applying refresh (`ui/doctor_view/doctor_remcard_widget.py:1061`, nurse `ui/nurse_view/nurse_main_widget.py:1043`).

## 17. Где tab-level cache

Orders tab cache: `ReadCoordinator._orders_tab_cache` (`services/read_coordinator.py:353-364`), `load_orders_tab()` cache/delta/full reload (`838-1079`).

Cache key: `OrdersContext`/context hash. Delta apply allowed only for `administrations` (`ChangeLogApplier.SAFE_ORDERS_DELTA_ENTITIES`, `services/read_coordinator.py:165`).

## 18. Где latest_change_id/content_hash/version

- `DatabaseManager.get_latest_change_id()` optimized query (`data/dao/db_manager.py:1721-1785`);
- `RemCardService.get_latest_change_id()` wrapper (`services/remcard_facade.py:1282-1290`);
- `ReadCoordinator._finalize_snapshot()` writes `version`, `last_change_id`, `content_hash`, `dedup_signature` (`services/read_coordinator.py:1626-1635`);
- W1a content hash: `RemCardService._upcoming_orders_content_hash()` (`services/remcard_facade.py:1068-1080`).
