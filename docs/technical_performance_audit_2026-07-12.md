# Технический аудит производительности RemCard

Дата: 2026-07-12. Объект: текущее рабочее дерево ветки `test1`, включая незакоммиченные изменения (50 изменённых файлов, 5444 добавления, 964 удаления и новые файлы). Исходный код в ходе аудита не изменялся; этот документ является единственным добавленным артефактом.

## 1. Краткое резюме

Текущая архитектура в целом правильно отделяет UI, services, DAO и SQLite. Записи проходят через очередь и `SQLiteWriteController`, чтение карточек — через snapshot/read coordinator, тяжёлая аналитика имеет worker-обвязку. Architecture gates проходят 8/8, quality gates 3/3, unit/UI tests 241/241.

Основные подтверждённые проблемы:

1. **Critical — неполный журнал подтверждённой offline/remote-записи.** Изолированный acceptance-тест стабильно не находит `remote_committed` после подтверждённого commit для зарегистрированного `operation_uuid`. Это создаёт неоднозначность после аварии и запрещает ускорять/coalesce этот путь до восстановления полного автомата состояний.
2. **High — блокировка Qt event loop до 1200 мс при остановке snapshot worker медсестры.** `worker.wait(1200)` вызывается из UI-метода.
3. **High — повторное действие в таблице назначений искусственно ждёт в среднем 419,2 мс, максимум 480,1 мс.** Сама optimistic UI-операция быстрая (p95 0,585 мс), commit p95 19,053 мс; воспринимаемая задержка формируется guard-окном.
4. **Medium — cold orders snapshot делает последовательные обращения к SQLite.** На измеренном открытии: 34,67 мс, включая 4,75 + 5,98 + 4,99 + 6,69 мс; до загрузки дважды логируется одинаковый miss lookup.
5. **Medium — mutating path графика витальных функций остаётся выше одного кадра.** 27,278 мс/итерацию после оптимизации, хотя same-data polling снижен до 0,542 мс.

Пять наиболее выгодных безопасных действий: восстановить доказуемый переход journal `pending → remote_committed`; убрать UI `wait`; заменить временной repeat guard на per-cell pending/operation UUID; собрать orders snapshot в одном read scope/запросе без повторного lookup; обновлять график инкрементально и только для актуального видимого контекста.

## 2. Архитектурная карта

Общий запуск:

`run_<role>.py → app.main.main(forced_role) → paths/runtime context → startup guard → quick_check/client policy → bootstrap → DatabaseManager → schema fastpath/migration guard → services/DataService/ReadCoordinator → MainWindow → role widget → initial snapshot → DataUpdateMonitor/background tasks`

Роли:

- врач: `run_doctor.py → app.main → bootstrap → MainWindow → DoctorMainWidget → lightweight shell → lazy DoctorRemCardWidget → ReadCoordinator`;
- медсестра: `run_nurse.py → app.main → bootstrap → MainWindow → NurseMainWidget → snapshot worker → nurse layout`;
- оперблок: `run_operblock_{emergency,planned}.py → emergency/network startup selection → bootstrap → OperBlockMainWidget → board snapshot → table cards → standby/offline services`.

Основные пользовательские цепочки:

- открытие карточки: `role widget → request/generation context → ReadCoordinator cache → RemCardService snapshot → DAO reads → queued result → context/request guard → apply`;
- вкладка: `tab click → lazy component/show handler → scoped snapshot worker → guard → incremental apply`;
- запись: `UI pending state → DataService.enqueue_write → LocalWriteQueue → FileWriteLock → BEGIN IMMEDIATE → DAO → COMMIT → queued Qt success → cache invalidation/change_log`;
- межклиентское обновление: `SQLite trigger → change_log → DataUpdateMonitor (2 s default) → SyncCoordinator.classify → targeted/full refresh`;
- offline оперблок: `write intent(operation_uuid,payload,state) → local/remote transaction → journal state → retry/migration → verified cleanup`.

Защиты, которые нельзя ослаблять: network SQLite `journal_mode=DELETE`, `synchronous=EXTRA`, file write lock, transaction rollback, operation UUID, revision/request/generation guards, verified offline retention, backup-before-migration.

## 3. Таблица проблем

| ID | Приоритет | Подсистема | Файлы/функции | Сценарий и доказательство | Текущее время | Риск | Минимальное решение | Ожидаемый выигрыш | Риск изменения | Проверка |
|---|---|---|---|---|---:|---|---|---:|---|---|
| PERF-001 | Critical | sync/offline | `services/data_service.py`, offline journal; `scripts/operblock_offline_acceptance_runner.py:_scenario_precommit_journal_runtime_drop` | Acceptance: есть `opblock_write_intent` с `remote_commit_state=pending`, но после подтверждённого commit нет `remote_committed`; 11/12 сценариев passed | функциональный дефект | неоднозначный retry после crash, дубль либо незавершённая синхронизация | атомарно/достоверно записывать подтверждение после commit; восстановление должно сверять operation UUID с удалённым состоянием | не про latency; снимает блокер оптимизации | High | crash между intent/commit/ack; повторный старт; один UUID — одна операция; очередь полностью пуста |
| PERF-002 | High | UI/workers | `ui/nurse_view/nurse_main_widget.py:_shutdown_snapshot_worker` (515–524) | UI вызывает `worker.quit(); worker.wait(1200)` | 0–1200 мс | UI freeze, зависание закрытия/переключения | disconnect + stale generation; асинхронное завершение, финальное ожидание только вне UI; не отменять committed write | до 1200 мс freeze | Medium | worker с искусственной задержкой; UI heartbeat; закрытие/смена пациента; callback после destroy не применяется |
| PERF-003 | High | UI/orders | `ui/doctor_view/orders_widget.py`, repeat guard; `scripts/orders_click_latency_benchmark.py` | 30 кликов: UI p95 0,585 мс, DB p95 19,053 мс, ожидание guard avg 419,228, max 480,104 мс | ~0,42–0,48 с | ощущение неотзывчивости; повторный клик может быть отброшен | per-cell pending state + operation sequence/UUID; принимать следующее действие после ack либо безопасно сериализовать | ~400 мс на повтор | Medium | 100 быстрых кликов; отсутствие дублей; итоговое состояние равно последовательности подтверждённых действий |
| PERF-004 | Medium | SQLite/orders | `services/remcard_facade.py` orders snapshot, `services/read_coordinator.py` | cold open: snapshot 34,67 мс; SQL steps 4,75/5,98/4,99/6,69 мс; одинаковый cache miss зарегистрирован дважды | 34,67 мс | >16 мс, лишние сетевые round trips | единый readonly scope; объединить совместимые запросы; один cache lookup; не менять payload | 10–20 мс локально, больше на SMB | Low/Medium | query-count assertion; EXPLAIN; равенство snapshot/hash; p95 на сетевой копии |
| PERF-005 | Medium | graphics | `ui/shared/chart_widget.py`, `scripts/vitals_pipeline_benchmark.py` | 220 точек; same-data 0,542 мс/iter, mutating 27,278 мс/iter | 27,278 мс | пропуск кадров при серии событий | `setData`/delta, reuse items, visible-tab gate, generation guard | 8–15 мс/обновление | Medium | pixel/data equality; patient switch during worker; 320 repeat/80 mutate iterations |
| PERF-006 | Medium | startup/SQLite | `data/dao/db_manager.py`, schema/quick check | свежая temp DB при инициализации последовательно добавляет 13 legacy columns, затем opblock migration и validated backup; это корректно только для первого migration | в acceptance отдельные старты ~0,1–0,2 с локально; network p95 не снят | холодный старт на SMB; I/O конкуренция | сохранять schema fastpath; доказать, что PRAGMA/table_info и migration не повторяются на готовой схеме | зависит от SMB | Medium | два последовательных старта; второй: zero DDL/backup, одинаковая schema version |
| PERF-007 | Medium | shutdown/SQLite | `data/dao/db_manager.py:close` | offline acceptance наблюдал shutdown 167,7 и 190,3 мс против обычных 2–4 мс | max 190,3 мс | задержка закрытия, конкуренция с фоновой I/O | трассировать владельца central I/O lock; stop schedulers → drain writes → close; backup не в UI | до ~180 мс | Medium | p50/p95 30 закрытий, активная maintenance/write, queue drained |
| PERF-008 | Low | observability | benchmark suite | startup benchmark агрегирует median/max, но не p95; обязательные 18 сценариев не имеют единого harness и общей схемы SQL/busy/queue metrics | неполный baseline | ложные выводы по единичному прогону | единый scenario runner, ≥30 прогонов, UI heartbeat, SQL trace counters, queue gauges | ускоряет диагностику | Low | self-test runner и стабильный JSON schema |

## 4. Измеренный baseline

Текущий изолированный прогон (Windows, Python 3.11, 2026-07-12):

| Сценарий | Результат |
|---|---:|
| Vitals same-data polling, 320 итераций | 173,47 мс total; 0,5421 мс/iter; улучшение встроенного before/after 93,26% |
| Vitals mutating, 80 итераций | 2182,21 мс total; 27,2777 мс/iter; улучшение 19,41% |
| Orders optimistic UI, 30 кликов | avg 0,499; p95 0,585; max 0,609 мс |
| Orders commit, 30 кликов | avg 14,408; p95 19,053; max 20,338 мс |
| Orders repeat guard | avg 419,228; max 480,104 мс |
| Cold orders snapshot (наблюдённый прогон) | 34,67 мс |
| Offline acceptance | 11 passed, 1 failed (`precommit_journal_runtime_drop`) |
| Unit/UI tests | 241 passed / 13,80 с |
| Architecture checks | 8/8 / 0,84 с |
| Quality checks | 3/3; новых complexity-F блоков нет |
| compileall | passed |

Исторический baseline от 2026-05-04 (`docs/performance_a_baseline.md`) нельзя считать текущим before: full card 125,058 мс; balance 83,778; vitals 52,078; card cache hit 7,168; warm reopen 19,939; Orders tab 4,330 return / 25,159 done; commit p95 66,263 мс. Текущий orders commit p95 19,053 мс на другой локальной среде, поэтому это индикатор, а не доказанное production-ускорение.

Не измерены безопасно на production-подобной сетевой копии: cold/warm полный запуск всех ролей, p95 открытия/переключения пациентов и столов, все тяжёлые вкладки, память после циклов, outage >5 минут, восстановление сети, crash-after-send-before-ack. Для них нельзя указывать выдуманные миллисекунды.

## 5. Критический путь запуска

`entrypoint → импорт app.main/PySide6 → resolve paths → role/single-instance locks → startup DB guard → readonly quick_check/client policy → bootstrap → DatabaseManager/schema fastpath → settings → services → MainWindow → lightweight role shell → first snapshot → board/card ready → monitors`

В текущем коде уже присутствуют правильные оптимизации: lazy full layout, persistent readonly polling connection для doctor/nurse, async snapshot writer, schema/index caches, lazy icon loader/LRU и W1 handoff. Их эффект нужно отделить от незакоммиченной серии изменений через чистый before commit/worktree и одинаковую копию БД.

На критическом пути запрещено переносить safety-решение в optimistic UI: quick check/migration могут быть background только при явном состоянии «не готово»; окно не должно принимать медицинскую запись до готовности write path.

## 6. Критический путь записи и offline-sync

Обычный путь:

`UI pending → enqueue_write → LocalWriteQueue → foreground/write lease → file lock → BEGIN IMMEDIATE → DAO SQL → COMMIT → Qt success → cache epoch/invalidation → change_log → monitor refresh`

Offline/remote путь должен иметь автомат состояний:

`intent(operation_uuid,payload,pending) durable → local commit → remote attempt → remote dedupe/revision check → remote commit → durable remote_committed → UI success/cleanup`

Если сеть оборвалась после remote commit, но до ack, retry обязан сначала проверить UUID/эквивалентный receipt. Удалять intent можно только после durable confirmation. Найденный PERF-001 означает, что этот инвариант сейчас не доказан и acceptance его опровергает для одного сценария.

## 7. Карта блокировок и фоновых задач

| Участники | Общий ресурс | Конфликт | Имеющаяся защита | Остаточный риск |
|---|---|---|---|---|
| LocalWriteQueue / DAO writers | network SQLite + file lock | параллельные commits | `SQLiteWriteController`, `BEGIN IMMEDIATE`, retry | SMB latency и длинный владелец lock |
| DataUpdateMonitor (2 s) / foreground read-write | SQLite/read handle | polling round trips | отдельный QThread, persistent readonly connection | maintenance вызывается перед poll; проверить foreground lease для каждой задачи |
| backup/integrity/migration / user write | disk + DB lock | I/O и exclusive/write contention | maintenance activity, central I/O lock, migration backup | shutdown max 190 мс; network p95 неизвестен |
| snapshot/chart workers / patient switch | CPU/result context | stale apply | request/generation/context hash | UI `wait(1200)` в nurse shutdown |
| offline migration / active local writes | local+remote DB | порядок и двойная доставка | UUID/journal/verified retention | отсутствует подтверждающее journal event в одном crash-сценарии |

## 8. Память и lifecycle

Статически подтверждены disconnect snapshot/monitor signals и cache invalidation/context hashes. Однако количественный leak baseline отсутствует. Нужен автоматический цикл не менее 100 раз: открыть/закрыть пациента, doctor↔nurse role widget, tabs, стол, offline→online. Метрики: RSS, Python tracemalloc, число `QWidget/QTimer/QThread`, SQLite handles, cache entries после GC/event drain. Критерий: после прогрева линейный slope статистически не отличается от нуля; все worker finished/deleted; timers stopped; connection count возвращается к baseline.

## 9. План оптимизации

### Stage 0 — воспроизводимый baseline

- Файлы: benchmark scripts и отдельные test fixtures.
- Цель: 18 обязательных сценариев, 30+ повторов, median/p95/max, UI heartbeat, SQL/busy/retry/queue counters.
- Риск: Low. Откат: удалить только instrumentation.
- Gate: одинаковая обезличенная production-size копия, cold/warm разделены.

### Stage 1 — целостность offline journal

- Файлы: `services/data_service.py`, offline journal/store/migration tests.
- Цель: закрыть PERF-001 без изменения схемы/протокола, если возможно.
- Риск: High. Откат: обязательный до изменения state machine.
- Gate: все 12 acceptance плюс crash matrix, UUID dedupe, zero loss/duplicates.

### Stage 2 — убрать ожидание worker из UI

- Файлы: `ui/nurse_view/nurse_main_widget.py`, общий async worker lifecycle.
- Цель: max event-loop pause <50 мс при switch/close.
- Риск: Medium: callback-after-destroy.
- Gate: generation guard, deleteLater, delayed worker test.

### Stage 3 — orders interaction/read path

- Файлы: orders widget, `read_coordinator.py`, `remcard_facade.py`, DAO.
- Цель: repeat action <100 мс после ack; cold snapshot <20 мс локально и меньше SMB round trips.
- Риск: Medium: порядок кликов/stale snapshot.
- Gate: rapid-click state model, query count, snapshot equality, revision conflicts.

### Stage 4 — графики

- Файлы: chart widget/data processor/generators.
- Цель: mutating p95 <16 мс при 220+ точках.
- Риск: Medium: неполная серия/другой пациент.
- Gate: full-vs-incremental equality, hidden tab, cancellation, time conversion.

### Stage 5 — startup и maintenance

- Файлы: bootstrap, DB manager, startup guard, schema caches.
- Цель: готовая schema выполняет zero DDL/backup; maintenance не конкурирует с foreground lease.
- Риск: High для DB guards.
- Gate: migration/recovery/backup regression, network latency, corruption/locked classified separately.

### Stage 6 — lifecycle и release candidate

- Цель: 100-cycle memory test, multi-client stress, outage/recovery, crash restart, shutdown p95.
- Gate выпуска: p95 не хуже baseline; busy/locked не выросли; очередь заканчивается нулём; zero loss/duplicates/stale apply; все существующие gates green.

## 10. Заключение о готовности

Безопасны для следующей реализации после отдельного baseline: устранение UI `wait`, удаление двойного cache lookup, объединение read-only orders запросов в один scope и инкрементальное обновление графика с существующим generation guard.

Не готовы к выпуску/оптимизации: любые изменения coalescing, cleanup, optimistic success или порядка offline-sync, пока PERF-001 не исправлен и не пройдена crash matrix. Также нельзя объявлять общий выигрыш запуска: актуальный cold/warm p95 на production-size сетевой копии ещё не получен.

Текущий вердикт: архитектурная база и тестовые gates сильные, интерактивный orders UI и same-data chart path быстрые, но release-candidate оптимизации блокируются дефектом подтверждающего offline-журнала и отсутствием единого production-like p95 baseline для обязательных сценариев.
