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
    ("vitals", "Витальные функции", "Температура, давление, пульс и другие показатели наблюдения.", True),
    ("prescriptions", "Назначения", "Лекарственные назначения и отметки об их выполнении.", True),
    ("balance", "Баланс", "Введённые и выведенные объёмы за выбранный период.", True),
    ("events", "Движение", "Переводы, исходы и ключевые события госпитализации.", True),
    ("ventilation", "ИВЛ", "Параметры вентиляции и история респираторной поддержки.", True),
    ("death_outcome", "Отчёт о смерти", "Включать форму итогового отчёта о смерти пациента.", True),
    ("death_protocol", "Протокол смерти", "Добавлять протокол установления смерти в комплект документов.", True),
    ("transfusion_registration", "Лист регистрации трансфузий", "Печатать сводный лист регистрации трансфузий.", True),
    ("transfusion_protocols", "Протоколы гемотрансфузии", "Добавлять отдельные протоколы проведённых гемотрансфузий.", True),
    ("outcome_report_reminder", "Напоминание при исходе", "Предлагать печать отчёта после сохранения финального исхода.", True),
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
        list_frame.setProperty("settingsSurfacePanel", True)
        list_layout = QVBoxLayout(list_frame)
        list_layout.setContentsMargins(10, 6, 10, 6)
        list_layout.setSpacing(0)

        for index, (key, label, description, enabled) in enumerate(REPORT_SECTIONS):
            row = self._create_switch_row(key, label, description, enabled)
            list_layout.addWidget(row)
            if index < len(REPORT_SECTIONS) - 1:
                divider = QFrame()
                divider.setObjectName("SettingsSurfaceDivider")
                divider.setFixedHeight(1)
                list_layout.addWidget(divider)

        main_layout.addWidget(list_frame)

        self.status_label = QLabel("")
        self.status_label.setAlignment(Qt.AlignCenter)
        self.status_label.setProperty("settingsSurfaceMuted", True)
        main_layout.addWidget(self.status_label)

    def _create_switch_row(self, key, label_text, description_text, enabled):
        row = QWidget()
        row.setFixedHeight(58)
        row.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)

        layout = QHBoxLayout(row)
        layout.setContentsMargins(2, 0, 2, 0)
        layout.setSpacing(12)

        text_layout = QVBoxLayout()
        text_layout.setContentsMargins(0, 7, 0, 7)
        text_layout.setSpacing(2)
        label = QLabel(label_text)
        label.setProperty("settingsSurfaceLabel", True)
        label.setProperty("settingsSurfaceDisabled", not enabled)
        description = QLabel(description_text)
        description.setProperty("settingsSurfaceDescription", True)
        description.setProperty("settingsSurfaceDisabled", not enabled)
        text_layout.addWidget(label)
        text_layout.addWidget(description)

        switch = ToggleSwitch()
        switch.setEnabled(enabled)
        switch.setToolTip("" if enabled else "Раздел пока недоступен")
        switch.stateChanged.connect(lambda _state, section_key=key: self.on_switch_changed(section_key))

        layout.addLayout(text_layout)
        layout.addStretch()
        layout.addWidget(switch, 0, Qt.AlignRight)
        self.switches[key] = switch
        return row

    def _config_from_switches(self):
        values = {}
        for key, _label, _description, enabled in REPORT_SECTIONS:
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
        for key, _label, _description, enabled in REPORT_SECTIONS:
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
            cfg.get("labs", False),
            cfg.get("procedures", False),
            cfg["death_outcome"],
            cfg["death_protocol"],
            cfg["transfusion_registration"],
            outcome_report_reminder=cfg["outcome_report_reminder"],
            transfusion_protocols=cfg["transfusion_protocols"],
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
