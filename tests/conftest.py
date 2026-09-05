"""Изоляция Qt-окон между тестами без запуска приложения для обычных тестов."""

from __future__ import annotations

import gc
import sys

import pytest


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
