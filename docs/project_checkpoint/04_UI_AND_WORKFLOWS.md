# UI and workflows

## 1. Врач: главный экран, W1, W1a, W1b

Entry: `run_doctor.py:14-18`.

Главный виджет: `DoctorMainWidget` (`ui/doctor_view/doctor_main_widget.py:60`). Он подключает `data_service.changes_detected`, обновляет W1 beds/W1a только когда видим и не открыта карта (`doctor_main_widget.py:72-190`).

Основная карта врача: `DoctorRemCardWidget` (`ui/doctor_view/doctor_remcard_widget.py:59`). Layout создаёт `DoctorRemCardWidget` и `RemCardLayoutManager` (`ui/doctor_view/doctor_main_widget.py:192-210`).

W1:

- `BedsSelectionWidget` создаётся в `ui/shared/remcard_layout.py:222-236`.
- W1 mode стартует сразу: selection stack index 1, W1b index 1 (`ui/shared/remcard_layout.py:295-300`).
- W1a: `SectorW1a(self.remcard_service, role="doctor")` (`ui/shared/remcard_layout.py:266-275`).
- W1b: `SectorW1b(role="doctor")` создаётся фабрикой (`ui/shared/layout_components.py:66-70`, `148`).

Данные:

- W1 beds: `RemCardService.build_beds_snapshot()` (`services/remcard_facade.py:204-227`) через `ReadCoordinator.load_beds_snapshot()` (`services/read_coordinator.py:735-757`) в widgets.
- W1a: `build_w1a_upcoming_orders_snapshot()` (`services/remcard_facade.py:1082-1100`).

Known risks: W1b doctor — placeholder (`ui/rem_card_sectors/sector_w1b.py:10-42`); если ожидается функциональный сектор, нужна проверка требований.

## 2. Медсестра: главный экран, W1, W1a, W1b

Entry: `run_nurse.py:14-18`.

Главный виджет: `NurseMainWidget` (`ui/nurse_view/nurse_main_widget.py:83`). Он держит snapshot worker/cache, add-patient lock, W1 refresh, sync handling (`nurse_main_widget.py:85-135`, `1035-1120`).

Layout: `NurseRemCardLayoutManager` (`ui/nurse_view/nurse_remcard_layout.py:14`). W1 mode стартует сразу (`nurse_remcard_layout.py:293-299`).

W1:

- `NurseBedsSelectionWidget` (`ui/nurse_view/nurse_remcard_layout.py:232-237`).
- W1a: `SectorW1a(self.remcard_service, role="nurse")` (`nurse_remcard_layout.py:264-270`).
- W1b nurse: `sector_w1b_nurse` в stack index 1 (`nurse_remcard_layout.py:274-278`), создаётся в `SectorFactory` (`ui/shared/layout_components.py:70-71`, `149`).

Known risks: W1b nurse — placeholder (`ui/rem_card_sectors/sector_w1b_nurse.py:10-42`).

## 3. Карточка пациента

Doctor:

- `DoctorRemCardWidget._request_card_snapshot()` создаёт `AsyncCallThread` (`ui/doctor_view/doctor_remcard_widget.py:529-561`).
- `_build_card_snapshot_job()` использует `ReadCoordinator.load_patient_vitals_snapshot()` или `load_patient_card_snapshot()`; fallback — `build_full_card_snapshot()` (`doctor_remcard_widget.py:567-625`).
- `_apply_card_snapshot()` проверяет request id, admission id, date, context key, dedup signature (`doctor_remcard_widget.py:627-680`).

Nurse:

- Аналогично в `NurseMainWidget._request_card_snapshot()` (`ui/nurse_view/nurse_main_widget.py:514-550`), `_build_card_snapshot_job()` (`577-635`), `_apply_card_snapshot()` (`637-704`).

Данные: `ReadCoordinator` + `RemCardService` snapshots (`services/read_coordinator.py`, `services/remcard_facade.py`).

Guards: stale snapshot discard, `_is_closing`, context hash, chart lazy init.

## 4. Сектора 1a/1b/2/4/ИВЛ/баланс/питание/события/статус

Сектора создаются `SectorFactory.create_all_sectors()` (`ui/shared/layout_components.py:40-149`).

Карта врача:

- `sector_1a`, `sector_1b`: left column; `sector_1b` получает `VitalsWidget` после lazy init (`ui/doctor_view/doctor_remcard_widget.py:1910-1920`).
- `sector_2v`: chart area, `ChartWidget` lazy (`doctor_remcard_widget.py:1871-1885`).
- `sector_2a/2b`: header/tabs in layout (`ui/shared/remcard_layout.py:70-114`).
- balance tab lazy через `SectorFactory.create_balance_sectors()` (`ui/shared/layout_components.py:24-37`) и layout ensure methods.
- IVL/procedures/analytics/print tabs are optional/lazy (`ui/shared/remcard_layout.py:95-128`).

Карта медсестры:

- nurse-specific sectors replace 2a/2b/4v/7na_b (`ui/nurse_view/nurse_remcard_layout.py:49-67`).
- orders tab lazy (`nurse_remcard_layout.py:115-118`, `446-460`).
- balance tab lazy (`nurse_remcard_layout.py:358-385`).
- events/proc/anal/print lazy (`nurse_remcard_layout.py:334-402`).

Snapshot/read-model:

- balance: `build_balance_snapshot()` (`services/remcard_facade.py:343-375`), `ReadCoordinator.load_balance_snapshot()` (`services/read_coordinator.py:607-633`).
- diet: `build_diet_snapshot()` (`services/remcard_facade.py:377-393`).
- status: `build_status_snapshot()` (`services/remcard_facade.py:321-341`).
- IVL: `build_ivl_snapshot()` (`services/remcard_facade.py:395-416`).

Known risks: many legacy inline styles remain but tracked by `scripts/style_audit_check.py` baseline.

## 5. PatientForm

Файл: `ui/patient_bed_management/patient_form.py`.

Показывает: данные пациента, госпитализацию, диагноз/МКБ, койку.

Открытие:

- `PatientBedManagementWidget._open_patient_card_by_number()` guards `_is_closing`, `_opening_patient_form`, active dialog (`ui/patient_bed_management/management_widget.py:165-169`);
- `QTimer.singleShot(0, ...)` defers open (`169`);
- `dialog.open()` nonblocking (`190-199`).

Запись:

- validate fields (`patient_form.py:211-228`);
- create/update operation with expected admission revision (`258-276`);
- pending state before enqueue (`278-292`);
- success callback accept after commit (`297-373`);
- error callback warning and controls restored (`375-387`).

Guards: reject ignored while pending (`patient_form.py:404-412`), MKB connection closed once (`389-403`), shutdown force close (`425-436`).

## 6. Управление койками

Файл: `ui/patient_bed_management/management_widget.py`, service `services/patient_bed_management/service.py`.

Показывает: grid коек (`NUM_BEDS`, default 12), side patient card, status FREE/occupied (`management_widget.py:37-72`, `350-375`).

Пишет:

- create/update admission through `PatientForm`;
- move/swap patients via `move_patient()` (`management_widget.py:268-337`).

Safety:

- expected source/target bed/admission revisions collected before move (`management_widget.py:281-284`);
- service checks revisions and bumps counters (`services/patient_bed_management/service.py:251-333`);
- pending disables bed widgets (`management_widget.py:340-348`).

## 7. Назначения

Doctor widget: `ui/doctor_view/orders_widget.py`. Nurse widget: `ui/nurse_view/components/nurse_orders_widget.py`.

Read:

- `ReadCoordinator.load_orders_tab()` (`services/read_coordinator.py:838-1079`);
- `RemCardService.build_orders_snapshot()` (`services/remcard_facade.py:478-536`).

Write:

- `OrderService` with expected revisions (`services/order_service.py:63-121`, `378-486`, `505-522`, `599-674`);
- UI collects visible order revision maps (`ui/doctor_view/orders_widget.py:1972-2027`, `2605-2664`).

Guards:

- stale snapshot blocked if snapshot change id older than known change id (`ui/doctor_view/orders_widget.py:1007-1033`, nurse `ui/nurse_view/components/nurse_orders_widget.py:692-718`);
- local forced orders payload skips full card snapshot (`doctor_remcard_widget.py:1081-1097`, `nurse_main_widget.py:1074-1090`).

## 8. Выполнение назначений

Выполнения хранятся в `administrations` (`app/unified_db_schema.py:859-871`) с `version`.

Nurse execution logic:

- `services/order_domain_service.py:877-934` проверяет текущую version через `_assert_nurse_admin_current()`, затем `UPDATE ... WHERE id=? AND version=?`, rowcount !=1 -> conflict.
- W1a snapshot возвращает `expected_revision=a.version` and `order_revision=o.revision` (`services/order_domain_service.py:1039`).

UI: nurse orders widget and W1a cards. Exact click handlers требуют отдельной точечной проверки, но conflict/version path подтверждён.

## 9. Витальные

UI: `ui/shared/vitals_widget.py`, chart: `ui/shared/chart_widget.py`.

Read:

- `build_vitals_snapshot()` (`services/remcard_facade.py:234-292`);
- `ReadCoordinator.load_patient_vitals_snapshot()` (`services/read_coordinator.py:412-522`).

Write:

- `VitalService.add_vital()` and `delete_last_vital()` pass `expected_revision` (`services/vital_service.py:103-127`);
- DAO checks revision and rowcount (`data/dao/vitals_dao.py:14-75`, `237-245`).

Chart:

- lazy init in role widgets;
- clear on context change via `ChartWidget.clear_for_context()` (`ui/shared/chart_widget.py:492-517`);
- render dedupe key methods (`chart_widget.py:518-560+`), role-level signature dedupe (`doctor_remcard_widget.py:1887-1908`).

## 10. Баланс

Data:

- fluids table with `revision` (`app/unified_db_schema.py:803-818`);
- DAO optimistic lock (`data/dao/fluids_dao.py:26-56`);
- `build_balance_snapshot()` (`services/remcard_facade.py:343-375`);
- `ReadCoordinator.load_balance_snapshot()` (`services/read_coordinator.py:607-633`).

UI:

- balance sectors lazy via `SectorFactory.create_balance_sectors()` (`ui/shared/layout_components.py:24-37`);
- partial refresh via `_refresh_balance_from_db()` (`ui/doctor_view/doctor_remcard_widget.py:904-956`, nurse `ui/nurse_view/nurse_main_widget.py:928-938`).

Known risk: UI print data calls private `oral_service._calculate_totals()` (`ui/rem_card_sectors/s_print/full_report_data.py:129`) — P2/нужна проверка.

## 11. Питание

Tables:

- `diet_templates.version`, `diet_plan.version`, `oral_intake_events.version` (`app/unified_db_schema.py:937-981`).

Services/DAO:

- `services/diet_service.py` uses expected_version and raises `OptimisticLockError` (`services/diet_service.py:290-629`);
- `data/dao/diet_dao.py` rowcount conflict checks (`data/dao/diet_dao.py:71-111`, `144-214`, `278-345`).

Snapshot:

- `build_diet_snapshot()` (`services/remcard_facade.py:377-393`);
- `ReadCoordinator.load_diet_snapshot()` (`services/read_coordinator.py:635-658`).

## 12. ИВЛ

Tables:

- `ivl_episodes.revision`, `clinical_events.revision`, `devices`, `respiratory_support` (`app/unified_db_schema.py:609-647`, `1014-1024`).

DAO:

- `VentilationDAO.assert_case_revision()` and `assert_event_revision()` (`data/dao/ventilation_dao.py:269-281`).

Snapshot:

- `build_ivl_snapshot()` (`services/remcard_facade.py:395-416`);
- partial refresh via `_refresh_ivl_from_db()` (`doctor_remcard_widget.py:970-976`, nurse `nurse_main_widget.py:952-958`).

## 13. Архив

UI:

- `ArchiveWidget` (`ui/doctor_view/archive_widget.py:19`);
- layout archive view (`ui/shared/remcard_layout.py:239-243`, `534-612`; nurse `ui/nurse_view/nurse_remcard_layout.py:239-255`, `507-572`);
- doctor opens external archive via read-only service (`ui/doctor_view/doctor_remcard_widget.py:1233-1262`, `2578-2603`).

Read-only DB:

- `ArchiveReadOnlyDatabaseManager` opens `file:path?mode=ro` and `configure_connection(readonly=True)` (`services/archive_readonly_service.py:22-43`);
- write methods raise `ReadOnlyArchiveDbError` (`services/archive_readonly_service.py:132-141`);
- `create_archive_readonly_service()` wires DAOs + `ReadCoordinator` in archive mode (`services/archive_readonly_service.py:148-173`).

Known risk: archive deletion actions exist in `ArchiveWidget` (`ui/doctor_view/archive_widget.py:383`, `417`) and should be reviewed before changes.

## 14. Аналитика/графики/PDF

Analytics:

- readonly analytics managers: `services/analytics/multi_db_analytics.py:68-125`;
- dialogs: `ui/analytics/report_dialog.py`, `statistics_dialog.py`, `graphs_dialog.py`;
- graph build: `services/analytics/graphs_service.py:27-100`.

Workers:

- `AnalyticsWorker` (`ui/shared/analytics_worker.py:6-32`);
- `HtmlPdfWorker` (`ui/shared/html_pdf_worker.py:9-40`);
- `PdfBuildWorker` (`ui/shared/pdf_build_worker.py:10-29`).

PDF reports:

- `RemCardReportController` (`ui/shared/report_controller.py:14-240`);
- print sectors also have report workers (`ui/rem_card_sectors/sector_print.py`, `ui/nurse_view/sectors/nurse_sector_print.py`).

## 15. Настройки/тема

Theme:

- `ThemeStorage` path/load/save/quarantine (`ui/styles/theme_storage.py:23-118`);
- `ThemeManager` apply/save (`ui/styles/theme_manager.py:17-133`, `300-341`);
- built-in presets: `remcard_light`, `remcard_dark` only (`ui/styles/theme_presets.py:19-35`);
- settings dialog: `ui/styles/theme_settings_dialog.py:100-132`, save/reset `1085-1105`.

Display settings:

- `DisplaySettingsStorage` (`ui/shared/display_settings_storage.py:14-18`, `83-120`, `277-321`);
- W1a/W1b enabled flags (`display_settings_storage.py:261-274`).

## 16. Мини-игры

UI:

- `ui/shared/minigames/bonus_dialog.py`;
- arcade/snake/user/leaderboard widgets under `ui/shared/minigames/`.

Services:

- `services/minigames/minigame_paths.py` stores data under data root `minigames` (`services/minigames/minigame_paths.py:25-33`);
- user/score stores in `services/minigames/`.

Style audit allows minigame-specific inline style baseline (`scripts/style_audit_check.py:120-153`).
