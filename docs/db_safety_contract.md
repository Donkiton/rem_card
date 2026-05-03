# DB safety contract

Этот контракт фиксирует красные линии для общей SQLite-БД в сетевой папке
`Baza_rao3_jurnal`. Цель контура - сохранность данных и предсказуемое
поведение двух рабочих мест, а не максимальная скорость.

## Инварианты сетевой БД

- Сетевой профиль SQLite: `journal_mode=DELETE`.
- Сетевой профиль SQLite: `synchronous=EXTRA`.
- Сетевой профиль SQLite: `mmap_size=0`.
- `foreign_keys=ON` обязателен для рабочих соединений.
- `busy_timeout` обязателен для рабочих соединений.
- WAL для shared DB запрещён.
- Ошибки `locked`, `busy`, `unavailable`, ошибки доступа к сетевой папке и
  permission errors не являются признаком corruption.

## Записи

- Центральные записи должны проходить через `SQLiteWriteController`.
- Запись в shared DB должна быть сериализована через `FileWriteLock`.
- `BEGIN IMMEDIATE` остаётся точкой входа для write-транзакций.
- UI не должен обходить write-controller прямыми синхронными записями.

## Backup и миграции

- Живой backup SQLite-БД выполняется только через SQLite Backup API
  (`backup_connection`).
- Простой copy живой shared DB запрещён.
- Перед DDL/migration на существующей shared DB должен быть создан validated
  pre-migration backup.
- Backup считается пригодным только после `quick_check` и `integrity_check`.
- Невалидный backup должен быть отправлен в quarantine/invalid backups и не
  должен использоваться для restore.
- Массовые schema changes без pre-migration backup запрещены.

## Recovery

- Restore допускается только из validated backup/snapshot.
- Auto-restore не должен запускаться при обычном lock/unavailable.
- Restore shared DB должен идти через `recovery.lock`, затем `db.lock`, с
  повторной проверкой БД после получения lock.
- После restore обязательны `quick_check` и `integrity_check`.
- Нельзя заменять shared DB, если другой активный клиент продолжает работу с
  открытым SQLite-соединением.

## Изменения, требующие сетевых тестов

- Изменение PRAGMA network-профиля.
- Изменение `FileWriteLock`, `SQLiteWriteController`, startup guard или recovery.
- Новые DDL/migration и изменение `schema_migrations`.
- Включение local-first/outbox для shared workflow.
- Изменение backup/restore пути.
- Изменения polling/change_log/snapshot, влияющие на синхронизацию врача и
  медсестры.
