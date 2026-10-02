"""Non-blocking doctor notification for fresh local OperBlock -> RAO handoffs."""
from __future__ import annotations

from typing import Any

from PySide6.QtCore import QObject, QTimer, Qt
from PySide6.QtWidgets import (
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QLabel,
    QMessageBox,
    QVBoxLayout,
)

from rem_card.app.logger import logger
from rem_card.services.operblock_handoff_service import OperBlockLocalRaoHandoffService


def _remcard_db(service):
    for path in (("orders_dao", "db"), ("patient_dao", "db"), ("vitals_dao", "db")):
        current = service
        try:
            for attr in path:
                current = getattr(current, attr)
        except Exception:
            continue
        if current is not None and hasattr(current, "run_write_operation"):
            return current
    return None


class _HandoffChoiceDialog(QDialog):
    def __init__(self, handoff_service: OperBlockLocalRaoHandoffService, invitation: dict[str, Any], parent=None):
        super().__init__(parent)
        self.setWindowTitle("Перевод из оперблока")
        self._service = handoff_service
        self._invitation = dict(invitation)
        layout = QVBoxLayout(self)
        payload = dict(invitation.get("payload") or {})
        layout.addWidget(QLabel(
            "Пациент после операции: "
            f"{payload.get('full_name') or 'ФИО не указано'}\n"
            "Выберите свободную койку либо свяжите с существующей картой РАО."
        ))
        form = QFormLayout()
        self.kind = QComboBox(self)
        self.kind.addItem("Свободная койка", "bed")
        self.kind.addItem("Существующая карта РАО", "admission")
        self.target = QComboBox(self)
        form.addRow("Действие", self.kind)
        form.addRow("Назначение", self.target)
        layout.addLayout(form)
        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel, parent=self)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)
        self.kind.currentIndexChanged.connect(self._reload_targets)
        self._reload_targets()

    def _reload_targets(self):
        self.target.clear()
        if self.kind.currentData() == "bed":
            for bed in self._service.list_free_beds():
                self.target.addItem(f"Койка {bed}", int(bed))
        else:
            for admission in self._service.list_active_admissions():
                self.target.addItem(
                    f"Койка {admission.get('bed_number')}: {admission.get('full_name') or ''} "
                    f"({admission.get('history_number') or 'без № истории'})",
                    int(admission["id"]),
                )

    def selected(self) -> tuple[str, int] | None:
        value = self.target.currentData()
        if value in (None, ""):
            return None
        return str(self.kind.currentData()), int(value)


class OperBlockRaoHandoffNotifier(QObject):
    """Checks only on doctor refresh ticks; it never polls or claims OpBlock work."""

    def __init__(self, remcard_service, parent):
        super().__init__(parent)
        self._parent = parent
        db = _remcard_db(remcard_service)
        self._service = OperBlockLocalRaoHandoffService(db) if db is not None else None
        self._shown_ids: set[int] = set()
        self._dialog: QMessageBox | None = None
        self._closed = False

    def close(self) -> None:
        self._closed = True
        if self._dialog is not None:
            self._dialog.close()
            self._dialog.deleteLater()
            self._dialog = None

    def check(self) -> None:
        if self._closed or self._dialog is not None or self._service is None:
            return
        try:
            invitations = self._service.list_pending()
        except Exception:
            logger.exception("Unable to read local OperBlock RAO invitations")
            return
        invitation = next((row for row in invitations if int(row["id"]) not in self._shown_ids), None)
        if invitation is None:
            return
        self._show(invitation)

    def _show(self, invitation: dict[str, Any]) -> None:
        invitation_id = int(invitation["id"])
        self._shown_ids.add(invitation_id)
        payload = dict(invitation.get("payload") or {})
        dialog = QMessageBox(QMessageBox.Information, "Перевод из оперблока", "", parent=self._parent)
        dialog.setText(
            "После операции требуется решение врача РАО:\n"
            f"{payload.get('full_name') or 'ФИО не указано'}\n"
            "Выберите койку, свяжите карту или отклоните приглашение."
        )
        choose = dialog.addButton("Выбрать койку / карту", QMessageBox.AcceptRole)
        dismiss = dialog.addButton("Отклонить", QMessageBox.DestructiveRole)
        dialog.addButton("Позже", QMessageBox.RejectRole)
        dialog.setModal(False)
        dialog.setAttribute(Qt.WA_DeleteOnClose, True)
        self._dialog = dialog

        def finished(_result: int) -> None:
            clicked = dialog.clickedButton()
            self._dialog = None
            self._shown_ids.discard(invitation_id)
            if self._closed:
                return
            try:
                if clicked is choose:
                    self._choose(invitation)
                elif clicked is dismiss:
                    self._service.dismiss(invitation_id)
                else:
                    # A window close or "Later" preserves the invitation and
                    # records a durable short snooze, rather than treating it as a rejection.
                    self._service.mark_seen(invitation_id, defer=True)
            except Exception as exc:
                QMessageBox.warning(self._parent, "Перевод из оперблока", str(exc))
            QTimer.singleShot(0, self.check)

        dialog.finished.connect(finished)
        dialog.open()

    def _choose(self, invitation: dict[str, Any]) -> None:
        form = _HandoffChoiceDialog(self._service, invitation, self._parent)
        if form.exec() != QDialog.Accepted:
            self._service.mark_seen(int(invitation["id"]), defer=True)
            return
        selected = form.selected()
        if selected is None:
            raise RuntimeError("Не выбрана койка или карта РАО.")
        kind, target_id = selected
        if kind == "bed":
            self._service.accept_to_bed(int(invitation["id"]), target_id)
        else:
            self._service.accept_to_existing_admission(int(invitation["id"]), target_id)
