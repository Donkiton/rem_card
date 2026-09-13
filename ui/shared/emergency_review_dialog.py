from __future__ import annotations

from typing import Any, Mapping

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QCheckBox,
    QDialog,
    QFrame,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from rem_card.ui.shared.emergency_dialogs import EmergencyActionDialog, NonClosableEmergencyDialog


class AdmissionReviewBlock(QFrame):
    selectionChanged = Signal()

    def __init__(self, patient: Mapping[str, Any], parent: QWidget | None = None):
        super().__init__(parent)
        self.admission_id = int(patient["admission_id"])
        self.setObjectName("EmergencyReviewAdmissionBlock")
        self.setFrameShape(QFrame.StyledPanel)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Maximum)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(12, 10, 12, 10)
        layout.setSpacing(8)

        title = QLabel(str(patient.get("title") or "Госпитализация"), self)
        title.setTextFormat(Qt.PlainText)
        title.setObjectName("EmergencyReviewAdmissionTitle")
        title.setWordWrap(True)
        title.setTextInteractionFlags(Qt.TextSelectableByMouse)
        title.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Preferred)
        layout.addWidget(title)

        controls = QHBoxLayout()
        controls.setSpacing(8)
        self.accept_checkbox = QCheckBox("Принять локальные данные", self)
        self.accept_checkbox.setObjectName("EmergencyReviewAdmissionCheck")
        self.accept_checkbox.setChecked(False)
        self.accept_checkbox.setAccessibleName(
            f"Принять локальные данные: {str(patient.get('title') or 'госпитализация')}"
        )
        self.accept_checkbox.toggled.connect(lambda _checked: self.selectionChanged.emit())
        controls.addWidget(self.accept_checkbox)
        controls.addStretch(1)

        count = int(patient.get("operation_count") or 0)
        self.toggle_button = QToolButton(self)
        self.toggle_button.setObjectName("EmergencyReviewExpandButton")
        self.toggle_button.setText(f"Изменения: {count}")
        self.toggle_button.setToolButtonStyle(Qt.ToolButtonTextBesideIcon)
        self.toggle_button.setArrowType(Qt.RightArrow)
        self.toggle_button.setCheckable(True)
        self.toggle_button.setChecked(False)
        self.toggle_button.setAccessibleName(
            f"Показать изменения: {str(patient.get('title') or 'госпитализация')}"
        )
        self.toggle_button.setAccessibleDescription("Сворачивает и разворачивает подробный список изменений")
        controls.addWidget(self.toggle_button)
        layout.addLayout(controls)

        self.details_widget = QWidget(self)
        self.details_widget.setObjectName("EmergencyReviewAdmissionDetails")
        details_layout = QVBoxLayout(self.details_widget)
        details_layout.setContentsMargins(0, 4, 0, 0)
        details_layout.setSpacing(10)
        for section in patient.get("sections") or []:
            section_title = QLabel(str(section.get("title") or "Изменения"), self.details_widget)
            section_title.setObjectName("EmergencyReviewSectionTitle")
            section_title.setStyleSheet("font-weight: 600;")
            details_layout.addWidget(section_title)
            for item in section.get("items") or []:
                label = QLabel(str(item.get("text") or ""), self.details_widget)
                label.setTextFormat(Qt.PlainText)
                label.setObjectName("EmergencyReviewItem")
                label.setWordWrap(True)
                label.setTextInteractionFlags(Qt.TextSelectableByMouse)
                label.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Preferred)
                label.setAccessibleName(str(item.get("text") or "Изменение"))
                details_layout.addWidget(label)
        self.details_widget.hide()
        layout.addWidget(self.details_widget)

        self.toggle_button.toggled.connect(self._set_expanded)

    @property
    def selected(self) -> bool:
        return self.accept_checkbox.isChecked()

    def _set_expanded(self, expanded: bool) -> None:
        self.details_widget.setVisible(bool(expanded))
        self.toggle_button.setArrowType(Qt.DownArrow if expanded else Qt.RightArrow)
        self.toggle_button.setAccessibleName(
            ("Скрыть" if expanded else "Показать") + " изменения госпитализации"
        )


class EmergencyReviewDialog(NonClosableEmergencyDialog):
    """Review and select whole admissions without writing to any database."""

    def __init__(self, review: Mapping[str, Any], parent: QWidget | None = None):
        super().__init__(
            "Проверка аварийных данных",
            "Выберите госпитализации, для которых локальное состояние должно полностью заменить текущее сетевое состояние.",
            parent=parent,
        )
        self.review = dict(review)
        self._blocks: list[AdmissionReviewBlock] = []
        self._explicit_zero_selection = False
        self.setObjectName("EmergencyReviewDialog")
        self.setMinimumSize(640, 420)
        self.resize(900, 680)
        self.message_label.setMinimumWidth(0)
        self.message_label.setAccessibleName("Пояснение к проверке аварийных данных")

        summary = dict(review.get("summary") or {})
        summary_label = QLabel(
            f"Госпитализаций: {int(summary.get('patient_count') or 0)} · "
            f"изменений: {int(summary.get('operation_count') or 0)}",
            self.content_widget,
        )
        summary_label.setObjectName("EmergencyReviewSummary")
        self.content_layout.addWidget(summary_label)

        blockers = list(review.get("blockers") or [])
        self.blockers_label = QLabel(self.content_widget)
        self.blockers_label.setObjectName("EmergencyReviewBlockers")
        self.blockers_label.setWordWrap(True)
        self.blockers_label.setTextInteractionFlags(Qt.TextSelectableByMouse)
        if blockers:
            messages = [
                str(item.get("message") or "Объединение требует ручной проверки.")
                if isinstance(item, Mapping) else str(item)
                for item in blockers
            ]
            self.blockers_label.setText("Перенос заблокирован:\n• " + "\n• ".join(messages))
            self.blockers_label.setAccessibleName("Перенос заблокирован. " + " ".join(messages))
            self.content_layout.addWidget(self.blockers_label)

        self.scroll_area = QScrollArea(self.content_widget)
        self.scroll_area.setObjectName("EmergencyReviewScroll")
        self.scroll_area.setWidgetResizable(True)
        self.scroll_area.setFrameShape(QFrame.NoFrame)
        self.scroll_area.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.scroll_area.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        scroll_body = QWidget(self.scroll_area)
        scroll_body.setObjectName("EmergencyReviewScrollBody")
        body_layout = QVBoxLayout(scroll_body)
        body_layout.setContentsMargins(0, 0, 0, 0)
        body_layout.setSpacing(8)
        for patient in review.get("patients") or []:
            block = AdmissionReviewBlock(patient, scroll_body)
            block.selectionChanged.connect(self._update_selection_state)
            body_layout.addWidget(block)
            self._blocks.append(block)
        body_layout.addStretch(1)
        self.scroll_area.setWidget(scroll_body)
        self.content_layout.addWidget(self.scroll_area, 1)

        self.selection_label = QLabel("Выбрано: 0", self.content_widget)
        self.selection_label.setObjectName("EmergencyReviewSelectionCount")
        self.selection_label.setAccessibleName("Не выбрано ни одной госпитализации")
        self.content_layout.addWidget(self.selection_label)

        self.accept_button = QPushButton("Перенести выбранные", self.content_widget)
        self.accept_button.setObjectName("DialogOkBtn")
        self.accept_button.setEnabled(False)
        self.accept_button.clicked.connect(self._accept_selected)

        self.finish_without_button = QPushButton("Завершить без переноса", self.content_widget)
        self.finish_without_button.setObjectName("DialogSecondaryBtn")
        self.finish_without_button.clicked.connect(self._finish_without_transfer)

        self.cancel_button = QPushButton("Отмена", self.content_widget)
        self.cancel_button.setObjectName("DialogSecondaryBtn")
        self.cancel_button.clicked.connect(self.reject)

        self.button_layout.addWidget(self.cancel_button)
        self.button_layout.addWidget(self.finish_without_button)
        self.button_layout.addWidget(self.accept_button)
        self.content_layout.addLayout(self.button_layout)
        self._has_blockers = bool(blockers)
        self.finish_without_button.setEnabled(not self._has_blockers)
        self._update_selection_state()

    @property
    def selected_admission_ids(self) -> list[int]:
        if self._explicit_zero_selection:
            return []
        return [block.admission_id for block in self._blocks if block.selected]

    def reject(self) -> None:
        self._explicit_zero_selection = False
        self.finish_with_code(QDialog.Rejected)

    def closeEvent(self, event) -> None:
        self._allow_dialog_finish = True
        super().closeEvent(event)

    def showEvent(self, event) -> None:
        super().showEvent(event)
        screen = self.screen()
        if screen is None:
            return
        available = screen.availableGeometry()
        target_width = min(900, max(640, available.width() - 48))
        target_height = min(680, max(420, available.height() - 48))
        self.resize(target_width, target_height)

    def _update_selection_state(self) -> None:
        selected = len([block for block in self._blocks if block.selected])
        total = len(self._blocks)
        self.selection_label.setText(f"Выбрано: {selected} из {total}")
        self.selection_label.setAccessibleName(f"Выбрано госпитализаций: {selected} из {total}")
        self.accept_button.setText(f"Перенести выбранные ({selected})" if selected else "Перенести выбранные")
        self.accept_button.setEnabled(selected > 0 and not getattr(self, "_has_blockers", False))

    def _accept_selected(self) -> None:
        selected = len(self.selected_admission_ids)
        total = len(self._blocks)
        omitted = total - selected
        if selected <= 0 or self._has_blockers:
            return
        if omitted:
            result = EmergencyActionDialog.ask(
                self,
                "Есть невыбранные госпитализации",
                f"Не выбрано: {omitted} из {total}. Их локальные изменения не будут перенесены. Продолжить?",
                (("Продолжить", QDialog.Accepted), ("Вернуться к выбору", QDialog.Rejected)),
                default_code=QDialog.Rejected,
            )
            if result != QDialog.Accepted:
                return
        self._explicit_zero_selection = False
        self.finish_with_code(QDialog.Accepted)

    def _finish_without_transfer(self) -> None:
        if self._has_blockers:
            return
        total = len(self._blocks)
        result = EmergencyActionDialog.ask(
            self,
            "Завершить без переноса",
            f"Ни одна из {total} госпитализаций не будет перенесена. "
            "Локальная копия останется в архиве этого компьютера. Завершить аварийную сессию?",
            (("Завершить без переноса", QDialog.Accepted), ("Вернуться к выбору", QDialog.Rejected)),
            default_code=QDialog.Rejected,
        )
        if result != QDialog.Accepted:
            return
        for block in self._blocks:
            block.accept_checkbox.setChecked(False)
        self._explicit_zero_selection = True
        self.finish_with_code(QDialog.Accepted)
