# Версионность РЕМКАРТА

Версия хранится в файле `VERSION` в формате `MAJOR.MINOR.PATCH`.

- `PATCH`: небольшие исправления и точечные изменения, например `1.0.1` → `1.0.2`.
- `MINOR`: крупное обновление или заметная новая функция, например `1.0.2` → `1.1.0`.
- `MAJOR`: очень большое или несовместимое изменение, например `1.1.0` → `2.0.0`.

Слово `PATCH` здесь обозначает только уровень номера версии. Отдельных patch-пакетов обновления в проекте больше нет: версия любого уровня распространяется полной сборкой.

## Подготовка версии

```powershell
.\.venv\Scripts\python.exe scripts\bump_version.py patch "Исправлена печать карты"
```

Уровень версии задаётся при подготовке изменений:

```powershell
.\.venv\Scripts\python.exe scripts\bump_version.py patch "Описание исправления"
.\.venv\Scripts\python.exe scripts\bump_version.py minor "Описание возможности"
.\.venv\Scripts\python.exe scripts\bump_version.py major "Описание большого обновления"
```

Точную версию можно указать явно:

```powershell
.\.venv\Scripts\python.exe scripts\bump_version.py --set 4.1.0 "Описание релиза"
```

Команда одновременно обновляет `VERSION`, русский раздел `CHANGELOG.md` и
`app/release_info.json`. Эти файлы должны попасть в обычный pull request и быть
слиты в GitHub `main` до production-сборки.

## Production-сборка

Менеджер релизов получает точные версию и коммит из настроенного
GitHub-репозитория, безопасно обновляет чистую локальную ветку `main` и запускает:

```powershell
.\.venv\Scripts\python.exe scripts\build_release.py `
  --expected-version 4.1.0 `
  --expected-commit <40-символьный-коммит>
```

`build_release.py` не меняет исходники, не создаёт коммиты и не выполняет
`git push`. Он проверяет соответствие `VERSION`, changelog и release-info
указанному коммиту, запускает обязательные architecture, fast regression и
F821-проверки, собирает все EXE, проверяет manifest/inventory/settings snapshot,
выполняет compiled smoke и создаёт локальный full-релиз.

Сборка всегда начинается с чистых `build` и `dist`; временные каталоги удаляются
после завершения или ошибки. Если любой gate не пройден, локальный релиз не
создаётся.

Для непубликуемой сборки текущего рабочего дерева используется:

```powershell
.\.venv\Scripts\python.exe scripts\build_release.py --test-worktree
```

Такой пакет получает marker `TEST_WORKTREE_ONLY.txt` и не может быть опубликован.

## Результат

Локальный готовый пакет находится в:

```text
C:\Project\RemCardTestData\UPD\releases\<версия>
```

В production он переносится отдельной безопасной командой:

```powershell
.\.venv\Scripts\python.exe scripts\publish_full_update.py `
  --source "C:\Project\RemCardTestData\UPD\releases\<проверенная версия>" `
  --config "<папка программы>\remcard_data_path.json"
```

До этой команды локальный пакет обязан пройти приёмку на отдельной локальной/тестовой папке данных: обновление при запуске и после штатного закрытия, запуск всех ролей и изменённые рабочие сценарии. После создания `ready.ok` в сетевой production-базе релиз сразу доступен всем подключённым клиентам, поэтому production-публикация не используется как канареечный тест.

Чтобы остановить новые установки опубликованной версии, удаляют её `ready.ok`. Уже установленные клиенты это не откатывает; для них выпускают новую полную версию с более высоким номером.

Подробности: `docs/auto_update.md`.
