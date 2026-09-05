"""Изоляция Qt-окон между тестами без запуска приложения для обычных тестов."""

from __future__ import annotations

import gc
import json
import os
import sys
from pathlib import Path

import pytest

from scripts.ci.test_groups import PROJECT_ROOT, load_groups, test_area


def pytest_addoption(parser):
    parser.addoption("--ci-group", choices=("core", "ui"), help="Изолированная группа CI")
    parser.addoption("--collection-report", help="JSON со всеми выбранными идентификаторами тестов")


def pytest_configure(config):
    try:
        config.remcard_test_groups = load_groups()
    except ValueError as exc:
        raise pytest.UsageError(str(exc)) from exc

    # Настройки CI существуют только внутри отдельной временной папки runner.
    # В обычном локальном pytest не меняем политику пользовательских настроек.
    settings_dir = os.environ.get("REMCARD_CI_SETTINGS_DIR")
    if settings_dir:
        from PySide6.QtCore import QSettings

        QSettings.setDefaultFormat(QSettings.IniFormat)
        for scope in (QSettings.UserScope, QSettings.SystemScope):
            QSettings.setPath(QSettings.IniFormat, scope, settings_dir)


def pytest_ignore_collect(collection_path, config):
    selected = config.getoption("--ci-group")
    if selected is None or not collection_path.is_file():
        return None
    try:
        relative = collection_path.relative_to(PROJECT_ROOT).as_posix()
    except ValueError:
        return None
    group = config.remcard_test_groups.get(relative)
    return group is not None and group != selected


def pytest_collection_modifyitems(config, items):
    for item in items:
        relative = item.path.relative_to(PROJECT_ROOT).as_posix()
        group = config.remcard_test_groups.get(relative)
        if group:
            item.add_marker(getattr(pytest.mark, group))
            item.add_marker(getattr(pytest.mark, test_area(relative)))
    target = config.getoption("--collection-report")
    if target:
        path = Path(target)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps([item.nodeid for item in items], indent=2), encoding="utf-8")


@pytest.fixture(autouse=True)
def cleanup_qt_test_windows():
    # Не импортируем/не создаём QApplication ради тестов без интерфейса.
    qt_widgets = sys.modules.get("PySide6.QtWidgets")
    app = qt_widgets.QApplication.instance() if qt_widgets is not None else None
    existing = list(app.topLevelWidgets()) if app is not None else []
    existing_ids = {id(widget) for widget in existing}
    yield

    qt_widgets = sys.modules.get("PySide6.QtWidgets")
    app = qt_widgets.QApplication.instance() if qt_widgets is not None else None
    if app is None:
        return

    from PySide6.QtCore import QCoreApplication, QEvent
    from shiboken6 import isValid

    # close() лишь скрывает окно, а processEvents() не гарантирует удаление.
    # Не вызываем closeEvent: он может сохранять данные или открывать диалог.
    # Сохраняем окна, созданные общими фикстурами до начала этого теста.
    windows = [widget for widget in app.topLevelWidgets() if id(widget) not in existing_ids]
    if windows:
        try:
            # Нулевые singleShot/отложенная инициализация требуют живого окна.
            app.processEvents()
        finally:
            for widget in app.topLevelWidgets():
                if id(widget) not in existing_ids and isValid(widget):
                    widget.deleteLater()
            QCoreApplication.sendPostedEvents(None, QEvent.DeferredDelete)
    QCoreApplication.sendPostedEvents(None, QEvent.DeferredDelete)
    # Python/Qt-циклы собираются здесь, пока жив GUI-поток, а не случайно
    # при очередном выделении памяти в фоновом потоке следующего теста.
    gc.collect()
    QCoreApplication.sendPostedEvents(None, QEvent.DeferredDelete)
