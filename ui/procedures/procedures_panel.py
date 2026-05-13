from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import QSize, Qt
from PySide6.QtGui import QIcon
from PySide6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from rem_card.data.dto.procedures_dto import ProcedureType
from rem_card.ui.procedures.procedure_editor_dialog import ProcedureEditorDialog
from rem_card.ui.procedures.procedure_pdf_worker import ProcedurePdfWorker
from rem_card.ui.procedures.procedures_list_widget import ProceduresListWidget
from rem_card.ui.shared.custom_message_box import CustomMessageBox
from rem_card.ui.shared.pdf_opener import open_pdf_file


class ProceduresPanel(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.remcard_service = None
        self.admission_id = None
        self._pdf_worker = None
        self._write_pending = False
        self._build_ui()

    def _build_ui(self):
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        frame = QFrame()
        frame.setObjectName("procedures_frame")
        frame.setStyleSheet(
            """
            QFrame#procedures_frame {
                border: none;
                background-color: transparent;
            }
            QLabel#procedures_title {
                background-color: #e9ecef;
                border-top: 1.5px solid #bdc3c7;
                border-left: 1.5px solid #bdc3c7;
                border-right: 1.5px solid #bdc3c7;
                border-bottom: 0.5px solid #bdc3c7;
                border-top-left-radius: 5px;
                border-top-right-radius: 5px;
                padding: 0;
                font-weight: bold;
                font-size: 14px;
                color: #2c3e50;
            }
            QWidget#procedures_body {
                background-color: #f8f9fa;
                border-left: 1.5px solid #bdc3c7;
                border-right: 1.5px solid #bdc3c7;
                border-bottom: 1.5px solid #bdc3c7;
                border-top: none;
                border-bottom-left-radius: 5px;
                border-bottom-right-radius: 5px;
            }
            QPushButton#ProcedureCreateButton,
            QPushButton#ProcedureCreatePrimaryButton {
                background: #eef3f8;
                color: #172033;
                border: 1px solid #aebccd;
                border-radius: 6px;
                padding: 6px 12px;
                min-height: 34px;
                font-weight: 700;
            }
            QPushButton#ProcedureCreateButton:hover,
            QPushButton#ProcedureCreatePrimaryButton:hover {
                background: #e2ebf5;
                border-color: #7aa6d8;
            }
            QPushButton#ProcedureCreateButton:pressed,
            QPushButton#ProcedureCreatePrimaryButton:pressed {
                background: #d5e2ef;
                padding-top: 7px;
                padding-bottom: 5px;
            }
            QPushButton#ProcedureCreatePrimaryButton {
                background: #eaf4ff;
                border-color: #7faedc;
            }
            """
        )
        root.addWidget(frame, 1)

        layout = QVBoxLayout(frame)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        self.title_label = QLabel("Процедуры пациента")
        self.title_label.setObjectName("procedures_title")
        self.title_label.setAlignment(Qt.AlignCenter)
        self.title_label.setFixedHeight(30)
        layout.addWidget(self.title_label)

        body = QWidget()
        body.setObjectName("procedures_body")
        body.setAttribute(Qt.WA_StyledBackground, True)
        body_layout = QVBoxLayout(body)
        body_layout.setContentsMargins(12, 10, 12, 12)
        body_layout.setSpacing(10)
        layout.addWidget(body, 1)

        buttons = QHBoxLayout()
        self.add_cvc_btn = self._create_procedure_button("ЦВК", icon_name="cvk.png", primary=True)
        self.add_cvc_btn.clicked.connect(self._create_cvc)
        self.add_lp_btn = self._create_procedure_button("+ Люмбальная пункция", icon_name="lumbpunk.png")
        self.add_lp_btn.setToolTip("Форма будет добавлена следующим changeset-ом.")
        self.add_lp_btn.clicked.connect(lambda: self._show_not_implemented("Люмбальная пункция"))
        self.add_transfusion_btn = self._create_procedure_button("+ Гемотрансфузия", icon_name="balans_blood.png")
        self.add_transfusion_btn.setToolTip("Форма будет добавлена следующим changeset-ом.")
        self.add_transfusion_btn.clicked.connect(lambda: self._show_not_implemented("Гемотрансфузия"))
        self.refresh_btn = QPushButton("Обновить")
        self.refresh_btn.clicked.connect(self.refresh)
        buttons.addWidget(self.add_cvc_btn)
        buttons.addWidget(self.add_lp_btn)
        buttons.addWidget(self.add_transfusion_btn)
        buttons.addStretch(1)
        buttons.addWidget(self.refresh_btn)
        body_layout.addLayout(buttons)

        self.list_widget = ProceduresListWidget()
        self.list_widget.open_requested.connect(self._open_procedure)
        self.list_widget.print_requested.connect(self._print_protocol_from_list)
        self.list_widget.cancel_requested.connect(self._cancel_procedure)
        body_layout.addWidget(self.list_widget, 1)

        self.status_label = QLabel("")
        body_layout.addWidget(self.status_label)

    def _create_procedure_button(self, text: str, *, icon_name: str = "", primary: bool = False) -> QPushButton:
        button = QPushButton(text)
        button.setObjectName("ProcedureCreatePrimaryButton" if primary else "ProcedureCreateButton")
        button.setMinimumHeight(36)
        button.setMinimumWidth(128 if primary else 178)
        if icon_name:
            icon_path = Path(__file__).resolve().parents[2] / "icon" / icon_name
            if icon_path.exists():
                button.setIcon(QIcon(str(icon_path)))
                button.setIconSize(QSize(26, 26))
        return button

    def set_runtime_context(self, remcard_service=None, admission_id=None):
        if remcard_service is not None:
            self.remcard_service = remcard_service
        if admission_id is not None:
            self.admission_id = admission_id
        self.refresh()

    def refresh(self):
        self._resolve_runtime_context()
        if not self.remcard_service or not self.admission_id:
            self.list_widget.set_procedures([])
            return

        try:
            procedures = self.remcard_service.list_procedures(int(self.admission_id))
            self.list_widget.set_procedures(procedures)
            self.status_label.setText(f"Записей: {len(procedures)}")
        except Exception as exc:
            self.status_label.setText("Ошибка загрузки процедур.")
            CustomMessageBox.warning(self, "Процедуры", str(exc))

    def _resolve_runtime_context(self):
        widget = self.parentWidget()
        while widget is not None:
            if hasattr(widget, "remcard_service"):
                self.remcard_service = getattr(widget, "remcard_service")
            if hasattr(widget, "current_admission_id"):
                self.admission_id = getattr(widget, "current_admission_id")
            widget = widget.parentWidget()

    def _create_cvc(self):
        if not self._ensure_context():
            return
        self._open_editor(procedure_id=None, procedure_type=ProcedureType.CVC.value)

    def _show_not_implemented(self, procedure_name: str):
        self.status_label.setText(f"{procedure_name}: форма пока не реализована.")

    def _open_procedure(self, procedure_id: int):
        if not self._ensure_context():
            return
        self._open_editor(procedure_id=procedure_id, procedure_type=ProcedureType.CVC.value)

    def _open_editor(self, *, procedure_id, procedure_type: str):
        try:
            dialog = ProcedureEditorDialog(
                self.remcard_service,
                int(self.admission_id),
                procedure_id=procedure_id,
                procedure_type=procedure_type,
                parent=self,
            )
            dialog.exec()
        except Exception as exc:
            CustomMessageBox.warning(self, "Процедуры", str(exc))
        finally:
            self.refresh()

    def _print_protocol_from_list(self, procedure_id: int):
        self._print_document(int(procedure_id), "cvc_protocol")

    def _print_document(self, procedure_id: int, document_kind: str):
        if self._pdf_worker is not None and self._pdf_worker.isRunning():
            return
        try:
            pdf_path = Path(self.remcard_service.build_procedure_pdf_path(int(procedure_id), document_kind))
        except Exception as exc:
            CustomMessageBox.warning(self, "Печать", str(exc))
            return
        self.status_label.setText("Формирование PDF...")
        self._pdf_worker = ProcedurePdfWorker(
            self.remcard_service,
            int(procedure_id),
            document_kind,
            pdf_path,
            parent=self,
        )
        self._pdf_worker.completed.connect(self._on_pdf_ready)
        self._pdf_worker.failed.connect(self._on_pdf_error)
        self._pdf_worker.finished.connect(self._clear_pdf_worker)
        self._pdf_worker.start()

    def _on_pdf_ready(self, pdf_path: str):
        self.status_label.setText("PDF сформирован.")
        open_pdf_file(pdf_path, parent=self)

    def _on_pdf_error(self, message: str):
        self.status_label.setText("Ошибка PDF.")
        CustomMessageBox.warning(self, "Печать", message)

    def _clear_pdf_worker(self):
        self._pdf_worker = None

    def _cancel_procedure(self, procedure_id: int):
        if self._write_pending or not self._ensure_context():
            return
        answer = CustomMessageBox.question(
            self,
            "Отмена процедуры",
            "Отменить процедуру? Запись будет удалена.",
        )
        if answer != CustomMessageBox.Yes:
            return

        self._write_pending = True
        self.status_label.setText("Отмена процедуры...")
        service = self.remcard_service

        def operation():
            return service.cancel_procedure(int(procedure_id), updated_by="doctor")

        service.enqueue_write(
            description=f"procedure_cancel_ui:{procedure_id}",
            operation=operation,
            on_success=lambda _result: self._finish_cancel_success(),
            on_error=self._finish_cancel_error,
        )

    def _finish_cancel_success(self):
        self._write_pending = False
        self.status_label.setText("Процедура отменена.")
        self.refresh()

    def _finish_cancel_error(self, exc: Exception):
        self._write_pending = False
        self.status_label.setText("Ошибка отмены.")
        CustomMessageBox.warning(self, "Процедуры", str(exc))
        self.refresh()

    def _ensure_context(self) -> bool:
        self._resolve_runtime_context()
        if self.remcard_service and self.admission_id:
            return True
        CustomMessageBox.warning(self, "Процедуры", "Сначала выберите пациента.")
        return False
