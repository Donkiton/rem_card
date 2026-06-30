# Repository map

Репозиторий: `C:\Test\rem_card`.

Актуальность: исходный checkpoint собран 2026-05-12; entrypoint'ы, сборка и часть статуса обновлены по текущему коду на 2026-06-30. Старые line refs в этом каталоге могут отставать, перед изменениями сверяйте с кодом.

## 1. Корневые файлы

| Путь | Назначение | Кто использует |
|---|---|---|
| `_local_rem_card_bootstrap.py` | Dev-bootstrap alias package `rem_card` на текущий checkout; не подменяет frozen-сборку (`_local_rem_card_bootstrap.py:11-49`). | Scripts/tests/entrypoints. |
| `run_doctor.py` | Entry point врача: `main(forced_role="doctor")` (`run_doctor.py:14-18`). | PyInstaller, dev запуск. |
| `run_nurse.py` | Entry point медсестры: `main(forced_role="nurse")` (`run_nurse.py:14-18`). | PyInstaller, dev запуск. |
| `run_operblock.py` | Dev/диагностический entry point общего оперблока: `main(forced_role="operblock")`. | Dev запуск, не основной release EXE. |
| `run_operblock_emergency.py` | Entry point экстренной операционной: `main(forced_role="operblock_emergency")`. | PyInstaller, dev запуск. |
| `run_operblock_planned.py` | Entry point плановой операционной: `main(forced_role="operblock_planned")`. | PyInstaller, dev запуск. |
| `run_path_setup.py` | Entry point настройки пути к сетевой базе (`run_path_setup.py:9-13`). | `RemCardPathSetup.exe`. |
| `run_updater.py` | Entry point updater (`run_updater.py:9-13`). | `RemCardUpdater.exe`. |
| `launcher.py` | Dev helper без фиксированной роли: вызывает `main()` и полагается на выбор/определение роли. | Dev/ручной запуск. |
| `RemCard.spec` | PyInstaller сборка шести EXE и update package: doctor, nurse, operblock emergency/planned, path setup, updater. | Release build. |
| `VERSION` | Версия приложения; читается release/build/version code (`app/version.py`, `scripts/build_release.py`). | UI/update/build. |
| `CHANGELOG.md` | Changelog; build script обновляет release notes. | `scripts/build_release.py`. |
| `bars.py` | BARS-related entry/helper. ТРЕБУЕТ УТОЧНЕНИЯ. | BARS workflow. |
| `update_db.py` | Ручной helper миграции unified schema под lock/integrity-check. Для обычного релиза использовать release/update flow, не этот скрипт. | Ручные операции сопровождения. |

## 2. Папки

### `app/`

Инфраструктура запуска, путей, SQLite safety, backup/recovery/migration/update.

Ключевые файлы:

- `app/main.py` — startup, role lock, Qt lifecycle, shutdown (`app/main.py:659-850`).
- `app/paths.py` — Baza dirs, DB paths, lock paths, cache paths (`app/paths.py:122-177`).
- `app/runtime_paths.py` — compiled path setup/create DB (`app/runtime_paths.py:220-281`).
- `app/sqlite_shared.py` — SQLite profile, `FileWriteLock`, `SQLiteWriteController`, `LocalWriteQueue`, Backup API (`app/sqlite_shared.py:20-132`, `253-347`, `549-949`).
- `app/startup_db_guard.py` — startup quick_check/recovery/client_policy (`app/startup_db_guard.py:975-1125`).
- `app/schema_migration_guard.py` — pre-migration backup and schema migration (`app/schema_migration_guard.py:88-224`).
- `app/unified_db_schema.py` — unified schema/tables/triggers/migrations (`app/unified_db_schema.py:12-38`, `331-520`, `724-756`, `920-932`).
- `app/role_session_lock.py` — doctor/nurse session locks (`app/role_session_lock.py:15-307`).
- `app/update_checker.py`, `app/update_launcher.py`, `app/updater_main.py` — update detection/launch/install.
- `app/local_replica_sync.py`, `app/durable_sql_outbox.py` — local-first/outbox support, disabled by default.

Что можно менять осторожно: startup text, logging, non-safety UI glue. Что нельзя трогать без tests/gates: SQLite profile, backup/recovery, lock semantics, migration order, write controller, update lock/rollback.

### `data/`

DAO, DTO, dictionaries, MKB data.

- `data/dao/db_manager.py` — центральный DB manager: open connections, write transactions, backup, quick_check TTL, read retry, change cursor (`data/dao/db_manager.py:175-245`, `1580-1700`, `1721-1867`).
- `data/dao/*_dao.py` — SQL access. DAO могут писать, UI не должен.
- `data/dto/` — DTO models.
- `data/dictionaries/*.json` — drug/group dictionaries, user overrides.
- `data/mkb/mkb10.db` — readonly MKB reference (`services/mkb/service.py:7-28`).

Осторожно: DAO write SQL должен идти через `DatabaseManager.execute_remcard()`/transaction. Нельзя писать UI SQL напрямую.

### `services/`

Доменная логика, read/sync/write facades.

- `services/data_service.py` — UI-facing write queue and monitor signals (`services/data_service.py:11-168`).
- `services/remcard_facade.py` — `RemCardService`, snapshots and facade methods (`services/remcard_facade.py:46`, `204-536`, `1082-1100`).
- `services/read_coordinator.py` — snapshot/cache/delta (`services/read_coordinator.py:337-1651`).
- `services/sync_coordinator.py` — changed_entities → refresh actions (`services/sync_coordinator.py:6-138`).
- `services/data_update_monitor.py` — polling change_log (`services/data_update_monitor.py:12-189`).
- `services/order_service.py`, `services/order_domain_service.py` — orders and W1a read-model.
- `services/patient_bed_management/service.py` — patient/bed operations with optimistic locks.
- `services/analytics/*` — analytics/graphs read managers.
- `services/minigames/*` — isolated minigame state.

Нельзя: включать local-first/outbox by default, обходить `enqueue_write` в UI, ослаблять conflict checks.

### `ui/`

PySide6 UI.

- `ui/main_window.py` — окно, role loading, shutdown (`ui/main_window.py:104-188`, `624-660`).
- `ui/doctor_view/` — doctor role UI: `DoctorMainWidget`, `DoctorRemCardWidget`, orders, archive, W1 beds.
- `ui/nurse_view/` — nurse role UI: `NurseMainWidget`, nurse layout/orders/beds/sectors.
- `ui/operblock_view/` — UI экстренной/плановой операционной.
- `ui/shared/` — shared widgets/layout/workers/style helpers.
- `ui/rem_card_sectors/` — sector widgets and print.
- `ui/patient_bed_management/` — W1 patient/bed management and `PatientForm`.
- `ui/analytics/` — dialogs for statistics/graphs/PDF.
- `ui/styles/` — theme manager/storage/QSS/tokens/presets.
- `ui/admin_view/` — settings/display/print/admin.

Нельзя: `sqlite3.connect` in UI, direct write SQL, `DataService.run_write`, private service fields. Это проверяет `scripts/architecture_safety_check.py:77-84`.

### `scripts/`

Gates, benchmarks, release helpers.

Ключевые:

- `architecture_safety_check.py`;
- `regression_safety_checks.py`;
- `code_quality_checks.py`;
- `style_audit_check.py`;
- `network_acceptance_runner.py`;
- `multi_client_stress.py`;
- `restore_drill.py`;
- `validate_backups.py`;
- `startup_benchmark.py`;
- `build_release.py`, `bump_version.py`.

Некоторые scripts создают temp dirs/reports; перед production запуском смотреть параметры.

### `docs/`

Существующая документация: DB safety, restore drill, operational acceptance, release/update, performance baseline, medical audit, local-first plan. Новый checkpoint лежит в `docs/project_checkpoint/`.

### `settings/`

- `settings/color_scheme/style_settings.json` — активные темы doctor/nurse.
- `settings/display_settings/display_settings.json` — видимость кнопок/tabs/W1a/W1b.

В compiled режиме базовые display settings упаковываются через `RemCard.spec:50-61`, а style settings лежат рядом с exe (`ui/styles/theme_storage.py:52-63`).

### `icon/`

Иконки для EXE и UI. `RemCard.spec:80`, `127`, `141`, `155`, `169`.

### `standalone/`, `tmp/`

ТРЕБУЕТ УТОЧНЕНИЯ. Не являются основным runtime path по найденному коду.

## 3. Entry points

| Entry | Файл | Назначение |
|---|---|---|
| Врач | `run_doctor.py` | Запускает `app.main.main(forced_role="doctor")`. |
| Медсестра | `run_nurse.py` | Запускает `app.main.main(forced_role="nurse")`. |
| Оперблок общий | `run_operblock.py` | Dev entrypoint `forced_role="operblock"`; release использует отдельные столы. |
| Экстренная операционная | `run_operblock_emergency.py` | Запускает `forced_role="operblock_emergency"`, release EXE `RemCardOperBlockEmergency.exe`. |
| Плановая операционная | `run_operblock_planned.py` | Запускает `forced_role="operblock_planned"`, release EXE `RemCardOperBlockPlanned.exe`. |
| Path setup | `run_path_setup.py`, `app/main.py:_run_path_setup()` | Выбор сетевой папки и создание структуры (`app/main.py:413-446`). |
| Updater | `run_updater.py`, `app/updater_main.py` | Установка update package. |
| Startup benchmark | `scripts/startup_benchmark.py` | Измерение startup phases. |
| Acceptance | `scripts/network_acceptance_runner.py` | Isolated temp Baza + stress + restore drill. |
| Restore drill | `scripts/restore_drill.py` | Проверка restore backup в отдельную папку. |
| Release build | `scripts/build_release.py`, `RemCard.spec` | Version/changelog/PyInstaller/update package. |

## 4. Dependency directions

Допустимое направление:

```text
UI
  → services/controllers/use-cases
    → DAO
      → DatabaseManager
        → SQLite
```

Примеры:

- `PatientForm` → `PatientBedManagementService.enqueue_write()` → `DataService.enqueue_write()` → DAO/DB (`ui/patient_bed_management/patient_form.py:287-292`; `services/patient_bed_management/service.py:43-60`).
- `DoctorRemCardWidget` → `ReadCoordinator.load_patient_card_snapshot()` → `RemCardService.build_full_card_snapshot()` → DAO (`ui/doctor_view/doctor_remcard_widget.py:593-605`; `services/read_coordinator.py:524-605`).

Недопустимое:

- `UI → sqlite3.connect` напрямую.
- `UI → INSERT/UPDATE/DELETE/BEGIN/COMMIT` напрямую.
- `UI → DataService.run_write`.
- `UI → service._private` / `remcard_service._private`.
- `worker → Qt widgets` напрямую.

Проверенные результаты inspect:

- `sqlite3.connect` в `ui/` не найден.
- Direct write SQL in `ui/` не найден через опасные паттерны.
- Потенциальный private access найден: `ui/rem_card_sectors/s_print/full_report_data.py:129` вызывает `oral_service._calculate_totals(...)`. Это P2/нужна проверка: не write path, но нарушает общий принцип не лезть в private helpers.
- `services/ventilation_service.py:640` и `services/remcard_facade.py:1330` используют `data_service.run_write()` внутри service layer; это не UI, но для новых UI-действий предпочтителен `enqueue_write`.
