from __future__ import annotations

import os

from PySide6.QtCore import QSize, QTimer
from PySide6.QtGui import QIcon
from PySide6.QtWidgets import QPushButton


def create_emergency_mode_button(panel):
    button = QPushButton(" Аварийный режим", panel)
    button.setObjectName("emergencyModeButton")
    button.setIcon(QIcon(os.path.join(panel.icon_dir, "emergency_mode.png")))
    button.setIconSize(QSize(18, 18))
    button.setMinimumHeight(32)
    # Подсказка дублировала назначение кнопки и появлялась при наведении на
    # аварийную панель. Оставляем доступное имя для клавиатуры/скринридеров,
    # но не показываем hover-tooltip.
    button.setToolTip("")
    button.setAccessibleName("Аварийный режим: завершение работы и перенос данных")
    button.hide()

    def activate():
        controller = getattr(panel.window(), "_emergency_workflow", None)
        if controller is not None:
            controller.begin_wait()

    button.clicked.connect(activate)
    return button


def sync_emergency_mode_button(panel):
    button = panel.btn_emergency_mode

    def sync_visibility():
        runtime = getattr(getattr(panel.window(), "container", None), "runtime_context", None)
        # Наличие в layout задаётся настройками роли. Отложенный вызов
        # проверяет актуальный layout, чтобы не включить уже скрытую кнопку.
        configured = panel.layout.indexOf(button) >= 0
        button.setVisible(configured and getattr(runtime, "mode", "") == "emergency")

    sync_visibility()
    QTimer.singleShot(0, panel, sync_visibility)
