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

## Ротация медицинской БД

- Ротация `rao_journal.db` разрешена только для сетевого runtime.
- В аварийном режиме, snapshot-режиме или любой локальной аварийной БД ротация
  запрещена как вручную, так и автоматически.
- Автоматическая ротация выполняется только после выхода врача из программы, а
  не при запуске врача.
- Ротация запрещена, если роль медсестры активна на любом рабочем месте:
  обычный сетевой запуск держит `session_locks\nurse.lock`, аварийный запуск
  медсестры после восстановления доступа к сети держит
  `session_locks\nurse_emergency.lock`.
- Ротация запрещена, если найдена активная, ожидающая объединения или
  failed-merge аварийная сессия медсестры.
- Перед ротацией обязателен validated pre-rotation backup через SQLite Backup
  API в `backups\valid`.
- Ротация запрещена, если есть занятые койки или текущая БД не проходит
  `quick_check`.
- Новая рабочая БД должна быть подготовлена и пройти `quick_check` до переноса
  старой БД в архив.
- Ручная ротация должна использовать тот же lifecycle-код, locks и backup path,
  что и автоматическая ротация.

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
