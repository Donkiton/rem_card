# Документация

Обновлено: 2026-07-14.

## Действующие регламенты

- `versioning.md` - версия, changelog и full release-сборка.
- `auto_update.md` - full-update, безопасная публикация в локальный и сетевой `UPD`, правила updater.
- `how_to_build_and_update.md` - простая пошаговая инструкция сборки и публикации для владельца проекта.
- `release_update_regламент.md` - порядок обновления клиентов и блокировка старых версий.
- `updater_optimization_plan.md` - выполненные и оставшиеся шаги оптимизации full-only обновлений.
- `updater_optimization_report_2026-07-14.md` - итог реализованной оптимизации, эффект и оставшиеся ограничения.
- `updater_test_report_2026-07-14.md` - результаты regression, PyInstaller и compiled smoke.
- `updater_visible_chat_2026-07-14.md` - стенограмма видимой переписки и журнал решений по задаче.
- `settings_db.md` - центральная settings DB, release snapshot и legacy import настроек.
- `db_safety_contract.md` - инварианты сетевой SQLite-БД, backup, миграции, recovery.
- `source_of_truth_and_sync_contract.md` - источник истины, путь записи, local replica, snapshots, cache и доставка изменений на второй компьютер.
- `crash_reporting.md` - размещение обычных логов, структурированные аварийные отчёты, обработка и срок хранения 180 дней.
- `metric_aggregation.md` - минутные сводки штатных метрик, сохранение аномалий, временная диагностика и независимый откат второго этапа.
- `compact_text_logging.md` - компактные текстовые INFO, breadcrumbs, повторные ошибки, hang-дампы и независимый откат третьего этапа.
- `architecture_guardrails.md` и `code_quality.md` - статические safety/quality gates.
- `operational_acceptance.md` - базовая приемка и дополнительные gates по аварийному режиму/оперблоку.
- `emergency_runbook.md` и `emergency_mode_smoke_checklist.md` - аварийный режим и ручной smoke.
- `backup_restore_drill.md`, `network_stress_test_plan.md`, `performance_a_baseline.md` - проверки эксплуатации и производительности.
- `operblock_ui_standards.md` - UI-стандарты оперблока.
- `burn_infusion_calculator.md` - периоды мониторинга, загрузка по запросу и расчётные ориентиры ожогового калькулятора.

Критичное правило release-процесса: вся приёмка выполняется на локальной/изолированной тестовой базе до сетевой production-публикации. После создания production `ready.ok` релиз доступен сразу всем клиентам этой базы; отдельного канареечного этапа внутри production нет.

## Исторические документы

- `journal_to_remcard_migration_plan.md` - исторический план миграции журнала в RemCard. Не использовать как текущий backlog без сверки с кодом и changelog.
- `local_first_sync_plan.md` - исторический план внедрения local-first; актуальный контракт и ограничения находятся в `source_of_truth_and_sync_contract.md` и текущем коде.
- `project_checkpoint/` - снимок архитектуры от 2026-05-12. Он полезен как карта системы, но line refs и статусные утверждения могут отставать от текущего кода. При расхождении приоритет: текущий код, действующие регламенты из списка выше и `CHANGELOG.md`.

## Legacy в коде

В проекте остается совместимость с legacy-данными: импорт старых JSON-настроек в settings DB, перенос старых фоновых файлов из `icon`, compatibility aliases для journal/remcard callers, legacy order/status migration paths. Это не "мертвый код" по одному только названию `legacy`; удалять такие места можно только после отдельной миграционной проверки и regression gates.
