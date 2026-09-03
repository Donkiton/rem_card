from __future__ import annotations

import os

from PySide6.QtCore import QPoint, QSize, Qt
from PySide6.QtGui import QIcon
from PySide6.QtWidgets import QApplication, QDialog, QLabel, QPushButton

from rem_card.app.paths import get_icon_dir
from rem_card.ui.shared.base_dialog import BaseStyledDialog
from rem_card.ui.styles.theme import (
    BG_CARD,
    BG_LIGHT,
    BORDER_COLOR,
    COLOR_PRIMARY,
    TEXT_MUTED,
    TEXT_PRIMARY,
    TEXT_SECONDARY,
)


CALCULATION_INFUSION = "infusion"
CALCULATION_ELECTROLYTES = "electrolytes"
CALCULATION_BURNS = "burns"


class CalculationLauncherDialog(BaseStyledDialog):
    """Компактный выбор одного из клинических калькуляторов."""

    def __init__(
        self,
        parent=None,
        *,
        burn_enabled: bool = False,
        burn_disabled_reason: str = "Калькулятор доступен только из подходящей карты пациента",
    ):
        super().__init__("Расчёт", parent)
        self.selected_calculation: str | None = None
        self._burn_disabled_reason = str(burn_disabled_reason or "")
        self.resize(540, 390)
        self.setMinimumSize(500, 360)
        self.setMaximumWidth(620)
        self._setup_ui(bool(burn_enabled))
        self._apply_styles()

    def _setup_ui(self, burn_enabled: bool) -> None:
        self.content_layout.setContentsMargins(18, 14, 18, 18)
        self.content_layout.setSpacing(10)

        intro = QLabel("Выберите нужный расчёт")
        intro.setObjectName("CalculationLauncherIntro")
        self.content_layout.addWidget(intro)

        self.infusion_button = self._add_choice(
            CALCULATION_INFUSION,
            "Калькулятор скорости инфузии",
            "Пересчёт дозы и скорости введения",
            "calc.png",
        )
        self.electrolytes_button = self._add_choice(
            CALCULATION_ELECTROLYTES,
            "Электролиты",
            "Коррекция калия, натрия и хлора",
            "microelements.png",
        )
        self.burns_button = self._add_choice(
            CALCULATION_BURNS,
            "Ожоги",
            "Инфузионная терапия при острой ожоговой травме",
            "fire.png",
            enabled=burn_enabled,
            disabled_reason=self._burn_disabled_reason,
        )
        self.infusion_button.setFocus(Qt.OtherFocusReason)

    def _add_choice(
        self,
        calculation_id: str,
        title: str,
        description: str,
        icon_name: str,
        *,
        enabled: bool = True,
        disabled_reason: str = "",
    ) -> QPushButton:
        button = QPushButton(f"{title}\n{description}", self)
        button.setObjectName("CalculationLauncherChoice")
        button.setProperty("calculationId", calculation_id)
        button.setMinimumHeight(76)
        button.setIconSize(QSize(30, 30))
        icon_path = os.path.join(get_icon_dir(), icon_name)
        if os.path.exists(icon_path):
            button.setIcon(QIcon(icon_path))
        button.setAccessibleName(title)
        button.setAccessibleDescription(description if enabled else disabled_reason)
        button.setEnabled(enabled)
        if disabled_reason and not enabled:
            button.setToolTip(disabled_reason)
            button.setText(f"{title}\n{disabled_reason}")
        button.clicked.connect(lambda _checked=False, value=calculation_id: self._select(value))
        self.content_layout.addWidget(button)
        return button

    def _select(self, calculation_id: str) -> None:
        self.selected_calculation = str(calculation_id)
        self.accept()

    def _apply_styles(self) -> None:
        self.setStyleSheet(
            f"""
            {self.styleSheet()}
            QLabel#CalculationLauncherIntro {{
                color: {TEXT_SECONDARY};
                font-size: 13px;
                padding: 0 2px 2px 2px;
            }}
            QPushButton#CalculationLauncherChoice {{
                background-color: {BG_CARD};
                color: {TEXT_PRIMARY};
                border: 1px solid {BORDER_COLOR};
                border-radius: 8px;
                padding: 10px 14px;
                text-align: left;
                font-size: 13px;
                font-weight: 600;
            }}
            QPushButton#CalculationLauncherChoice:hover {{
                background-color: {BG_LIGHT};
                border: 1px solid {COLOR_PRIMARY};
            }}
            QPushButton#CalculationLauncherChoice:focus {{
                border: 2px solid {COLOR_PRIMARY};
                padding: 9px 13px;
            }}
            QPushButton#CalculationLauncherChoice:pressed {{
                background-color: {BG_LIGHT};
            }}
            QPushButton#CalculationLauncherChoice:disabled {{
                background-color: {BG_LIGHT};
                color: {TEXT_MUTED};
                border: 1px solid {BORDER_COLOR};
            }}
            """
        )


def run_calculation_launcher(
    parent,
    *,
    burn_enabled: bool,
    burn_disabled_reason: str,
) -> tuple[str | None, QPoint | None]:
    launcher = CalculationLauncherDialog(
        parent,
        burn_enabled=burn_enabled,
        burn_disabled_reason=burn_disabled_reason,
    )
    try:
        result = launcher.exec()
        center = QPoint(launcher.frameGeometry().center())
        selected = launcher.selected_calculation if result == QDialog.Accepted else None
        return selected, center
    finally:
        # Диалог имеет parent, поэтому одного выхода из локальной функции
        # недостаточно: без явного удаления закрытые окна копятся как QObject-дети.
        launcher.deleteLater()


def exec_calculation_dialog(dialog: QDialog, anchor_center: QPoint | None = None) -> int:
    """Открывает выбранный калькулятор из центра меню, сохраняя визуальную связь."""

    dialog.ensurePolished()
    if anchor_center is not None:
        frame = dialog.frameGeometry()
        frame.moveCenter(anchor_center)
        # QWidget.screen() до создания native window на Windows/PySide6 может
        # обращаться к уже перестраиваемому window handle после другого exec().
        # Экран надёжнее определить напрямую по сохранённой глобальной точке.
        screen = QApplication.screenAt(anchor_center)
        if screen is None:
            app = QApplication.instance()
            screen = app.primaryScreen() if app is not None else None
        if screen is not None:
            area = screen.availableGeometry()
            if frame.left() < area.left():
                frame.moveLeft(area.left())
            if frame.top() < area.top():
                frame.moveTop(area.top())
            if frame.right() > area.right():
                frame.moveRight(area.right())
            if frame.bottom() > area.bottom():
                frame.moveBottom(area.bottom())
        dialog.move(frame.topLeft())
    try:
        return dialog.exec()
    finally:
        # Все калькуляторы создаются с parent; освобождаем их после каждого
        # закрытия, чтобы повторные открытия не накапливали скрытые native окна.
        dialog.deleteLater()
