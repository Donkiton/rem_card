"""Политика включения темы без загрузки Qt и хранилища настроек."""
import os


FULL_RUNTIME_THEME_ENV = "REMCARD_FULL_RUNTIME_THEME"
_FALSE_VALUES = {"0", "false", "no", "off"}


def full_runtime_theme_enabled() -> bool:
    """Тема включена, если аварийный флаг явно её не отключает."""
    return str(os.environ.get(FULL_RUNTIME_THEME_ENV, "1")).strip().lower() not in _FALSE_VALUES
