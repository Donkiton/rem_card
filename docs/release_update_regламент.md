# Регламент обновлений

Главный риск обновления: один клиент мигрировал БД, а второй старый клиент продолжил писать в новую схему.

## Поля релиза

- `release_version`
- `db_schema_version`
- `min_client_version`
- `migration_required`
- `backup_before_update`
- `rollback_available`

## Порядок обновления

1. Сделать valid backup.
2. Закрыть врача и медсестру.
3. Обновить оба клиента.
4. Запустить первый клиент.
5. Выполнить миграцию.
6. Проверить `quick_check`.
7. Проверить `integrity_check`.
8. Запустить второй клиент.
9. Проверить синхронизацию врач -> медсестра и медсестра -> врач.

## Автоблокировка старого клиента

После несовместимой миграции `client_policy.json.min_client_version` должен быть не ниже версии клиента, который выполнил миграцию. Клиент с `APP_VERSION < min_client_version` не должен открывать рабочую БД.

## Full И Patch

Full-релиз остается основным безопасным способом для крупных обновлений:

```powershell
python scripts\build_release.py
```

Patch-релиз используется для точечных обновлений собранной one-dir программы:

```powershell
python scripts\build_patch_update.py
```

Оба варианта публикуются в:

```text
C:\Test\Baza_rao3_jurnal\UPD
```

В финальном `UPD` нет папки `Prog`.

Правила patch-релиза:

- первый patch-aware updater доставляется только full-релизом;
- patch применяется только к точной `base_version`;
- при несовпадении базы нужен full-релиз;
- `manifest.json` и `ready.ok` не участвуют в output diff;
- `ready.ok` создается последним;
- patch-builder делает `git push` до публикации в `UPD`;
- cache хранится в `C:\Test\rem_card\.remcard_patch_cache`;
- cache-база должна быть canonical patched tree;
- `settings_release_snapshot.json` пропускается при одинаковом `content_hash`;
- роли остаются отдельными EXE, без `--role`.

Если patch payload получается большим или содержит массовые изменения EXE/PYZ/DLL/ZIP, используйте full-релиз либо запускайте patch-builder с явным override после проверки причины.
