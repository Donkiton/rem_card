<p align="center">
  <img src="icon/remcardicon.png" width="96" alt="Рем Карта logo">
</p>

<h1 align="center">Рем Карта</h1>

<p align="center">
  Desktop-приложение для ведения реанимационной карты пациента, сменной работы врача/медсестры и рабочих мест оперблока.
</p>

<p align="center">
  <img alt="Python" src="https://img.shields.io/badge/Python-3.x-3776AB?style=for-the-badge&logo=python&logoColor=white">
  <img alt="PySide6" src="https://img.shields.io/badge/UI-PySide6-41CD52?style=for-the-badge&logo=qt&logoColor=white">
  <img alt="SQLite" src="https://img.shields.io/badge/DB-SQLite-003B57?style=for-the-badge&logo=sqlite&logoColor=white">
  <img alt="License" src="https://img.shields.io/badge/License-MIT-111827?style=for-the-badge">
  <img alt="Status" src="https://img.shields.io/badge/status-vibe%20coded-f59e0b?style=for-the-badge">
</p>

> Важно: это не медицинское изделие, не эталон архитектуры и не пример production-grade разработки. Проект требует проверки, аудита и ответственности перед любым реальным использованием.

## Скриншоты

Изображения ниже - реальные скриншоты из программы. Они сняты с текущего UI на временных демо-базах с тестовыми данными, поэтому не содержат реальных пациентов, диагнозов, назначений или медицинских записей.

Главный экран RemCard со списком активных пациентов:

![Реальный экран карты пациента](docs/assets/readme/remcard-real-screenshot.png)

Открытая карта пациента на вкладке витальных функций с заполненным графиком:

![Реальный экран карты пациента с графиком витальных функций](docs/assets/readme/remcard-vitals-real-screenshot.png)

## Что это такое

`Рем Карта` - локальное desktop-приложение для отделения реанимации/интенсивной терапии. Оно помогает вести карту пациента по сменам: хранить витальные показатели, назначения, выполнения, баланс жидкости, питание, ИВЛ, события, процедуры, исходы, печатные формы и архив.

Отдельно в проекте есть рабочие места оперблока: экстренная и плановая операционные, быстрые назначения, таймлайн операции, печать и локальный/offline fallback на случай проблем с сетью.

## Зачем оно нужно

Проект появился как попытка заменить разрозненные бумажные/табличные процессы одним рабочим инструментом:

- врач видит активных пациентов, открывает карту, назначает лечение и печатает отчеты;
- медсестра видит назначения, отмечает выполнения и ведет сменную динамику;
- оперблок ведет активные случаи, препараты, события и завершение операции;
- данные врача и медсестры синхронизируются через общую SQLite-БД в сетевой папке;
- приложение старается не показывать "сохранено" до реального commit в БД.

## Установка и запуск

Проект разрабатывается и проверяется в основном на Windows и Python 3.11. Для локального запуска из исходников:

```powershell
git clone https://github.com/santa1264-hash/rem_card.git
cd rem_card
py -3.11 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
pip install -r requirements.txt
python launcher.py
```

Я обычно запускаю проект именно через `launcher.py`. Он поднимает локальный package alias через `_local_rem_card_bootstrap.py`, после чего вызывает `rem_card.app.main.main()`.

Для прямого запуска конкретных ролей есть отдельные entry point'ы:

```powershell
python run_doctor.py
python run_nurse.py
python run_operblock_emergency.py
python run_operblock_planned.py
```

В dev-режиме приложение само использует/создает структуру `Baza_rao3_jurnal` для базы, логов, настроек, backup и update-пакетов. В реальной эксплуатации это обычно общая сетевая папка, поэтому перед рабочим использованием путь и права доступа нужно проверять отдельно.

## Как работает

```mermaid
flowchart LR
    Doctor["RemCardDoctor.exe<br/>врач"] --> DB["Baza_rao3_jurnal<br/>archiv/rao_journal.db"]
    Nurse["RemCardNurse.exe<br/>медсестра"] --> DB
    OpE["RemCardOperBlockEmergency.exe<br/>экстренная операционная"] --> DB
    OpP["RemCardOperBlockPlanned.exe<br/>плановая операционная"] --> DB
    DB --> Settings["settings/remcard_settings.db<br/>общие справочники и настройки"]
    DB --> Backup["backups / recovery / update"]
```

Ключевая идея: несколько клиентов работают с одной сетевой папкой `Baza_rao3_jurnal`. Основная медицинская БД лежит в `archiv/rao_journal.db`, общие настройки и справочники - в `settings/remcard_settings.db`.

Для сетевой SQLite-БД проект жестко держит безопасный профиль:

- `journal_mode=DELETE`;
- `synchronous=EXTRA`;
- `mmap_size=0`;
- запись через lock, очередь и транзакции;
- backup через SQLite Backup API, а не копирование живого файла.

## Что внутри

- Карта пациента по сменам.
- W1-экран коек и активных госпитализаций.
- Назначения и выполнения назначений.
- Витальные показатели, баланс, питание, ИВЛ.
- Процедуры, события, исходы, архив.
- PDF/HTML-отчеты и печатные формы.
- Оперблок: экстренная и плановая операционные.
- Центральная settings DB для справочников, тем, фонов и настроек.
- Автообновление full/patch-пакетами.
- Аварийный режим и offline-сценарии для части workflow.

## Структура проекта

```text
rem_card/
├── launcher.py
├── run_doctor.py / run_nurse.py / run_operblock_*.py
├── app/
├── data/
├── services/
├── ui/
├── scripts/
├── docs/
├── settings/
├── icon/
├── procedure_templates/
├── RemCard.spec
├── requirements.txt
├── VERSION
└── CHANGELOG.md
```

### Корень

- `launcher.py` - мой обычный запуск в dev-режиме. Файл вызывает `_local_rem_card_bootstrap.py`, чтобы текущая папка работала как пакет `rem_card`, и затем передает управление в `app.main.main()`.
- `run_doctor.py`, `run_nurse.py`, `run_operblock_emergency.py`, `run_operblock_planned.py` - прямые entry point'ы ролей.
- `run_path_setup.py` - настройка пути к рабочей папке базы для собранной версии.
- `run_updater.py` - запуск updater'а.
- `RemCard.spec` - сборка PyInstaller: врач, медсестра, две операционные, path setup и updater.
- `VERSION`, `CHANGELOG.md`, `LICENSE`, `requirements.txt` - версия, история изменений, лицензия и зависимости.

### app

`app/` - слой запуска и системной инфраструктуры:

- `main.py` - основной startup приложения, выбор роли, создание Qt-приложения и главного окна.
- `roles.py` - роли врача, медсестры, оперблока и их нормализация.
- `paths.py`, `runtime_paths.py`, `db_runtime_context.py` - где искать `Baza_rao3_jurnal`, БД, логи, backup, настройки и runtime-файлы.
- `sqlite_shared.py`, `unified_db_schema.py` - общий SQLite-профиль, schema init, миграционные инварианты.
- `startup_db_guard.py`, `db_lifecycle.py`, `db_availability.py` - проверки старта, доступность БД, recovery/rotation logic.
- `updater_main.py`, `update_package.py`, `version.py` - обновление, manifest, версия приложения.

### data

`data/` - слой данных:

- `data/dao/` - DAO для таблиц: пациенты, назначения, виталы, баланс, статусы, ИВЛ, процедуры, анализы, settings.
- `data/dto/` - dataclass/DTO-объекты, которыми обмениваются сервисы и UI.
- `data/settings/` - schema/import/release snapshot для центральной settings DB.
- `data/dictionaries/` - seed-справочники для первого импорта в settings DB.
- `data/mkb/` - локальная база/ресурсы МКБ.
- `data/patient_assets/` - ресурсы, связанные с пациентами и UI.

### services

`services/` - бизнес-логика между UI и DAO:

- `remcard_facade.py` - главный фасад RemCard для карты пациента.
- `patient_service.py`, `patient_status_service.py` - пациенты, койки, статусы, исходы.
- `vital_service.py`, `fluid_service.py`, `order_service.py` - витальные функции, баланс, назначения.
- `read_coordinator.py`, `sync_coordinator.py`, `data_service.py` - снапшоты, refresh, синхронизация изменений.
- `operblock_service.py` и `operblock_*` - логика экстренной/плановой операционной.
- `procedures_service.py`, `procedures_print_service.py`, `lab_orders_service.py` - процедуры, печать, анализы.
- `settings/`, `mkb/`, `analytics/`, `patient_bed_management/` - настройки, МКБ, графики/аналитика и управление пациентами/койками.

### ui

`ui/` - весь PySide6-интерфейс:

- `main_window.py` - главное окно и переключение ролей.
- `doctor_view/` - рабочее место врача, W1-экран, карта пациента, назначения, архив.
- `nurse_view/` - рабочее место медсестры, назначения, выполнения, печать.
- `operblock_view/` - интерфейс экстренной и плановой операционной.
- `patient_bed_management/` - встроенное управление пациентами и койками.
- `rem_card_sectors/` - сектора реанимационной карты: виталы, баланс, печать, события, ИВЛ, анализы.
- `procedures/` - UI процедур.
- `shared/` - общие виджеты, диалоги, графики, overlays, helpers.
- `styles/` - темы, QSS, токены, стили компонентов.

### scripts и docs

- `scripts/` - проверки, benchmarks, release/patch build, backup validation, restore drill, network acceptance. Это техническая зона сопровождения проекта.
- `scripts/pyinstaller_hooks/` - локальные hooks для PyInstaller.
- `docs/` - регламенты и контекст: DB safety, обновления, acceptance, emergency mode, checkpoint для будущей разработки.
- `docs/assets/readme/` - реальные скриншоты, которые отображаются в README.

### Ресурсы и runtime-данные

- `settings/` - JSON seed/default-настройки, из которых создается settings DB.
- `icon/` - иконки приложения, кнопок и собранных EXE.
- `procedure_templates/` - шаблоны процедур.
- `Baza_rao3_jurnal/` - рабочая runtime-папка, которая в dev-режиме может появляться рядом с проектом, а в эксплуатации обычно находится в сетевой папке. Внутри нее живут `archiv/rao_journal.db`, `settings/remcard_settings.db`, `backups/`, `logs/`, `UPD/`, locks и recovery-файлы.

Основной поток такой: UI вызывает сервисы, сервисы работают через DAO, DAO пишет в SQLite через общий безопасный профиль и транзакции. Update/backup/recovery-скрипты живут отдельно в `scripts/` и документации, потому что любые изменения в БД и обновлениях считаются зоной повышенного риска.

## Честно о качестве

Этот репозиторий - полностью вайбкод-проект.

Создатель проекта вообще не владеет ни одним языком программирования; это его первый проект. Большая часть решений рождалась через итерации с AI, быстрые правки, эксперименты и попытки заставить задачу работать здесь и сейчас.

Проект начинался в VS Code + Cline на Gemini 3.1. Позже, после постоянных ошибок запуска, сломанных итераций и усталости от ручного разгребания проблем, дальнейшая разработка переехала в Codex-приложение и ChatGPT-чат.

Поэтому проект может содержать и, скорее всего, содержит:

- большое количество костылей;
- архитектурные компромиссы;
- дублирование логики;
- странные участки кода;
- устаревшие compatibility paths;
- ошибки, которые еще не найдены;
- решения, которые опытный разработчик сделал бы иначе.

При этом в проекте уже есть много защитных механизмов: regression checks, architecture checks, backup/restore-drill, update-регламенты, блокировка старых клиентов и документация по критичным инвариантам БД.

## Документация

Начинать лучше отсюда:

- [docs/README.md](docs/README.md) - карта документации.
- [docs/db_safety_contract.md](docs/db_safety_contract.md) - правила безопасности БД.
- [docs/versioning.md](docs/versioning.md) - версии, changelog и релизы.
- [docs/auto_update.md](docs/auto_update.md) - full/patch автообновление.
- [docs/operational_acceptance.md](docs/operational_acceptance.md) - приемочные проверки.
- [docs/project_checkpoint/00_MASTER_CONTEXT_FOR_CHATGPT.md](docs/project_checkpoint/00_MASTER_CONTEXT_FOR_CHATGPT.md) - большой архитектурный снимок.

## Связь

Если проект заинтересовал, можно написать на [menfise@mail.ru](mailto:menfise@mail.ru) с темой письма `РЕМ КАРТА`. Я с радостью расскажу, как это творение работает.

Если вы точно разбираетесь в таких проектах, нашли ошибки или знаете, как помочь отечественному здравоохранению, а конкретно нашему отделению реанимации, ускорить это творение, оптимизировать работу и при этом не сломать логику, я тоже буду рад помощи.

## Проверки

Минимальные команды, которые часто используются перед релизом или рискованными изменениями:

```powershell
python -m compileall app data services ui scripts
python scripts\architecture_safety_check.py
python scripts\regression_safety_checks.py
python scripts\code_quality_checks.py
python scripts\style_audit_check.py
python scripts\network_acceptance_runner.py --operations 24 --benchmark-clicks 3
python scripts\validate_backups.py --max-files 20 --move-invalid
python scripts\restore_drill.py --max-files 20 --cleanup-restored
```

Для документационных изменений тяжелые проверки обычно не нужны; достаточно `git diff --check` и визуальной проверки Markdown.

## Текущий статус

Проект живой и меняется. Основная ветка: `main`.

Стабильность отдельных частей разная: базовые сценарии активно доводятся, но код нельзя считать завершенным или надежным без проверки под конкретную среду. Любые изменения, связанные с БД, миграциями, recovery, update, оперблоком или аварийным режимом, нужно делать особенно осторожно.

## Лицензия и ответственность

Код распространяется под открытой лицензией [MIT](LICENSE).

Создатель проекта не несет никакой ответственности, если у пользователя что-то пошло не так: потерялись данные, сломалась база, приложение повело себя неправильно, были приняты неверные решения или возник любой другой ущерб. Любое использование - строго на свой страх и риск.

Если вы смотрите этот репозиторий как разработчик, относитесь к нему как к большому экспериментальному desktop-проекту, а не как к готовому медицинскому продукту.

Если вы смотрите его как пользователь, не используйте приложение для принятия медицинских решений без независимой проверки, тестирования, резервного процесса и ответственного контроля.
