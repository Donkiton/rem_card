# Документация

Обновлено: 2026-06-30.

## Действующие регламенты

- `versioning.md` - версия, changelog, release- и patch-сборки.
- `auto_update.md` - full/patch update package, публикация в `UPD`, правила updater.
- `release_update_regламент.md` - порядок обновления клиентов и блокировка старых версий.
- `settings_db.md` - центральная settings DB, release snapshot и legacy import настроек.
- `db_safety_contract.md` - инварианты сетевой SQLite-БД, backup, миграции, recovery.
- `architecture_guardrails.md` и `code_quality.md` - статические safety/quality gates.
- `operational_acceptance.md` - базовая приемка и дополнительные gates по аварийному режиму/оперблоку.
- `emergency_runbook.md` и `emergency_mode_smoke_checklist.md` - аварийный режим и ручной smoke.
- `backup_restore_drill.md`, `network_stress_test_plan.md`, `performance_a_baseline.md` - проверки эксплуатации и производительности.
- `operblock_ui_standards.md` - UI-стандарты оперблока.

## Исторические документы

- `journal_to_remcard_migration_plan.md` - исторический план миграции журнала в RemCard. Не использовать как текущий backlog без сверки с кодом и changelog.
- `local_first_sync_plan.md` - план/статус local-first; режим остается выключенным по умолчанию и требует отдельного аудита перед включением.
- `project_checkpoint/` - снимок архитектуры от 2026-05-12. Он полезен как карта системы, но line refs и статусные утверждения могут отставать от текущего кода. При расхождении приоритет: текущий код, действующие регламенты из списка выше и `CHANGELOG.md`.

## Legacy в коде

В проекте остается совместимость с legacy-данными: импорт старых JSON-настроек в settings DB, перенос старых фоновых файлов из `icon`, compatibility aliases для journal/remcard callers, legacy order/status migration paths. Это не "мертвый код" по одному только названию `legacy`; удалять такие места можно только после отдельной миграционной проверки и regression gates.
