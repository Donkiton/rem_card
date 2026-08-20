# Performance A baseline

Дата фиксации: 2026-05-04.

Среда замера:
- БД: `\\fs.acrb-amursk.ru\common\РАО\Пациенты\remcard\copy`
- `REMCARD_LOCAL_FIRST_SYNC=0`
- `REMCARD_LOCAL_OUTBOX_SYNC=0`
- local metrics: buffered async mode
- SQLite network profile не менялся

Контрольные значения после Performance A:

| Сценарий | Baseline |
| --- | ---: |
| `build_full_card_snapshot` | 125.058 ms |
| `build_balance_snapshot` | 83.778 ms |
| `build_vitals_snapshot` | 52.078 ms |
| cache-hit карточки через coordinator | 7.168 ms |
| warm reopen карточки | 19.939 ms |
| открытие вкладки "Назначения" | 4.330 ms return / 25.159 ms done |
| `orders_clear_drafts` при обычном открытии | `writes: []` |
| быстрые X, UI avg / p95 | 1.169 ms / 1.769 ms |
| быстрые X, DB commit avg / p95 | 57.563 ms / 66.263 ms |

Цели Performance B:

| Сценарий | Цель |
| --- | ---: |
| `build_full_card_snapshot` | < 90 ms |
| `build_balance_snapshot` | < 65 ms |
| `build_vitals_snapshot` | < 45 ms |
| cache-hit карточки | < 5 ms |
| warm reopen карточки | < 30 ms |

Инварианты для следующих этапов:
- не менять `journal_mode=DELETE`;
- не менять `synchronous=EXTRA`;
- не включать WAL для сетевой БД;
- не менять медицинскую бизнес-логику;
- не отключать диагностику;
- не возвращать синхронную запись local metrics в read hot path.
