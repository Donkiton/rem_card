# Database and safety

## 1. Где хранится SQLite-БД

Основная БД: `BAZA_DIR\archiv\rao_journal.db`.

Код:

- `BAZA_DIR` определяется в `app/paths.py:122-131`.
- `ARCHIV_DIR = BAZA_DIR\archiv` (`app/paths.py:133`).
- `JOURNAL_DB_PATH = ARCHIV_DIR\rao_journal.db`, `REMCARD_DB_PATH = JOURNAL_DB_PATH` (`app/paths.py:160-162`).
- Legacy path: `LEGACY_REMCARD_DB_PATH = REM_CARD_DIR\rem_cards_data.db` (`app/paths.py:163`) — текущий unified path использует `rao_journal.db`.

## 2. Как выбирается путь

В dev:

- если задан `REMCARD_BAZA_DIR`, используется он;
- иначе dev fallback из `get_dev_baza_dir()`.

В compiled:

- используется `resolve_baza_dir()` и config file path setup (`app/paths.py:122-131`, `app/runtime_paths.py`).
- `run_path_setup.py` запускает `_run_path_setup()`, где пользователь выбирает папку, затем вызывается `create_baza_structure_and_db()` и `write_configured_baza_dir()` (`app/main.py:413-446`).

## 3. Network profile

Network profile задан в `app/sqlite_shared.py:_resolve_sqlite_profile_settings()`:

- `profile_id = network_safe_v1` (`app/sqlite_shared.py:20`);
- `journal_mode = DELETE`;
- `synchronous = EXTRA`;
- `temp_store = MEMORY`;
- `cache_kb = 8192`;
- `mmap_mb = 0`;
- `busy_timeout_ms = 10000`.

Код: `app/sqlite_shared.py:44-52`.

Env overrides для network принудительно не могут изменить `journal_mode`, `synchronous`, `mmap_mb`: код сбрасывает на `DELETE`, `EXTRA`, `0` (`app/sqlite_shared.py:103-107`).

## 4. PRAGMA

`configure_connection()` (`app/sqlite_shared.py:111-132`) применяет:

- `PRAGMA busy_timeout`;
- `PRAGMA foreign_keys = ON`;
- для readonly: `PRAGMA query_only = ON`;
- для write-capable connection: `journal_mode`, `synchronous`;
- `temp_store`;
- `cache_size = -cache_kb`;
- `mmap_size`.

`locking_mode` явно НЕ НАЙДЕНО В КОДЕ.

## 5. Почему WAL запрещён

В коде/документах WAL запрещён для shared DB:

- network profile always `DELETE` (`app/sqlite_shared.py:44-52`, `103-107`);
- `client_policy.json` default содержит `wal_allowed_on_shared_db: False` (`app/startup_db_guard.py:227-234`);
- startup guard блокирует policy с `wal_allowed_on_shared_db=True` (`app/startup_db_guard.py:284-285`);
- `checkpoint_wal()` ничего не делает и пишет log, что WAL disabled (`data/dao/db_manager.py:1869-1870`);
- `docs/db_safety_contract.md:14` фиксирует запрет.

Причина в проектной модели: SQLite лежит в сетевой папке и используется несколькими ПК; safety выбран важнее скорости.

## 6. Почему synchronous=EXTRA

`synchronous=EXTRA` — обязательная инварианта network profile (`app/sqlite_shared.py:44-52`, `103-107`). Это усиливает гарантию сброса данных на диск/сетевое хранилище по сравнению с более слабыми режимами. Ослабление запрещено acceptance criteria и проверяется `scripts/architecture_safety_check.py:87-102`.

## 7. Почему mmap_size=0

`mmap_mb=0` для network profile (`app/sqlite_shared.py:44-52`) и принудительный reset env override (`app/sqlite_shared.py:103-107`). Это снижает риск неконсистентного поведения memory-mapped IO на сетевом ресурсе.

## 8. FileWriteLock

`FileWriteLock` (`app/sqlite_shared.py:549-686`) — JSON lock-файл с atomic create `os.O_EXCL`, owner metadata, stale detection, reentrant per thread release.

Используется:

- write transactions через `SQLiteWriteController`;
- backup/migration/recovery locks;
- connection profile lock.

Lock paths:

- `DB_LOCK_PATH = BAZA_DIR\archiv\db.lock` (`app/paths.py:164`);
- `RECOVERY_LOCK_PATH = BAZA_DIR\locks\recovery.lock` (`app/paths.py:165`).

## 9. SQLiteWriteController

`SQLiteWriteController` (`app/sqlite_shared.py:688-815`) контролирует write transaction:

- retry settings default 20 попыток, 200ms base delay (`app/sqlite_shared.py:688-704`);
- `connection_guard()` сериализует доступ к connection (`708-729`);
- `transaction()` берёт file lock, делает `BEGIN IMMEDIATE`, затем `COMMIT` или `ROLLBACK` (`732-810`);
- nested transaction uses existing cursor (`748-754`).

## 10. BEGIN IMMEDIATE

`BEGIN IMMEDIATE` выполняется в `SQLiteWriteController.transaction()` (`app/sqlite_shared.py:772`). Это заранее берёт write lock SQLite, чтобы конфликт был явным до выполнения пачки операций.

В migration fallback `_plain_write_transaction()` тоже использует `BEGIN IMMEDIATE` (`app/schema_migration_guard.py:64-76`).

## 11. busy_timeout/retry/backoff

- SQLite `busy_timeout_ms` default 10000 (`app/sqlite_shared.py:21`, `111-132`).
- `SQLiteWriteController` retry/backoff при lock/busy (`app/sqlite_shared.py:688-815`).
- `LocalWriteQueue` retry для sqlite locked/busy с random wait 0.10-0.30 sec (`app/sqlite_shared.py:912-949`).
- Connection profile lock waits up to `REMCARD_CONNECTION_PROFILE_LOCK_TIMEOUT_SEC` default 12s (`data/dao/db_manager.py:95-106`, `348-383`).

## 12. quick_check/integrity_check

Functions:

- `run_quick_check(conn)` (`app/sqlite_shared.py:152-168`);
- `run_integrity_check(conn)` (`app/sqlite_shared.py:134-150`);
- `validate_sqlite_file(path)` (`app/sqlite_shared.py:395-423`).

Startup:

- startup guard quick_check with retries (`app/startup_db_guard.py:441-484`);
- `DatabaseManager._verify_quick_integrity_or_restore()` quick_check + TTL/recovery (`data/dao/db_manager.py:753-809`);
- background integrity monitor only logs critical and latest backup, does not auto-restore (`data/dao/db_manager.py:1424-1456`).

## 13. StartupDbGuard

Файл: `app/startup_db_guard.py`.

Основные функции:

- `_load_or_create_client_policy()` (`237-293`);
- `_check_quick_with_retries()` (`462-484`);
- `_apply_network_safe_profile_with_lock()` (`507-535`);
- `_recover_shared_db()` (`779-929`);
- `recover_shared_db_with_locks()` (`931-972`);
- `run_startup_db_guard()` (`975-1125`).

Класс с точным именем `StartupDbGuard` НЕ НАЙДЕН В КОДЕ; guard реализован функциями и dataclass `StartupGuardResult` (`app/startup_db_guard.py:41-49`).

## 14. Backup API

`backup_connection()` (`app/sqlite_shared.py:253-347`):

- optional `FileWriteLock`;
- делает SQLite `conn.backup(backup_conn)` (`281-284`);
- проверяет temp backup через `validate_sqlite_file()` (`301-318`);
- atomic `os.replace(temp_path, backup_path)` (`320`);
- пишет meta/audit/metrics (`321-345`).

Pre-migration backup использует этот API (`app/schema_migration_guard.py:88-104`, `149-155`).

Runtime backups: `_create_named_backup()` (`data/dao/db_manager.py:1464-1490`), shutdown backup (`1491-1504`), periodic backup (`1506-1510`).

## 15. Restore

Restore из backup:

- `restore_database()` копирует выбранный backup в temp, валидирует, `os.replace` в target, затем валидирует restored DB (`app/sqlite_shared.py:350-364`);
- startup recovery выбирает candidates, quarantine invalid/current corrupt, restore and profile (`app/startup_db_guard.py:538-646`, `761-929`).

Важно: `shutil.copy2` здесь используется для backup file → temp restore, не как live backup живой БД. Живой backup должен быть только SQLite Backup API.

## 16. recovery.lock/db.lock

- `recovery.lock`: serializes recovery across clients (`app/startup_db_guard.py:931-972`).
- `db.lock`: protects profile/migration/recovery/write windows (`app/startup_db_guard.py:787-928`, `app/sqlite_shared.py:549-686`).
- Heartbeats for startup/recovery locks: `_LockHeartbeat` (`app/startup_db_guard.py:56-92`).

## 17. active second client protection

Role locks:

- `get_role_lock_path(role)` returns `BAZA_DIR\session_locks\{role}.lock` (`app/paths.py:179-181`);
- `RoleSessionLock` writes heartbeat JSON and handles stale lock (`app/role_session_lock.py:15-307`);
- startup main acquires role lock with stale 60s/heartbeat 8s (`app/main.py:595-619`);
- recovery blocks if another role lock active (`app/startup_db_guard.py:732-758`, `839-856`).

Updater waits for local doctor/nurse windows before replacing program files (`app/updater_main.py:314-335`).

## 18. migration guard

`ensure_unified_schema_with_migration_backup()` (`app/schema_migration_guard.py:169-224`) runs under controller/file lock and creates backup before DDL if fastpath is not ready.

Fastpath readiness checks tables, indexes, triggers, columns, `schema_migrations`, and meta revision (`app/unified_db_schema.py:219-270`).

## 19. pre-migration backup

`_create_validated_backup()` (`app/schema_migration_guard.py:88-104`) calls `backup_connection(... validate=True ...)`. If backup fails, DDL must not run. Regression: `scripts/regression_safety_checks.py:1397-1435`.

## 20. schema_migrations

Table: `schema_migrations(version, applied_at, note)` (`app/unified_db_schema.py:749-756`).

Markers include:

- v8 orders optimistic lock;
- v9 medical audit foundation;
- v10 clinical optimistic locks;
- v11 patients.birth_date.

Код: `app/unified_db_schema.py:1195-1205`.

## 21. min_client_version/client_policy

Default policy:

```json
{
  "schema_version": 1,
  "min_client_version": APP_VERSION,
  "required_db_profile": "network_safe_v1",
  "wal_allowed_on_shared_db": false
}
```

Код: `app/startup_db_guard.py:227-234`.

Startup blocks old client if `APP_VERSION < min_client_version` (`app/startup_db_guard.py:277-279`). Migration updates policy through `update_client_policy_min_version()` (`app/schema_migration_guard.py:110-124`, `app/startup_db_guard.py:296-347`).

## 22. rollback/restore drill

Restore drill script:

- selects newest valid backup (`scripts/restore_drill.py:46-72`);
- restores into separate drill dir, not live DB (`scripts/restore_drill.py:1-8`, `89-155`);
- validates quick_check + integrity_check (`scripts/restore_drill.py:75-86`);
- CLI: `python scripts\restore_drill.py --max-files 20 --cleanup-restored` (`scripts/restore_drill.py:158-178`).

Backup validation:

- `python scripts\validate_backups.py --max-files 20 --move-invalid` (`scripts/validate_backups.py:1-10`, `114-130`).

## 23. Forbidden operations

- `journal_mode=WAL` for shared DB.
- `synchronous` below `EXTRA`.
- `mmap_size` above `0` for network profile.
- live DB copy backup via `shutil.copy*`.
- UI direct SQLite connection/write SQL.
- UI direct `DataService.run_write`.
- `service._private` access from UI.
- Auto-restore on lock/unavailable without corruption proof.
- Migration DDL without validated pre-migration backup.
- Enabling local-first/outbox without audit.

Static gate: `scripts/architecture_safety_check.py:77-160`.

## 24. Что делать при ошибках

### DB locked

Не считать corruption. Дождаться/закрыть второй клиент/проверить lock holder. Код классифицирует busy/locked отдельно (`app/startup_db_guard.py:162-171`, `1071-1083`). Write path имеет retry (`app/sqlite_shared.py:912-949`).

### DB unavailable

Проверить сетевую папку и путь `BAZA_DIR`. Startup guard возвращает unavailable message, recovery не запускает (`app/startup_db_guard.py:1071-1083`).

### quick_check failure

Если failure подтверждён как corruption, recovery через `recovery.lock` + `db.lock` + valid backup (`app/startup_db_guard.py:1085-1097`). Если не подтверждён — не restore (`data/dao/db_manager.py:785-786`).

### corrupt backup

Backup validation помечает invalid; startup recovery quarantines invalid candidates (`app/startup_db_guard.py:564-599`). Запустить `validate_backups.py --move-invalid`.

### old client

Startup guard блокирует до обновления (`app/startup_db_guard.py:277-279`), compiled flow может предложить updater (`app/main.py:484-539`).

### migration failure

Не продолжать работу; использовать pre-migration backup. Проверить reports/logs и `schema_migrations`. Regression гарантирует backup before DDL (`scripts/regression_safety_checks.py:1397-1481`).

### network folder disconnected

Не запускать recovery. Восстановить доступ к сетевой папке, затем повторить запуск. Unavailable markers не считаются corruption (`app/startup_db_guard.py:1071-1083`).
