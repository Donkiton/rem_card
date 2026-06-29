# Автообновление РЕМКАРТА

Рабочая папка программы определяется как папка запущенного EXE. Роли остаются отдельными файлами:

- `RemCardDoctor.exe`
- `RemCardNurse.exe`
- `RemCardOperBlockEmergency.exe`
- `RemCardOperBlockPlanned.exe`
- `RemCardPathSetup.exe`
- `RemCardUpdater.exe`

Публикационная папка обновлений:

```text
C:\Test\Baza_rao3_jurnal\UPD
```

В финальной публикации нет вложенной папки `UPD\Prog`. Имя `Prog` в `RemCard.spec` используется только как временная папка PyInstaller `dist\Prog` до копирования результата в `UPD`.

## Full-Пакет

Full-релиз собирается командой:

```powershell
python scripts\build_release.py
```

Скрипт поднимает версию, обновляет `CHANGELOG.md` и `app/release_info.json`, запускает PyInstaller и публикует полный one-dir пакет в `C:\Test\Baza_rao3_jurnal\UPD`.

Full-пакет содержит все файлы собранной программы:

```text
UPD\
  RemCardDoctor.exe
  RemCardNurse.exe
  RemCardOperBlockEmergency.exe
  RemCardOperBlockPlanned.exe
  RemCardPathSetup.exe
  RemCardUpdater.exe
  _internal\
  manifest.json
  ready.ok
```

`ready.ok` создаётся последним. Пока его нет, клиент не считает пакет готовым.

## Patch-Пакет

Patch-релиз собирается отдельной командой:

```powershell
python scripts\build_patch_update.py
```

Patch-builder не копирует исходники `.py` в установленную программу. Он делает временную full one-dir сборку, сравнивает её с базовой canonical-сборкой и кладёт в patch только реально изменённые output-файлы: EXE, ZIP, DLL, ресурсы, JSON и другие файлы из итоговой собранной папки.

Формат patch-публикации:

```text
UPD\
  payload\
    ...
  support\
    RemCardUpdater.exe   # только если сам updater входит в patch
  manifest.json
  ready.ok
```

Patch применяется только если `base_version` в manifest совпадает с установленной версией. При несовпадении updater показывает причину и нужен full-релиз.

Первый patch-aware updater доставляется только через full-релиз. До такого full-релиза patch-пакеты публиковать нельзя.

## Patch Cache

Локальный cache builder хранит в игнорируемой папке репозитория:

```text
C:\Test\rem_card\.remcard_patch_cache
```

В cache хранится canonical patched tree, а не просто raw full build. Это важно для generated-файлов: если `settings_release_snapshot.json` отличается только `exported_at`, но `content_hash` тот же, файл не включается в patch, а в canonical tree остаётся старая версия файла.

## Generated-Файлы

Из output diff исключаются:

- `manifest.json`
- `ready.ok`

Для `_internal\rem_card\settings_release\settings_release_snapshot.json` действует JSON-aware правило:

- одинаковый `content_hash`: файл считается неизменённым и не попадает в patch;
- разный `content_hash`: файл попадает в patch.

Dry-run patch сравнивается с canonical patched tree, а не с raw full build.

## Публикация

Patch-builder автоматически делает `git push` текущей ветки. Если push не прошёл, публикации в `UPD` нет и `ready.ok` не создаётся.

Порядок публикации:

1. Удалить старый `ready.ok`.
2. Очистить старый full/patch пакет из `UPD`.
3. Скопировать patch payload.
4. Записать и проверить `manifest.json`.
5. Проверить SHA-256 payload-файлов.
6. Создать `ready.ok` последним.

## Установка

Updater проверяет manifest, `base_version`, SHA-256 payload-файлов и SHA-256 старых установленных файлов. Hash mismatch останавливает установку без force-режима.

Перед заменой updater делает backup только затронутых файлов. При ошибке backup возвращается назад, а частично скопированные patch-файлы удаляются.

Patch не трогает локальные файлы рабочей папки:

- `remcard_data_path.json`
- `logs`
- `crash`, `crashes`, `fault`, `faults`
- `emergency`, `local`
- `settings`
- `rem_card\data\dictionaries`
- root-level `*.log`

Python-изменения в текущей PyInstaller-схеме всё ещё могут менять EXE/PYZ/ZIP, потому что Python-код упакован внутрь one-dir output. Эксперимент с другой раскладкой PyInstaller - отдельная будущая задача.
