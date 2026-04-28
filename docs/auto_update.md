# Автообновление РЕМКАРТА

Рабочая программа лежит в папке `Prog` рядом с папкой базы:

```text
\\test-fileserver.example.test\share\TestDepartment\Patients\remcard\Prog
```

Пакет обновления кладется в соседнюю папку `UPD`:

```text
\\test-fileserver.example.test\share\TestDepartment\Patients\remcard\UPD
```

Внутри `UPD` должны лежать сами файлы сборки, без вложенной папки `Prog`:

```text
UPD\
  RemCardDoctor.exe
  RemCardNurse.exe
  RemCardPathSetup.exe
  RemCardUpdater.exe
  _internal\
  manifest.json
  ready.ok
```

`manifest.json` создается при сборке через `RemCard.spec`. `ready.ok` означает, что пакет полностью скопирован и его можно ставить.

Важно: при публикации на сетевую папку сначала удалите старый `ready.ok`, затем скопируйте все файлы обновления, и только последним действием положите новый `ready.ok`. Пока `ready.ok` отсутствует, пользователи не увидят недокопированное обновление.

## Сборка пакета

Обычная команда релизной сборки:

```powershell
python scripts\build_release.py
```

Скрипт сам поднимет версию, добавит git-коммиты в `CHANGELOG.md`, обновит `app/release_info.json`, запустит PyInstaller и положит готовый пакет в локальную папку:

```text
C:\Test\Baza_rao3_jurnal\UPD
```

Для крупного обновления можно явно указать уровень:

```powershell
python scripts\build_release.py minor
python scripts\build_release.py major
```

## Поведение у пользователя

При запуске программа быстро проверяет `UPD\manifest.json`. Если версия там выше текущей, программа запускает `RemCardUpdater.exe` из `UPD` и закрывается.

Updater показывает отдельное окно прогресса, ставит общий lock в `Baza_rao3_jurnal\locks\remcard_update.lock`, ждет закрытия активных окон врача/медсестры и заменяет управляемые файлы в рабочей папке `Prog`:

- `RemCardDoctor.exe`
- `RemCardNurse.exe`
- `RemCardPathSetup.exe`
- `RemCardUpdater.exe`
- `_internal`
- `VERSION`
- `CHANGELOG.md`
- `manifest.json`

Локальные файлы рабочей папки не удаляются:

- `remcard_data_path.json`
- `logs`
- `rem_card\data\dictionaries`

Если пользователь попытается закрыть окно updater во время установки, появится предупреждение, а обновление продолжится.
