# Фирменный установщик Windows

Канонические исходники находятся в `installer/windows` основного проекта.
Перед работой читать [полную инструкцию](../../docs/windows_installer.md)
и [AGENTS.md](../../AGENTS.md). Не искать старые копии по чатам и временным worktree.

Принятый установщик 5.1.14 — русскоязычный; его не пересобирать. В исходниках
5.1.15 реализован переключатель русского/английского с первого окна, русский
по умолчанию. Смена языка сохраняет папку, параметры, согласие, тему и прогресс.

Мастер установки использует WPF из .NET Framework и отдельный принятый full ZIP приложения. Интерфейс повторяет согласованный концепт: свои окна и диалоги, выбор папки, лицензия, журнал, ЭКГ и переход в изолинию при ошибке установки. Сборка и запуск установщика не публикуют файлы в GitHub или сетевой UPD.

## Сборка

Требуются Windows x64 с .NET Framework 4.8, Python с Pillow и уже принятый полный ZIP. Версия берётся из `manifest.json` архива и сверяется с `VERSION`; SHA-256 принятого ZIP обязателен. Пользовательские настройки, базы и незакоммиченные файлы рабочего checkout не упаковываются.

```powershell
python installer\windows\build_installer.py `
  --package "<принятый full ZIP>" `
  --expected-sha256 "<SHA-256 из приёмки>" `
  --expected-min-client-version 5.1.15 `
  --expected-database-upgrades enabled `
  --output-dir "<отдельный каталог сборки>"
```

Результат — `RemCard-Setup-<версия>.exe`. Исходный архив входит в EXE без изменения байтов. `ready.ok` не устанавливается. Небольшой `RemCardUninstall.exe` и `remcard_installation.json` создаются только в конечной установке и не добавляются в исходный full-release или его manifest.

Профиль manifest сверяется с явными параметрами. Для обязательного 5.1.15:
`min_client_version=5.1.15`, `database_upgrades_disabled=false`; для необязательных
выпусков передавать `disabled` и согласованный минимум. Профиль не переписывается.
Все файлы ZIP проверяются по inventory, размеру и SHA-256 до компиляции.

Обычная установка персональная: `%LOCALAPPDATA%\Programs\RemCard`, без повышения прав. Каталог можно изменить. Программа регистрируется в списке приложений текущего Windows-пользователя; ярлыки создаются по выбранным параметрам.

## Изолированная приёмка

CLI-проверки требуют явные каталоги и JSON-отчёты. `--install-test` и `--uninstall-test` не меняют реестр и ярлыки, не запускают приложение и не открывают общую базу.

```powershell
& .\RemCard-Setup-5.1.15.exe --verify-payload --report "<отчёт JSON>"
& .\RemCard-Setup-5.1.15.exe --install-test --target "<изолированная установка>" --report "<отчёт JSON>"
& .\RemCard-Setup-5.1.15.exe --install-test --target "<та же установка>" --fail-after 0 --report "<отчёт rollback JSON>"
& .\RemCard-Setup-5.1.15.exe --uninstall-test --target "<та же установка>" --report "<отчёт удаления JSON>"
```

При `--fail-after 0` вызывается контролируемая ошибка после первого атомарного переноса программной части, чтобы проверить откат. Положительное число вызывает ошибку при распаковке соответствующего числа файлов. В обычном мастере эти режимы не включаются.

При переустановке сохраняются `remcard_data_path.json`, журналы и неизвестные пользовательские файлы. Удаление не затрагивает общую базу, локальные клинические данные, настройки темы и внешние кэши. Оно удаляет только распознанные программные файлы и ресурсы установленной копии.

Цифровая подпись не подменяется самоподписанным сертификатом. Для публичной подписанной поставки нужен отдельно предоставленный доверенный механизм подписи.

Полная приёмка `verify_installer.py` требует `--expected-min-client-version`
и `--expected-database-upgrades`, как builder. Оба установленных EXE проходят
compiled smoke с ожиданием принятого профиля миграций.

## Проверка русского и английского

```powershell
python installer\windows\verify_ui_languages.py --installer "<Setup EXE>" --output-dir "<новый LocalAppData\Temp\RemCard-installer-QA-ui-...>"
python -m unittest discover -s installer\windows -p test_installer_localization.py -v
```

Для UI без готового full ZIP заменить `--installer` на `--build-preview-fixture`.
Результат `RemCard-PREVIEW-ONLY.exe` содержит искусственный UI-payload, не является
дистрибутивом приложения и не используется для полной приёмки установки.

Один экран: `--ui-smoke --language en --preview review --target <путь>
--render-preview <PNG> --report <JSON>`. Экраны: `welcome`, `license`, `location`,
`review`, `progress`, `error`, `uninstall`. Отдельный `--language-state-smoke`
проверяет сохранение состояния при смене языка. `LICENSE` остаётся исходным,
CLI-параметры, машинные JSON-ключи и диагностические коды сохраняются.
