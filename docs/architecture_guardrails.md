# Архитектурные автозапреты

Автопроверка:

```powershell
python scripts\architecture_safety_check.py
```

Проверка падает, если возвращаются опасные паттерны:

- `sqlite3.connect` в UI;
- write/DDL SQL из UI;
- `DataService.run_write` из UI;
- доступ UI к private service fields;
- обход SQLite Backup API для живой БД;
- изменение network SQLite profile.

Зафиксированные инварианты network profile:

- `journal_mode = DELETE`;
- `synchronous = EXTRA`;
- `mmap_size = 0`.

Исключения допустимы только в инфраструктурных модулях, где они уже оформлены как единый безопасный путь: `sqlite_shared.py`, startup recovery и updater.
