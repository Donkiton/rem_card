from __future__ import annotations
from rem_card.ui.styles.theme_runtime import set_widget_style


from PySide6.QtCore import QEvent, Qt, QTimer
from PySide6.QtWidgets import (
    QComboBox,
    QFormLayout,
    QFrame,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QWidget,
)

from rem_card.services.operblock_team import (
    normalize_operblock_team_text,
)
from rem_card.ui.operblock_view.operblock_control_styles import (
    operblock_vertical_scrollbar_style as _operblock_vertical_scrollbar_style,
)

from rem_card.ui.operblock_view.operblock_visual_primitives import (
    OPERBLOCK_DIALOG_CANCEL_BUTTON_STYLE,
    OPERBLOCK_DIALOG_SAVE_BUTTON_STYLE,
)
from rem_card.ui.operblock_view.operblock_dialog_base import (
    OperBlockStyledDialog,
)
from rem_card.ui.operblock_view.operblock_stage_dialogs import (
    StartAnesthesiaDialog,
)

class EditOperBlockStaffDialog(OperBlockStyledDialog):
    def __init__(
        self,
        *,
        surgeon_options: list[str] | None = None,
        operating_nurse_options: list[str] | None = None,
        anesthesiologist_options: list[str] | None = None,
        anesthetist_options: list[str] | None = None,
        current_surgeons: list[str] | None = None,
        current_operating_nurse: str = "",
        current_anesthesiologist: str = "",
        current_anesthetist: str = "",
        surgery_enabled: bool = True,
        anesthesia_enabled: bool = True,
        parent=None,
    ):
        super().__init__(
            "Изменить состав",
            "edit_staff_dialog_geometry",
            parent,
            minimum_size=(560, 380),
            initial_size=(660, 460),
        )
        self._surgeon_options = list(surgeon_options or [])
        self._surgeon_combos: list[QComboBox] = []
        self._syncing_surgeon_fields = False
        self._surgery_enabled = bool(surgery_enabled)
        self._anesthesia_enabled = bool(anesthesia_enabled)
        self._init_ui(
            operating_nurse_options or [],
            anesthesiologist_options or [],
            anesthetist_options or [],
            current_surgeons or [],
            current_operating_nurse,
            current_anesthesiologist,
            current_anesthetist,
        )
        self._finalize_dialog_chrome()

    def _init_ui(
        self,
        operating_nurse_options: list[str],
        anesthesiologist_options: list[str],
        anesthetist_options: list[str],
        current_surgeons: list[str],
        current_operating_nurse: str,
        current_anesthesiologist: str,
        current_anesthetist: str,
    ) -> None:
        layout = self.content_layout

        self.team_scroll = QScrollArea()
        self.team_scroll.setObjectName("OperBlockEditStaffScroll")
        self.team_scroll.setWidgetResizable(True)
        self.team_scroll.setFrameShape(QFrame.NoFrame)
        self.team_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.team_scroll.setVerticalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        self.team_scroll.setMinimumHeight(250)
        self.team_scroll.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        scrollbar = self.team_scroll.verticalScrollBar()
        scrollbar.setObjectName("OperBlockEditStaffScrollBar")
        scrollbar.setFixedWidth(14)
        scrollbar.setSingleStep(36)
        scrollbar.setPageStep(108)
        set_widget_style(scrollbar, _operblock_vertical_scrollbar_style(
                "OperBlockEditStaffScrollBar",
                width_px=14,
                left_margin_px=3,
                right_margin_px=2,
            ))

        self.team_widget = QWidget()
        self.team_widget.setObjectName("OperBlockEditStaffContent")
        set_widget_style(self.team_widget, "QWidget#OperBlockEditStaffContent { background: transparent; border: none; }")
        self.team_layout = QFormLayout(self.team_widget)
        self.team_layout.setFieldGrowthPolicy(QFormLayout.ExpandingFieldsGrow)
        self.team_layout.setContentsMargins(0, 0, 0, 0)
        self.team_layout.setHorizontalSpacing(10)
        self.team_layout.setVerticalSpacing(8)

        self._syncing_surgeon_fields = True
        try:
            for surgeon in current_surgeons:
                self._add_surgeon_combo(surgeon)
            if not self._surgeon_combos:
                self._add_surgeon_combo()
            if self._surgeon_combos and normalize_operblock_team_text(self._surgeon_combos[-1].currentText()):
                self._add_surgeon_combo()
        finally:
            self._syncing_surgeon_fields = False

        self.operating_nurse_combo = StartAnesthesiaDialog._staff_combo(operating_nurse_options)
        self.operating_nurse_combo.setEditText(normalize_operblock_team_text(current_operating_nurse))
        self._install_team_combo_event_filter(self.operating_nurse_combo)
        self.team_layout.addRow("Операционная медсестра:", self.operating_nurse_combo)

        self.anesthesiologist_combo = StartAnesthesiaDialog._staff_combo(anesthesiologist_options)
        self.anesthesiologist_combo.setEditText(normalize_operblock_team_text(current_anesthesiologist))
        self._install_team_combo_event_filter(self.anesthesiologist_combo)
        self.team_layout.addRow("Анестезиолог:", self.anesthesiologist_combo)

        self.anesthetist_combo = StartAnesthesiaDialog._staff_combo(anesthetist_options)
        self.anesthetist_combo.setEditText(normalize_operblock_team_text(current_anesthetist))
        self._install_team_combo_event_filter(self.anesthetist_combo)
        self.team_layout.addRow("Анестезист:", self.anesthetist_combo)

        for combo in self._surgeon_combos + [self.operating_nurse_combo]:
            combo.setEnabled(self._surgery_enabled)
        for combo in (self.anesthesiologist_combo, self.anesthetist_combo):
            combo.setEnabled(self._anesthesia_enabled)

        self.team_scroll.setWidget(self.team_widget)
        set_widget_style(self.team_scroll, """
            QScrollArea#OperBlockEditStaffScroll {
                background: transparent;
                border: none;
            }
            QScrollArea#OperBlockEditStaffScroll > QWidget > QWidget {
                background: transparent;
            }
            """)
        layout.addWidget(self.team_scroll, 1)

        footer = QHBoxLayout()
        footer.addStretch(1)
        cancel_button = QPushButton("Отмена")
        cancel_button.setMinimumHeight(34)
        set_widget_style(cancel_button, OPERBLOCK_DIALOG_CANCEL_BUTTON_STYLE)
        cancel_button.clicked.connect(self.reject)
        self.save_button = QPushButton("Сохранить")
        self.save_button.setMinimumHeight(34)
        set_widget_style(self.save_button, OPERBLOCK_DIALOG_SAVE_BUTTON_STYLE)
        self.save_button.clicked.connect(self.accept)
        self._configure_enter_accept_button(cancel_button, self.save_button)
        footer.addWidget(cancel_button)
        footer.addWidget(self.save_button)
        layout.addLayout(footer)
        self._refresh_surgeon_row_labels()

    def _add_surgeon_combo(self, text: str = "") -> QComboBox:
        combo = StartAnesthesiaDialog._staff_combo(self._surgeon_options)
        combo.setEnabled(self._surgery_enabled)
        clean_text = normalize_operblock_team_text(text)
        if clean_text:
            combo.setEditText(clean_text)
        combo.currentTextChanged.connect(lambda *_args: self._sync_surgeon_fields())
        self._install_team_combo_event_filter(combo)
        insert_row = self._first_static_staff_row()
        self._surgeon_combos.append(combo)
        self.team_layout.insertRow(insert_row, "Хирург:" if len(self._surgeon_combos) == 1 else "", combo)
        self._refresh_surgeon_row_labels()
        self._scroll_team_to_bottom_later()
        return combo

    def _install_team_combo_event_filter(self, combo: QComboBox) -> None:
        combo.installEventFilter(self)
        line_edit = combo.lineEdit()
        if line_edit is not None:
            line_edit.installEventFilter(self)

    def _first_static_staff_row(self) -> int:
        for attr in ("operating_nurse_combo", "anesthesiologist_combo", "anesthetist_combo"):
            combo = getattr(self, attr, None)
            if combo is None:
                continue
            row, _role = self.team_layout.getWidgetPosition(combo)
            if row >= 0:
                return row
        return self.team_layout.rowCount()

    def _refresh_surgeon_row_labels(self) -> None:
        for index, combo in enumerate(self._surgeon_combos):
            label = self.team_layout.labelForField(combo)
            if isinstance(label, QLabel):
                label.setText("Хирург:" if index == 0 else "")

    def _scroll_team_to_bottom_later(self) -> None:
        scroll = getattr(self, "team_scroll", None)
        if scroll is None:
            return

        def scroll_to_bottom():
            try:
                bar = scroll.verticalScrollBar()
                bar.setValue(bar.maximum())
            except RuntimeError:
                return

        QTimer.singleShot(0, scroll_to_bottom)

    def _scroll_team_wheel(self, event) -> bool:
        scroll = getattr(self, "team_scroll", None)
        if scroll is None:
            return False
        bar = scroll.verticalScrollBar()
        if bar.maximum() <= bar.minimum():
            return False
        pixel_delta = event.pixelDelta().y() if hasattr(event, "pixelDelta") else 0
        angle_delta = event.angleDelta().y() if hasattr(event, "angleDelta") else 0
        delta = pixel_delta or angle_delta
        if not delta:
            return False
        steps = max(1, int(round(abs(angle_delta) / 120))) if angle_delta else 1
        direction = -1 if delta > 0 else 1
        bar.setValue(max(bar.minimum(), min(bar.maximum(), bar.value() + direction * bar.singleStep() * steps)))
        event.accept()
        return True

    def _is_team_wheel_widget(self, watched) -> bool:
        combos = list(getattr(self, "_surgeon_combos", [])) + [
            self.operating_nurse_combo,
            self.anesthesiologist_combo,
            self.anesthetist_combo,
        ]
        for combo in combos:
            if watched is combo:
                return True
            line_edit = combo.lineEdit()
            if line_edit is not None and watched is line_edit:
                return True
        return False

    def eventFilter(self, watched, event):
        if event.type() == QEvent.Wheel and self._is_team_wheel_widget(watched):
            if self._scroll_team_wheel(event):
                return True
        return super().eventFilter(watched, event)

    def _sync_surgeon_fields(self) -> None:
        if self._syncing_surgeon_fields or not self._surgery_enabled:
            return
        self._syncing_surgeon_fields = True
        try:
            if not self._surgeon_combos:
                self._add_surgeon_combo()

            if self._surgeon_combos and normalize_operblock_team_text(self._surgeon_combos[-1].currentText()):
                self._add_surgeon_combo()

            for combo in list(self._surgeon_combos[:-1]):
                if normalize_operblock_team_text(combo.currentText()):
                    continue
                self._surgeon_combos.remove(combo)
                self.team_layout.removeRow(combo)

            if not self._surgeon_combos:
                self._add_surgeon_combo()
            self._refresh_surgeon_row_labels()
        finally:
            self._syncing_surgeon_fields = False

    def selected_surgeons(self) -> list[str]:
        result: list[str] = []
        seen: set[str] = set()
        for combo in self._surgeon_combos:
            name = normalize_operblock_team_text(combo.currentText())
            key = name.casefold()
            if name and key not in seen:
                seen.add(key)
                result.append(name)
        return result

    def selected_operating_nurse(self) -> str:
        return normalize_operblock_team_text(self.operating_nurse_combo.currentText())

    def selected_anesthesiologist(self) -> str:
        return normalize_operblock_team_text(self.anesthesiologist_combo.currentText())

    def selected_anesthetist(self) -> str:
        return normalize_operblock_team_text(self.anesthetist_combo.currentText())
