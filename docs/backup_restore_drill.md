# Регламент backup / restore-drill

Наличие backup не считается достаточным. Backup считается рабочим только после тестового восстановления.

## Проверка backup

```powershell
python scripts\validate_backups.py --max-files 20 --move-invalid
```

## Restore-drill

```powershell
python scripts\restore_drill.py --max-files 20
```

Скрипт:

- выбирает последнюю валидную копию;
- восстанавливает ее в отдельную тестовую папку;
- запускает `quick_check`;
- запускает `integrity_check`;
- пишет JSON-отчет в `backup_health/reports`.

## Поля отчета

- `restore_drill_result`
- `last_successful_restore_test_at`
- `backup_source_path`
- `restored_db_path`
- `restored_db_hash`
- `validation_status`

## Запрещено

- заменять живую БД простым copy;
- использовать backup, который не прошел validation;
- делать restore при активном втором клиенте без ручного решения ответственного.
