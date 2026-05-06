from PySide6.QtCore import QTimer
from PySide6.QtWidgets import (
    QComboBox,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPlainTextEdit,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from rem_card.services.bars_auth_service import (
    BarsAuthCheckResult,
    BarsAuthService,
    BarsPatientLabsResult,
    BarsPatientListResult,
    BarsPatientProbeResult,
)
from rem_card.services.bars_labs import split_bars_laboratory_result_items
from rem_card.ui.shared.async_call import AsyncCallThread
from rem_card.ui.shared.base_dialog import BaseStyledDialog


class BarsAuthDialog(BaseStyledDialog):
    def __init__(self, auth_service: BarsAuthService, parent=None):
        super().__init__("Авторизация БАРС", parent)
        self.auth_service = auth_service
        self.authorized = bool(auth_service.last_authorized)
        self._launch_worker = None
        self._check_worker = None
        self._patient_probe_worker = None
        self._patient_list_worker = None
        self._patient_labs_worker = None
        self._background_worker = None
        self._auto_accept_scheduled = False
        self._last_patient_list_result = None
        self._patient_card_buttons = []

        self._init_content()

        self._poll_timer = QTimer(self)
        self._poll_timer.setInterval(1800)
        self._poll_timer.timeout.connect(self._check_authorized_async)
        QTimer.singleShot(0, self._open_browser_async)

    def _init_content(self):
        self.status_label = QLabel("Открываю БАРС...")
        self.status_label.setWordWrap(True)
        self.status_label.setMinimumWidth(760)
        self.status_label.setStyleSheet("font-size: 14px; color: #2c3e50;")

        self.hint_label = QLabel(
            "В открытом окне Яндекс-Браузера выберите способ входа, ЭЦП и кабинет. "
            "После входа РЕМКАРТА будет использовать служебную сессию БАРС без ручной навигации в браузере."
        )
        self.hint_label.setWordWrap(True)
        self.hint_label.setStyleSheet("font-size: 12px; color: #5d6d7e;")

        button_layout = QHBoxLayout()
        button_layout.addStretch()

        self.btn_check = QPushButton("Проверить")
        self.btn_check.setObjectName("DialogOkBtn")
        self.btn_check.clicked.connect(self._check_authorized_async)

        self.btn_close = QPushButton("Закрыть")
        self.btn_close.setObjectName("DialogOkBtn")
        self.btn_close.clicked.connect(self.reject)

        button_layout.addWidget(self.btn_check)
        button_layout.addWidget(self.btn_close)

        search_layout = QHBoxLayout()
        self.history_input = QLineEdit()
        self.history_input.setPlaceholderText("Номер истории или фамилия")
        self.history_input.returnPressed.connect(self._probe_patient_async)
        self.btn_patient_probe = QPushButton("Поиск по журналу")
        self.btn_patient_probe.setObjectName("DialogOkBtn")
        self.btn_patient_probe.clicked.connect(self._probe_patient_async)

        self.btn_patient_list = QPushButton("Список пациентов")
        self.btn_patient_list.setObjectName("DialogOkBtn")
        self.btn_patient_list.clicked.connect(self._load_patient_list_async)

        search_layout.addWidget(self.history_input, 1)
        search_layout.addWidget(self.btn_patient_probe)
        search_layout.addWidget(self.btn_patient_list)

        self.probe_text = QPlainTextEdit()
        self.probe_text.setReadOnly(True)
        self.probe_text.setPlaceholderText("Здесь появится результат поиска по журналу.")
        self.probe_text.setMinimumHeight(170)
        self.probe_text.setStyleSheet("font-size: 12px; color: #2c3e50; background: #ffffff;")

        self.patient_cards_widget = QWidget()
        self.patient_cards_layout = QVBoxLayout(self.patient_cards_widget)
        self.patient_cards_layout.setContentsMargins(2, 2, 2, 2)
        self.patient_cards_layout.setSpacing(8)
        self.patient_cards_layout.addStretch()

        self.patient_scroll = QScrollArea()
        self.patient_scroll.setWidgetResizable(True)
        self.patient_scroll.setFrameShape(QFrame.NoFrame)
        self.patient_scroll.setWidget(self.patient_cards_widget)
        self.patient_scroll.setMinimumHeight(420)
        self.patient_scroll.setStyleSheet("QScrollArea { background: #f8f9fa; border: none; }")
        self.patient_scroll.hide()

        self.labs_panel = QFrame()
        self.labs_panel.setObjectName("BarsLabsPanel")
        self.labs_panel.setStyleSheet(
            """
            QFrame#BarsLabsPanel {
                background: #ffffff;
                border: 1px solid #dee2e6;
                border-radius: 6px;
            }
            """
        )
        labs_layout = QVBoxLayout(self.labs_panel)
        labs_layout.setContentsMargins(12, 10, 12, 12)
        labs_layout.setSpacing(8)

        labs_header_layout = QHBoxLayout()
        self.btn_labs_back = QPushButton("К списку")
        self.btn_labs_back.setObjectName("DialogOkBtn")
        self.btn_labs_back.clicked.connect(self._back_to_patient_cards)
        self.labs_title = QLabel("Анализы")
        self.labs_title.setWordWrap(True)
        self.labs_title.setStyleSheet("font-size: 14px; font-weight: bold; color: #2c3e50;")
        labs_header_layout.addWidget(self.labs_title, 1)
        labs_header_layout.addWidget(self.btn_labs_back)

        self.labs_text = QPlainTextEdit()
        self.labs_text.setReadOnly(True)
        self.labs_text.setMinimumHeight(380)
        self.labs_text.setStyleSheet("font-size: 12px; color: #2c3e50; background: #ffffff;")

        labs_layout.addLayout(labs_header_layout)
        labs_layout.addWidget(self.labs_text)
        self.labs_panel.hide()

        self.content_layout.addWidget(self.status_label)
        self.content_layout.addWidget(self.hint_label)
        self.content_layout.addLayout(button_layout)
        self.content_layout.addLayout(search_layout)
        self.content_layout.addWidget(self.probe_text)
        self.content_layout.addWidget(self.patient_scroll)
        self.content_layout.addWidget(self.labs_panel)
        self.resize(980, 760)

    def _set_busy(self, busy: bool):
        self.btn_check.setEnabled(not busy)
        self.btn_patient_probe.setEnabled(not busy)
        self.btn_patient_list.setEnabled(not busy)
        self._set_patient_card_buttons_enabled(not busy)

    def _open_browser_async(self):
        if self._launch_worker and self._launch_worker.isRunning():
            return
        self._set_busy(True)
        self.status_label.setText("Открываю окно БАРС...")
        self._launch_worker = AsyncCallThread(self.auth_service.open_auth_window, parent=self)
        self._launch_worker.succeeded.connect(self._on_browser_opened)
        self._launch_worker.failed.connect(self._on_worker_failed)
        self._launch_worker.finished.connect(lambda: self._set_busy(False))
        self._launch_worker.start()

    def _on_browser_opened(self, result: BarsAuthCheckResult):
        self.status_label.setText(result.message)
        if result.authorized:
            self._complete_authorization(result)
            return
        if result.message.startswith("Яндекс-Браузер не найден") or result.message.startswith("Не удалось открыть"):
            self._poll_timer.stop()
            return
        self._poll_timer.start()
        QTimer.singleShot(900, self._check_authorized_async)

    def _check_authorized_async(self):
        if self._check_worker and self._check_worker.isRunning():
            return
        self._check_worker = AsyncCallThread(self.auth_service.check_authorized, parent=self)
        self._check_worker.succeeded.connect(self._on_auth_checked)
        self._check_worker.failed.connect(self._on_worker_failed)
        self._check_worker.start()

    def _on_auth_checked(self, result: BarsAuthCheckResult):
        self.status_label.setText(result.message)
        if result.authorized:
            self._complete_authorization(result)

    def _probe_patient_async(self):
        if self._patient_probe_worker and self._patient_probe_worker.isRunning():
            return
        history_number = self.history_input.text().strip()
        if not history_number:
            self.status_label.setText("Введите номер истории или фамилию")
            return
        self._set_busy(True)
        self.status_label.setText("Ищу пациента по журналу...")
        self._patient_probe_worker = AsyncCallThread(
            self.auth_service.probe_patient_by_history,
            history_number,
            parent=self,
        )
        self._patient_probe_worker.succeeded.connect(self._on_patient_probed)
        self._patient_probe_worker.failed.connect(self._on_worker_failed)
        self._patient_probe_worker.finished.connect(lambda: self._set_busy(False))
        self._patient_probe_worker.start()

    def _on_patient_probed(self, result: BarsPatientProbeResult):
        self.status_label.setText(result.message)
        self._show_text_result(self._format_patient_probe_result(result))
        if result.ok:
            self.authorized = True

    def _load_patient_list_async(self):
        if self._patient_list_worker and self._patient_list_worker.isRunning():
            return
        self._set_busy(True)
        self.status_label.setText("Получаю список пациентов отделения...")
        self._patient_list_worker = AsyncCallThread(
            self.auth_service.list_department_patients,
            parent=self,
        )
        self._patient_list_worker.succeeded.connect(self._on_patient_list_loaded)
        self._patient_list_worker.failed.connect(self._on_worker_failed)
        self._patient_list_worker.finished.connect(lambda: self._set_busy(False))
        self._patient_list_worker.start()

    def _on_patient_list_loaded(self, result: BarsPatientListResult):
        self.status_label.setText(result.message)
        self._last_patient_list_result = result
        if result.ok and result.patients:
            self._show_patient_cards(result)
        else:
            self._show_text_result(self._format_patient_list_result(result))
        if result.ok:
            self.authorized = True

    def _load_patient_labs_async(self, patient: dict, mode: str):
        if self._patient_labs_worker and self._patient_labs_worker.isRunning():
            return
        self._set_busy(True)
        full_name = patient.get("full_name") or patient.get("history_number") or "пациент"
        self.status_label.setText(f"Получаю анализы: {full_name}...")
        self._show_labs_panel(patient, mode, "Загружаю лабораторные исследования из БАРС...")
        self._patient_labs_worker = AsyncCallThread(
            self.auth_service.get_patient_labs,
            patient,
            mode,
            parent=self,
        )
        self._patient_labs_worker.succeeded.connect(self._on_patient_labs_loaded)
        self._patient_labs_worker.failed.connect(self._on_worker_failed)
        self._patient_labs_worker.finished.connect(lambda: self._set_busy(False))
        self._patient_labs_worker.start()

    def _on_patient_labs_loaded(self, result: BarsPatientLabsResult):
        self.status_label.setText(result.message)
        self.labs_title.setText(self._labs_title_for_result(result))
        self.labs_text.setPlainText(self._format_patient_labs_result(result))
        if result.ok:
            self.authorized = True

    def _complete_authorization(self, result: BarsAuthCheckResult):
        self.authorized = True
        self._poll_timer.stop()
        self.status_label.setText(f"{result.message}. Служебная сессия подготовлена для работы из РЕМКАРТЫ.")
        self._prepare_background_session_async()

    def _prepare_background_session_async(self):
        if self._background_worker and self._background_worker.isRunning():
            return
        self._background_worker = AsyncCallThread(self.auth_service.prepare_background_session, parent=self)
        self._background_worker.failed.connect(lambda exc: None)
        self._background_worker.start()

    def _format_patient_probe_result(self, result: BarsPatientProbeResult) -> str:
        patients = result.patients or []
        if not patients:
            return result.message

        lines = [result.message]
        for index, patient in enumerate(patients, start=1):
            if len(patients) > 1:
                lines.append("")
                lines.append(f"{index}. {patient.get('full_name', '')}")
                prefix = "   "
            else:
                prefix = ""
            if len(patients) == 1:
                lines.append(f"ФИО: {patient.get('full_name', '')}")
            lines.append(f"{prefix}Номер истории: {patient.get('history_number', '')}")
            lines.append(f"{prefix}Дата рождения: {patient.get('birthdate', '') or 'не найдена'}")
            lines.append(f"{prefix}Диагноз: {patient.get('diagnosis', '') or 'не найден'}")
        return "\n".join(lines)

    def _format_patient_list_result(self, result: BarsPatientListResult) -> str:
        lines = [result.message]
        if result.department:
            lines.append(f"Отделение: {result.department}")

        patients = result.patients or []
        if patients:
            lines.append("")
            for index, patient in enumerate(patients, start=1):
                line = f"{index}. {patient.get('history_number', '')} - {patient.get('full_name', '')}"
                details = []
                if patient.get("age"):
                    details.append(patient["age"])
                if patient.get("birthdate"):
                    details.append(patient["birthdate"])
                if patient.get("doctor"):
                    details.append(f"врач: {patient['doctor']}")
                if details:
                    line = f"{line} ({', '.join(details)})"
                lines.append(line)
                lines.append(f"   Диагноз: {patient.get('diagnosis', '') or 'не найден'}")

        if result.text_preview and not patients:
            lines.append("")
            lines.append("Контекст:")
            lines.append(result.text_preview)
        return "\n".join(lines)

    def _show_text_result(self, text: str):
        self.patient_scroll.hide()
        self.labs_panel.hide()
        self.probe_text.show()
        self.probe_text.setPlainText(text)

    def _clear_patient_cards(self):
        self._patient_card_buttons = []
        while self.patient_cards_layout.count() > 0:
            item = self.patient_cards_layout.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.deleteLater()

    def _show_patient_cards(self, result: BarsPatientListResult):
        self._clear_patient_cards()
        patients = result.patients or []
        for index, patient in enumerate(patients, start=1):
            self.patient_cards_layout.addWidget(self._build_patient_card(index, patient))
        self.patient_cards_layout.addStretch()
        self.probe_text.hide()
        self.labs_panel.hide()
        self.patient_scroll.show()

    def _build_patient_card(self, index: int, patient: dict) -> QFrame:
        card = QFrame()
        card.setObjectName("BarsPatientCard")
        card.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Minimum)
        card.setStyleSheet(
            """
            QFrame#BarsPatientCard {
                background: #ffffff;
                border: 1px solid #dee2e6;
                border-radius: 6px;
            }
            QFrame#BarsPatientCard QLabel {
                background: transparent;
                color: #2c3e50;
            }
            """
        )
        layout = QVBoxLayout(card)
        layout.setContentsMargins(12, 10, 12, 10)
        layout.setSpacing(6)

        header_layout = QHBoxLayout()
        full_name = patient.get("full_name", "") or "Без ФИО"
        name_label = QLabel(f"{index}. {full_name}")
        name_label.setWordWrap(True)
        name_label.setStyleSheet("font-size: 14px; font-weight: bold;")

        mode_combo = QComboBox()
        mode_combo.addItem("Свежие", "latest")
        mode_combo.addItem("Все анализы", "all")
        mode_combo.addItem("Динамика", "dynamics")
        mode_combo.setMinimumWidth(135)

        btn_labs = QPushButton("Анализы")
        btn_labs.setObjectName("DialogOkBtn")
        btn_labs.clicked.connect(
            lambda _checked=False, item=dict(patient), combo=mode_combo: self._load_patient_labs_async(
                item,
                combo.currentData() or "latest",
            )
        )
        self._patient_card_buttons.extend([btn_labs, mode_combo])

        header_layout.addWidget(name_label, 1)
        header_layout.addWidget(mode_combo)
        header_layout.addWidget(btn_labs)

        history_label = QLabel(f"Номер ИБ: {patient.get('history_number', '') or 'не найден'}")
        history_label.setWordWrap(True)
        age = patient.get("age") or ""
        birthdate = patient.get("birthdate") or ""
        age_parts = [part for part in [age, birthdate] if part]
        age_label = QLabel(f"Возраст: {', '.join(age_parts) if age_parts else 'не найден'}")
        age_label.setWordWrap(True)
        diagnosis_label = QLabel(f"Диагноз: {patient.get('diagnosis', '') or 'не найден'}")
        diagnosis_label.setWordWrap(True)

        layout.addLayout(header_layout)
        layout.addWidget(history_label)
        layout.addWidget(age_label)
        layout.addWidget(diagnosis_label)
        return card

    def _set_patient_card_buttons_enabled(self, enabled: bool):
        for widget in list(self._patient_card_buttons):
            try:
                widget.setEnabled(enabled)
            except RuntimeError:
                continue

    def _show_labs_panel(self, patient: dict, mode: str, text: str):
        self.probe_text.hide()
        self.patient_scroll.hide()
        self.labs_panel.show()
        self.labs_title.setText(self._labs_title(patient, mode))
        self.labs_text.setPlainText(text)

    def _back_to_patient_cards(self):
        self.labs_panel.hide()
        if self._last_patient_list_result and self._last_patient_list_result.patients:
            self.probe_text.hide()
            self.patient_scroll.show()
        else:
            self.patient_scroll.hide()
            self.probe_text.show()

    def _labs_title(self, patient: dict, mode: str) -> str:
        mode_label = self._labs_mode_label(mode)
        full_name = patient.get("full_name") or "пациент"
        history_number = patient.get("history_number") or ""
        return f"Анализы: {full_name} | {history_number} | {mode_label}"

    def _labs_title_for_result(self, result: BarsPatientLabsResult) -> str:
        return self._labs_title(result.patient or {}, result.mode)

    @staticmethod
    def _labs_mode_label(mode: str) -> str:
        return {
            "all": "все анализы",
            "latest": "свежие",
            "dynamics": "динамика",
        }.get(mode, "свежие")

    def _format_patient_labs_result(self, result: BarsPatientLabsResult) -> str:
        patient = result.patient or {}
        labs = result.labs or []
        lines = [
            result.message,
            f"Пациент: {patient.get('full_name', '') or 'не указан'}",
            f"Номер ИБ: {patient.get('history_number', '') or 'не указан'}",
            f"Режим: {self._labs_mode_label(result.mode)}",
        ]
        if not labs:
            if result.text_preview:
                lines.extend(["", "Контекст:", result.text_preview])
            return "\n".join(lines)

        lines.append("")
        if result.mode == "dynamics":
            for index, item in enumerate(labs, start=1):
                lines.append(f"{index}. {item.get('indicator', '')}")
                timeline = item.get("timeline") or []
                for value in timeline:
                    date = value.get("date", "")
                    result_value = value.get("value", "")
                    analysis = value.get("analysis", "")
                    suffix = f" | {analysis}" if analysis else ""
                    lines.append(f"   {date}: {result_value}{suffix}")
                lines.append("")
            return "\n".join(lines).rstrip()

        for index, item in enumerate(labs, start=1):
            lines.append(f"{index}. {item.get('date', '')} | {item.get('name', '')}")
            result_text = item.get("result", "") or "результат не заполнен"
            if result.mode == "latest":
                indicators = split_bars_laboratory_result_items(result_text)
                if indicators:
                    for indicator, value in indicators:
                        lines.append(f"   {indicator} - {value}")
                else:
                    lines.append(f"   {result_text}")
            else:
                lines.append(f"   Результат: {result_text}")
            if item.get("doctor"):
                lines.append(f"   Врач: {item['doctor']}")
            lines.append("")
        return "\n".join(lines).rstrip()

    def _on_worker_failed(self, exc):
        self.status_label.setText(f"Ошибка: {exc}")

    def reject(self):
        self._poll_timer.stop()
        super().reject()
