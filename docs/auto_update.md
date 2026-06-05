# Автообновление РЕМКАРТА

Рабочая программа лежит в папке `Prog` рядом с папкой базы:

```text
\\test-fileserver.example.test\share\TestDepartment\Patients\remcard\Prog
```

Пакет обновления кладется в папку `UPD` внутри папки базы:

```text
\\test-fileserver.example.test\share\TestDepartment\Patients\remcard\Baza_rao3_jurnal\UPD
```

Внутри `UPD` должны лежать сами файлы сборки, без вложенной папки `Prog`:

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

При запуске программа не ставит обычное необязательное обновление. Если старая версия уже заблокирована политикой базы и появляется сообщение о необходимости обновить программу, после нажатия `Понятно` запускается `RemCardUpdater.exe`. Если обновление уже выполняется для этой же установленной копии программы, пользователь увидит сообщение и программа не откроется до окончания замены файлов.

После нормального выхода из РЕМКАРТА программа проверяет `UPD\manifest.json`. Если версия там выше текущей, она сразу ставит короткий lock запуска обновления и запускает `RemCardUpdater.exe` из `UPD`.

`RemCardUpdater.exe` можно запускать вручную. Если он запущен прямо из `Baza_rao3_jurnal\UPD`, updater сам использует эту папку как источник обновления и ставит новую версию в соседнюю рабочую папку `Prog`. Если запущен установленный `Prog\RemCardUpdater.exe`, он ищет готовый пакет в `UPD` и передает работу `RemCardUpdater.exe` из пакета, чтобы установленный exe не блокировал замену файлов.

Updater показывает отдельное кастомное окно прогресса, ставит lock в `Baza_rao3_jurnal\locks` с привязкой к целевой папке программы, ждет закрытия активных окон врача/медсестры/оперблока на этом же компьютере и заменяет управляемые файлы прямо в папке, из которой была запущена программа. Для локальной рабочей папки lock привязан к паре `компьютер + путь`, поэтому обновление на другом ПК не блокирует запуск уже обновленной копии. Если целевая папка программы сама находится на сетевом пути, lock остается общим для этого пути, потому что несколько ПК в таком случае заменяют одни и те же файлы.

- `RemCardDoctor.exe`
- `RemCardNurse.exe`
- `RemCardOperBlockEmergency.exe`
- `RemCardOperBlockPlanned.exe`
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
