# Сетевой стресс-тестовый стенд

`scripts/network_acceptance_runner.py` имитирует сетевую папку локальной временной директорией:

- создает временную `Baza_rao3_jurnal`;
- изолирует `LOCALAPPDATA`, локальные логи и cache;
- создает активную госпитализацию;
- запускает `multi_client_stress.py` как второй процесс;
- проверяет `quick_check` и `integrity_check` до и после;
- опционально запускает UI click benchmark в offscreen Qt.

## Быстрый прогон

```powershell
python scripts\network_acceptance_runner.py --operations 10 --benchmark-clicks 1
```

## Приемочный прогон

```powershell
python scripts\network_acceptance_runner.py --operations 120 --benchmark-clicks 5 --timeout-s 300
```

## Что проверяется

- врач-писатель меняет назначения и administrations;
- медсестринский читатель видит изменения по `change_log`;
- writer не получает lock/error;
- reader не отстает дольше `sync-timeout-s`;
- БД остается валидной после нагрузки.

## Отдельные сценарии, закрытые regression suite

- параллельный старт двух клиентов;
- старый клиент после новой схемы;
- падение миграции;
- невалидный backup;
- второй клиент держит активный role/session lock;
- `db.lock` занят;
- `recovery.lock` занят;
- БД locked, но не corrupt;
- corrupt latest backup и выбор следующего valid backup.
