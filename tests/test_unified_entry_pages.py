from __future__ import annotations

import os
import sys
from pathlib import Path


os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
PROJECT_DIR = Path(__file__).resolve().parents[1]
PACKAGE_PARENT = PROJECT_DIR.parent
if str(PACKAGE_PARENT) not in sys.path:
    sys.path.insert(0, str(PACKAGE_PARENT))

from PySide6.QtCore import QPoint
from PySide6.QtGui import QFontDatabase, QImage, QPainter
from PySide6.QtWidgets import QApplication

from rem_card.app.roles import (
    ROLE_DOCTOR,
    ROLE_NURSE,
    ROLE_OPERBLOCK_EMERGENCY,
    ROLE_OPERBLOCK_PLANNED,
)
from rem_card.ui.shared.unified_entry_pages import StartupPage, WelcomePage


def application() -> QApplication:
    app = QApplication.instance() or QApplication([])
    font_path = Path("C:/Windows/Fonts/segoeui.ttf")
    if font_path.is_file():
        QFontDatabase.addApplicationFont(str(font_path))
    return app


def _render(widget, path: Path) -> QImage:
    widget.resize(1366, 768)
    widget.show()
    application().processEvents()
    image = QImage(widget.size(), QImage.Format_ARGB32_Premultiplied)
    image.fill(0)
    painter = QPainter(image)
    widget.render(painter, QPoint())
    painter.end()
    assert image.save(str(path))
    return image


def test_welcome_page_exposes_all_roles_actions_and_access_state(tmp_path):
    app = application()
    page = WelcomePage()
    page.set_institution("Краевая клиническая больница", "ККБ")
    roles = []
    page.role_selected.connect(roles.append)
    actions = []
    page.settings_requested.connect(lambda: actions.append("settings"))
    page.about_requested.connect(lambda: actions.append("about"))
    page.update_requested.connect(lambda: actions.append("update"))

    expected_roles = {
        ROLE_DOCTOR,
        ROLE_NURSE,
        ROLE_OPERBLOCK_EMERGENCY,
        ROLE_OPERBLOCK_PLANNED,
    }
    assert set(page.role_buttons) == expected_roles
    page.role_buttons[ROLE_DOCTOR].click()
    page.settings_button.click()
    page.about_button.click()
    page.set_update_available("3.1.0")
    page.update_button.click()
    assert roles == [ROLE_DOCTOR]
    assert actions == ["settings", "about", "update"]
    assert page.context_label.text() == "ОАРИТ"
    assert page.hospital_label.text() == "ККБ"
    assert not page.hospital_separator.isHidden()
    assert page.institution_label.text() == "Краевая клиническая больница"
    assert page.update_button.text() == "Обновить до 3.1.0"
    assert not page.update_button.isHidden()
    assert page.about_button.isHidden()
    page.set_update_available()
    assert not page.about_button.isHidden()
    assert page.update_button.isHidden()

    page.set_access_state("Требуется обновление", blocked=True)
    assert not any(card.isEnabled() for card in page.role_buttons.values())
    page.set_access_state()
    assert all(card.isEnabled() for card in page.role_buttons.values())

    image = _render(page, tmp_path / "welcome-1366x768.png")
    assert image.pixelColor(683, 384).alpha() > 0
    page.deleteLater()
    app.processEvents()


def test_welcome_page_keeps_four_roles_in_one_row_without_tooltips():
    app = application()
    page = WelcomePage()
    page.resize(900, 650)
    page.show()
    app.processEvents()

    positions = [(page.roles_grid.getItemPosition(index)[:2]) for index in range(page.roles_grid.count())]
    assert positions == [(0, 0), (0, 1), (0, 2), (0, 3)]
    assert all(not card.toolTip() and '\n' not in card.title for card in page.role_buttons.values())
    assert all(card.width() >= 178 and card.height() >= 168 for card in page.role_buttons.values())
    page.deleteLater()
    app.processEvents()


def test_startup_page_reports_only_explicit_stage_completion_and_errors(tmp_path):
    app = application()
    page = StartupPage()
    assert page.institution_label.text() == "Отделение анестезиологии, реанимации и интенсивной терапии"
    page.set_institution("Краевая клиническая больница", "ККБ")
    page.set_stage(0, "Проверяем параметры подключения")
    assert page.stage_rows[0].state == "active"
    assert page.progress_bar.value() == 0
    page.complete_stage(0)
    page.set_stage(3, "Не отмечаем этапы таймером")
    assert page.progress_bar.value() == 20
    page.set_stage(1, "Подключаемся к базе")
    page.complete_stage(1)
    assert page.progress_bar.value() == 40
    assert page.message_label.text() == "Подключаемся к базе"
    page.set_error("Нет доступа к базе данных")
    assert page.stage_rows[1].state == "error"
    assert not page.error_label.isHidden()
    assert page.institution_label.text() == (
        "Краевая клиническая больница\n"
        "Отделение анестезиологии, реанимации и интенсивной терапии"
    )

    page.reset()
    assert page.progress_bar.value() == 0
    assert all(row.state == "pending" for row in page.stage_rows)
    assert page.message_label.text() == "Ожидание запуска"
    assert page.error_label.isHidden()

    image = _render(page, tmp_path / "startup-1366x768.png")
    assert image.pixelColor(683, 384).alpha() > 0
    page.deleteLater()
    app.processEvents()
