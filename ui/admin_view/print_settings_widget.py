from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from rem_card.ui.rem_card_sectors.sector_print import PrintConfig
from rem_card.ui.shared.base_dialog import BaseStyledDialog
from rem_card.ui.shared.components.vital_settings_dialog import ToggleSwitch


REPORT_SECTIONS = [
    ("vitals", "Витальные функции", True),
    ("prescriptions", "Назначения", True),
    ("balance", "Баланс", True),
    ("events", "Движение", True),
    ("ventilation", "ИВЛ", True),
    ("death_outcome", "Отчет о смерти", True),
    ("death_protocol", "Протокол смерти", True),
    ("transfusion_registration", "Лист регистрации трансфузий", True),
    ("procedures", "Процедуры", False),
    ("labs", "Анализы", False),
]


class PrintSettingsWidget(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.remcard_service = None
        self.admission_id = None
        self.card_date = None
        self.config = PrintConfig()
        self.switches = {}
        self.status_label = None
        self.setup_ui()
        self.load_settings()

    def setup_ui(self):
        main_layout = QVBoxLayout(self)
        main_layout.setContentsMargins(0, 0, 0, 0)
        main_layout.setSpacing(12)

        list_frame = QFrame()
        list_frame.setObjectName("PrintSettingsList")
        list_frame.setStyleSheet(
            """
            QFrame#PrintSettingsList {
                background-color: #ffffff;
                border: 1px solid #dee2e6;
                border-radius: 5px;
            }
            """
        )
        list_layout = QVBoxLayout(list_frame)
        list_layout.setContentsMargins(10, 6, 10, 6)
        list_layout.setSpacing(0)

        for index, (key, label, enabled) in enumerate(REPORT_SECTIONS):
            row = self._create_switch_row(key, label, enabled)
            list_layout.addWidget(row)
            if index < len(REPORT_SECTIONS) - 1:
                divider = QFrame()
                divider.setFixedHeight(1)
                divider.setStyleSheet("background-color: #edf0f2; border: none;")
                list_layout.addWidget(divider)

        main_layout.addWidget(list_frame)

        self.status_label = QLabel("")
        self.status_label.setAlignment(Qt.AlignCenter)
        self.status_label.setStyleSheet(
            "color: #7f8c8d; font-style: italic; background: transparent;"
        )
        main_layout.addWidget(self.status_label)

    def _create_switch_row(self, key, label_text, enabled):
        row = QWidget()
        row.setFixedHeight(38)
        row.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)

        layout = QHBoxLayout(row)
        layout.setContentsMargins(2, 0, 2, 0)
        layout.setSpacing(12)

        label = QLabel(label_text)
        label.setStyleSheet(
            "font-size: 13px; font-weight: 600; color: #2c3e50; background: transparent;"
            if enabled
            else "font-size: 13px; font-weight: 600; color: #adb5bd; background: transparent;"
        )

        switch = ToggleSwitch()
        switch.setEnabled(enabled)
        switch.setToolTip("" if enabled else "Раздел пока недоступен")
        switch.stateChanged.connect(lambda _state, section_key=key: self.on_switch_changed(section_key))

        layout.addWidget(label)
        layout.addStretch()
        layout.addWidget(switch, 0, Qt.AlignRight)
        self.switches[key] = switch
        return row

    def _config_from_switches(self):
        values = {}
        for key, _label, enabled in REPORT_SECTIONS:
            values[key] = self.switches[key].isChecked() if enabled else False
        return values

    def on_switch_changed(self, _section_key):
        self.save_settings()
        self.status_label.setText("Настройки сохранены")

    def set_context(self, service, admission_id, date):
        self.remcard_service = service
        self.admission_id = admission_id
        self.card_date = date
        self.status_label.setText("")

    def load_settings(self):
        cfg = self.config.load()
        for key, _label, enabled in REPORT_SECTIONS:
            switch = self.switches[key]
            checked = bool(cfg.get(key, False)) if enabled else False
            switch.blockSignals(True)
            switch.setChecked(checked)
            switch.position = 1.0 if checked else 0.0
            switch.blockSignals(False)

    def save_settings(self):
        cfg = self._config_from_switches()
        self.config.save(
            cfg["vitals"],
            cfg["balance"],
            cfg["prescriptions"],
            cfg["events"],
            cfg["ventilation"],
            cfg["labs"],
            cfg["procedures"],
            cfg["death_outcome"],
            cfg["death_protocol"],
            cfg["transfusion_registration"],
        )


class PrintSettingsDialog(BaseStyledDialog):
    def __init__(self, service=None, admission_id=None, date=None, parent=None):
        super().__init__("Печать / Отчеты", parent)
        self.main_frame.setFixedWidth(430)
        self.settings_widget = PrintSettingsWidget(self)
        self.content_layout.addWidget(self.settings_widget)
        self.set_context(service, admission_id, date)

    def set_context(self, service, admission_id, date):
        self.settings_widget.set_context(service, admission_id, date)

    def load_settings(self):
        self.settings_widget.load_settings()
