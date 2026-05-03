# Приемочная проверка после дорожной карты

Цель проверки: доказать, что врач и медсестра могут работать с одной сетевой SQLite-БД без повреждения файла, потери commit и ложного UI-состояния.

## Обязательный набор

1. `python scripts/code_quality_checks.py`
2. `python scripts/architecture_safety_check.py`
3. `python scripts/regression_safety_checks.py`
4. `python scripts/network_acceptance_runner.py --operations 24 --benchmark-clicks 3`
5. `python scripts/restore_drill.py --max-files 20`

## Критерии приемки

- `quick_check` и `integrity_check` проходят после миграций, stress-run и restore-drill.
- Перед несовместимой миграцией создается validated pre-migration backup.
- Старый клиент блокируется через `client_policy.json.min_client_version` до работы с БД.
- Recovery не запускается при `locked/unavailable` и при активном втором клиенте.
- UI не показывает финальное сохранение до commit.
- Конфликт optimistic lock по orders показывает доменную ошибку, а не затирает строку молча.
- Обычные изменения через `change_log` не вызывают full snapshot всей карты.
- PDF/отчеты строятся в worker, а не в UI callback.

## Что считать провалом

- Любой `database disk image is malformed`.
- Любой auto-restore при занятой/недоступной БД.
- `journal_mode=WAL` или `synchronous` ниже `EXTRA` для network profile.
- `shutil.copy*` живой БД вместо SQLite Backup API.
- Прямой `sqlite3.connect` или write SQL из UI.
