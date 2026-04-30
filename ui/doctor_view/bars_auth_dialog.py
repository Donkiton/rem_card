from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QHBoxLayout, QLabel, QLineEdit, QPlainTextEdit, QPushButton

from rem_card.services.bars_auth_service import (
    BarsAuthCheckResult,
    BarsAuthService,
    BarsPatientListResult,
    BarsPatientProbeResult,
)
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
        self._background_worker = None
        self._auto_accept_scheduled = False

        self._init_content()

        self._poll_timer = QTimer(self)
        self._poll_timer.setInterval(1800)
        self._poll_timer.timeout.connect(self._check_authorized_async)

        QTimer.singleShot(0, self._open_browser_async)

    def _init_content(self):
        self.status_label = QLabel("Открываю БАРС...")
        self.status_label.setWordWrap(True)
        self.status_label.setMinimumWidth(460)
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

        self.btn_manual_done = QPushButton("Я вошел")
        self.btn_manual_done.setObjectName("DialogOkBtn")
        self.btn_manual_done.clicked.connect(self._mark_authorized_manually)

        self.btn_close = QPushButton("Закрыть")
        self.btn_close.setObjectName("DialogOkBtn")
        self.btn_close.clicked.connect(self.reject)

        button_layout.addWidget(self.btn_check)
        button_layout.addWidget(self.btn_manual_done)
        button_layout.addWidget(self.btn_close)

        search_layout = QHBoxLayout()
        self.history_input = QLineEdit()
        self.history_input.setPlaceholderText("Номер истории")
        self.history_input.returnPressed.connect(self._probe_patient_async)
        self.btn_patient_probe = QPushButton("Проба ФИО")
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
        self.probe_text.setPlaceholderText("Здесь появится результат поиска ФИО.")
        self.probe_text.setMinimumHeight(170)
        self.probe_text.setStyleSheet("font-size: 12px; color: #2c3e50; background: #ffffff;")

        self.content_layout.addWidget(self.status_label)
        self.content_layout.addWidget(self.hint_label)
        self.content_layout.addLayout(button_layout)
        self.content_layout.addLayout(search_layout)
        self.content_layout.addWidget(self.probe_text)
        self.resize(680, 380)

    def _set_busy(self, busy: bool):
        self.btn_check.setEnabled(not busy)
        self.btn_patient_probe.setEnabled(not busy)
        self.btn_patient_list.setEnabled(not busy)

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
            self.status_label.setText("Введите номер истории")
            return
        self._set_busy(True)
        self.status_label.setText("Пробую найти ФИО по номеру истории...")
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
        self.probe_text.setPlainText(self._format_patient_probe_result(result))
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
        self.probe_text.setPlainText(self._format_patient_list_result(result))
        if result.ok:
            self.authorized = True

    def _mark_authorized_manually(self):
        result = self.auth_service.mark_authorized_manually()
        self._complete_authorization(result)

    def _complete_authorization(self, result: BarsAuthCheckResult):
        self.authorized = True
        self._poll_timer.stop()
        self.status_label.setText(f"{result.message}. Служебная сессия подготовлена для работы из РЕМКАРТЫ.")
        self.btn_close.setText("Готово")
        self._prepare_background_session_async()

    def _prepare_background_session_async(self):
        if self._background_worker and self._background_worker.isRunning():
            return
        self._background_worker = AsyncCallThread(self.auth_service.prepare_background_session, parent=self)
        self._background_worker.failed.connect(lambda exc: None)
        self._background_worker.start()

    def _format_patient_probe_result(self, result: BarsPatientProbeResult) -> str:
        lines = [result.message]
        if result.history_number:
            lines.append(f"Номер истории: {result.history_number}")
        if result.full_name:
            lines.append(f"ФИО: {result.full_name}")
        if result.matched_line:
            lines.append("")
            lines.append("Строка совпадения:")
            lines.append(result.matched_line)
        if result.text_preview:
            lines.append("")
            lines.append("Фрагмент прочитанного текста:")
            lines.append(result.text_preview)
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
                if patient.get("diagnosis"):
                    lines.append(f"   {patient['diagnosis']}")

        if result.text_preview and not patients:
            lines.append("")
            lines.append("Контекст:")
            lines.append(result.text_preview)
        return "\n".join(lines)

    def _on_worker_failed(self, exc):
        self.status_label.setText(f"Ошибка: {exc}")

    def reject(self):
        self._poll_timer.stop()
        super().reject()
