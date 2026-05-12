# Tests and release checklist

## 1. Быстрая проверка перед любым changeset

```powershell
git status --short --branch
git diff --check
python -m compileall app data services ui scripts
python scripts\architecture_safety_check.py
python scripts\code_quality_checks.py
```

Назначение:

- `git diff --check` ловит whitespace/conflict markers;
- `compileall` ловит syntax errors;
- `architecture_safety_check.py` защищает DB/write/profile boundaries;
- `code_quality_checks.py` ловит F821/BOM/complexity baseline (`scripts/code_quality_checks.py:56-180`).

## 2. Safety gates

```powershell
python scripts\architecture_safety_check.py
python scripts\regression_safety_checks.py
python scripts\code_quality_checks.py
python scripts\style_audit_check.py
```

Что защищают:

- architecture: UI no direct SQLite/write SQL, network profile, backup/recovery artifacts (`scripts/architecture_safety_check.py:77-160`);
- regression: DB safety, backup, migrations, recovery, optimistic locks, sync/read coordinator, W1a/W1b, workers, PatientForm, chart, medical audit (`scripts/regression_safety_checks.py:8538-8675`);
- code quality: undefined names, BOM, complexity baseline;
- style audit: inline QSS/colors baseline (`scripts/style_audit_check.py:11-174`, `322+`).

## 3. UI smoke

Manual:

1. Запустить врача: `python run_doctor.py`.
2. Запустить медсестру: `python run_nurse.py`.
3. Проверить W1 список коек, W1a ближайшие назначения, открытие карты.
4. Создать тестовую запись в не-production БД.
5. Проверить pending state и success/error callbacks.
6. Проверить закрытие приложения без зависших workers.

Автоматических UI smoke-команд кроме benchmark/acceptance в коде НЕ НАЙДЕНО.

## 4. Network acceptance

```powershell
python scripts\network_acceptance_runner.py --operations 24 --benchmark-clicks 3
```

Скрипт создаёт временную isolated `Baza_rao3_jurnal`, seed-ит admission, запускает stress/benchmark/restore и валидирует DB (`scripts/network_acceptance_runner.py:1-8`, `283-405`). Он не должен указывать на production DB.

Полезные параметры:

```powershell
python scripts\network_acceptance_runner.py --operations 60 --benchmark-clicks 10 --timeout-s 300
python scripts\network_acceptance_runner.py --operations 24 --benchmark-clicks 3 --keep-temp
```

## 5. Multi-client stress

Direct command example:

```powershell
python scripts\multi_client_stress.py --admission-id 1 --operations 120 --writer-interval-ms 120 --reader-poll-ms 150 --sync-timeout-s 5 --max-runtime-s 180
```

Параметры подтверждены в `scripts/multi_client_stress.py:382-390`. Скрипт проверяет writer commit → reader-visible propagation and latency (`scripts/multi_client_stress.py:8`, `550-588`).

## 6. Startup benchmark

```powershell
python scripts\startup_benchmark.py --mode remcard --role doctor
python scripts\startup_benchmark.py --mode remcard --role nurse
python scripts\startup_benchmark.py --mode patient-bed-management
```

Usage указан в `scripts/startup_benchmark.py:1-9`; measured phases: theme, imports, bootstrap, MainWindow/role widget (`scripts/startup_benchmark.py:34-90`).

## 7. Restore drill

```powershell
python scripts\validate_backups.py --max-files 20 --move-invalid
python scripts\restore_drill.py --max-files 20 --cleanup-restored
```

`validate_backups.py` проверяет backup files and can move invalid (`scripts/validate_backups.py:43-111`). `restore_drill.py` restores into separate test folder and runs quick_check/integrity_check (`scripts/restore_drill.py:1-8`, `75-86`, `158-178`).

## 8. Migration test

Минимальный gate:

```powershell
python scripts\regression_safety_checks.py
```

Покрытие migration:

- schema migration backup fastpath policy (`scripts/regression_safety_checks.py:1315-1383`);
- invalid backup blocks DDL (`1397-1435`);
- failed migration creates pre-migration backup (`1458-1481`);
- client policy/min version checks (`app/schema_migration_guard.py:110-224`).

Отдельной standalone команды "migration test" НЕ НАЙДЕНО В КОДЕ.

## 9. Old client block test

Покрывается кодом startup guard:

- default policy/min version (`app/startup_db_guard.py:227-293`);
- block if `APP_VERSION < min_client_version` (`app/startup_db_guard.py:277-279`);
- updater flow on old client in compiled startup (`app/main.py:484-539`).

Manual test: на копии Baza поднять `config/client_policy.json.min_client_version` выше текущей версии и проверить, что старый клиент не открывает DB и предлагает обновление. Автоматический standalone script НЕ НАЙДЕН.

## 10. Manual doctor/nurse сценарии

Проверить на тестовой БД:

- врач и медсестра одновременно открыты;
- врач меняет назначение, медсестра видит изменение через sync;
- медсестра отмечает выполнение, врач видит изменение без full redraw карты;
- конфликт: два клиента меняют один order/admin/vital/diet item, второй получает conflict/refresh;
- PatientForm save: pending → commit → close, error → pending снят;
- move/swap bed с expected revisions;
- W1a обновляется без открытия всех карт;
- archive readonly external DB не позволяет запись;
- PDF/graphs формируются в worker и UI не зависает;
- shutdown без зависших role locks.

## 11. Что проверять перед production rollout

1. Есть fresh valid backup.
2. `validate_backups.py --max-files 20 --move-invalid` passed.
3. `restore_drill.py --max-files 20 --cleanup-restored` passed.
4. Safety gates passed.
5. Network acceptance passed.
6. `client_policy.json.min_client_version` соответствует release plan.
7. Update package has `manifest.json`, `ready.ok`, four EXE and `_internal`.
8. Старые клиенты блокируются после несовместимой migration.
9. Второй клиент открывается после migration.
10. Не менялись `journal_mode=DELETE`, `synchronous=EXTRA`, `mmap_size=0`.

## 12. Что мониторить после запуска

- Startup errors: quick_check, client_policy, DB unavailable/locked.
- `backup_health` reports and invalid backups.
- `locks/recovery.lock`, `archiv/db.lock`, `session_locks/*.lock` stale state.
- Local logs/metrics: `app/local_metrics.py` writes JSONL metrics (`app/local_metrics.py:74-85`).
- Sync lag: `DataUpdateMonitor` records `change_log_lag_ms` (`services/data_update_monitor.py:143-189`).
- ReadCoordinator orders telemetry (`services/read_coordinator.py:1792-1920`).
- Update logs: updater writes to Baza logs (`app/updater_main.py:552-559`).
