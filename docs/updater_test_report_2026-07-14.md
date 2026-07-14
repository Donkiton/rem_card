# Отчёт о финальных проверках updater

Дата финального цикла: 2026-07-14.

## Автоматические проверки

| Проверка | Результат |
|---|---:|
| Architecture safety | 8 / 8 PASS |
| Полный fast regression registry | 538 / 538 PASS |
| Покрытие registry | полное |
| Таймауты / worker crash / native crash | нет / нет / нет |
| Flake8 F821 | PASS, 0 ошибок |
| `compileall` и `py_compile` | PASS |
| UTF-8 BOM scan | PASS |
| `git diff --check` | PASS |

Финальный post-rebase fast regression занял 58,671 секунды, использовал 4 worker и 16 shards. Manifest реестра проверок: `e1017fbc987ff6937db05be16669bff6794573130f37b1b5aaf398737d05848a`.

В 538 проверок входят сценарии:

- legacy/full-only discovery и отказ от patch/неизвестной схемы;
- произвольная target-папка и сохранение production JSON;
- update при старте и после штатного закрытия;
- offline-старт оперблока без сетевых updater-проб;
- inventory/SHA, подмена manifest и лишние/пропавшие файлы;
- rollback, неполный rollback и запрет пересекающихся путей;
- lock retry, terminal fallback, dead PID и release-before-restart;
- успешная установка с предупреждением при сбое только автозапуска;
- обязательные release gates, push contract и smoke всех EXE;
- возобновляемая атомарная публикация и обязательный явный `--source` принятой версии.

## Реальная PyInstaller-сборка

Команда:

```powershell
.\.venv\Scripts\python.exe -m PyInstaller RemCard.spec
```

Результат:

- PyInstaller 6.20.0, Python 3.11;
- время финальной post-rebase сборки: 86,4 секунды;
- `dist\Prog`: 1 425 файлов, 452 687 238 байт;
- созданы `RemCardDoctor.exe`, `RemCardNurse.exe`, `RemCardOperBlockEmergency.exe`, `RemCardOperBlockPlanned.exe`, `RemCardPathSetup.exe`, `RemCardUpdater.exe`;
- `ready.ok` отсутствует, production и локальный тестовый `UPD` прямой PyInstaller-сборкой не затронуты.

## Compiled smoke

Выполнен точный helper из `build_release.py`. Каждый из шести EXE запущен с безопасным `--compiled-smoke` из готового `dist\Prog`.

Результат: 6 / 6 PASS, 31,7 секунды. Проверено дополнительно:

- в PYZ присутствуют `rem_card.app.main` и `rem_card.app.updater_main`;
- каждый процесс завершился с кодом 0;
- зависших процессов `RemCard*` не осталось;
- preliminary package validation прошла;
- `ready.ok` не создан преждевременно.

## Quality baseline

F821 и BOM полностью зелёные. Общий complexity-gate остаётся красным только из-за двух существовавших до этой задачи несвязанных блоков:

- `services/order_service.py::OrderService.commit_local_draft` — complexity 108;
- `ui/doctor_view/orders_widget.py::OrdersWidget._apply_snapshot_data` — complexity 43.

Они не изменялись в ходе оптимизации updater. Новых F-ranked блоков в updater/release-контуре нет.

## Что намеренно не проверялось на production

- Реальная скорость копирования на сетевой production SMB.
- Фактическая публикация в рабочий `UPD` и массовое обновление ПК.
- Реальные рабочие БД и production-настройки не изменялись.

Эти действия требуют отдельной приёмки принятого релиза по `docs/how_to_build_and_update.md`.
