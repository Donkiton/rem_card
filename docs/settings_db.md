# Central Settings DB

RemCard хранит общие справочники и пользовательские настройки в отдельной SQLite-БД:

```text
<BAZA_DIR>\settings\remcard_settings.db
```

`<BAZA_DIR>` берется из того же источника, что и основная медицинская БД. Основная БД пациентов остается в `<BAZA_DIR>\archiv\rao_journal.db`; таблицы справочников в нее не добавляются.

## Почему отдельная БД

- справочники и настройки отделены от медицинских записей;
- backup и миграции справочников не трогают схему пациентов;
- doctor/nurse/compiled build читают один сетевой источник истины;
- один lock-файл: `<BAZA_DIR>\settings\settings.db.lock`;
- один `settings_change_log` и один cursor версий каталогов.

## Что хранится

- препараты, группы препаратов, лекарственные формы, пути введения, растворители;
- шаблоны назначений и быстрые назначения;
- список врачей и подписи для печати;
- шаблоны питания;
- справочник анализов и материалы;
- настройки печати;
- настройки отображения, фона и цветовой схемы.

## Что не хранится

- медицинские записи пациента;
- факты назначений и выполнения назначений;
- `lab_orders`;
- vitals, admissions, patient status events;
- карточки пациента и snapshot-данные.

## Runtime source of truth

После создания `remcard_settings.db` runtime читает справочники и настройки из settings DB. JSON-файлы допускаются только как:

- legacy source для первого импорта;
- bundled seed внутри пакета/сборки;
- test fixture при явном тестовом пути.

Compiled build не должен создавать или требовать внешние dictionary/settings JSON рядом с exe.

## Safety profile

Settings DB использует тот же сетевой профиль SQLite:

- rollback journal / `journal_mode=DELETE`;
- `synchronous=EXTRA`;
- `mmap_size=0`;
- `foreign_keys=ON`;
- короткие write-транзакции через `SQLiteWriteController`;
- отдельный lock-файл `settings.db.lock`;
- без WAL, локальных копий и local-first/outbox.

Locked/busy/unavailable не считаются corruption и не запускают recovery. Corrupt settings DB обрабатывается отдельно и не затрагивает `rao_journal.db`.

## Миграции и backup

При создании пустой settings DB выполняется controlled schema init и one-time import legacy sources. Перед DDL-миграцией существующей settings DB создается validated backup через SQLite Backup API в:

```text
<BAZA_DIR>\settings\backups
```

`shutil.copy` для живой БД не используется.

## Reset одного элемента к seed

Reset должен быть service-level операцией: загрузить bundled seed, обновить выбранную строку в settings DB, сохранить audit, bump catalog version и записать `settings_change_log`. Нельзя перезаписывать весь каталог seed-ом.

## Добавление нового каталога

1. Добавить таблицу или ключ `app_settings`.
2. Добавить `catalog_key` в `settings_catalog_versions`.
3. Реализовать typed snapshot в `SettingsService`.
4. Все write-операции вести через settings transaction, audit и change log.
5. Добавить invalidation mapping в `SyncCoordinator`.
6. Добавить regression/architecture checks.
