from __future__ import annotations

from copy import deepcopy
from datetime import date
import os
import time

from PySide6.QtCore import QDate, QRect, QSize, Qt
from PySide6.QtGui import QPainter, QPixmap
from PySide6.QtWidgets import (
    QApplication,
    QDateEdit,
    QDialog,
    QFileDialog,
    QFrame,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QPushButton,
    QSizePolicy,
    QVBoxLayout,
)

from rem_card.ui.shared.background_settings import (
    DEFAULT_BACKGROUND_ID,
    BackgroundSettingsStorage,
    active_background_entry,
    background_entry_file_path,
    background_ranges_overlap,
    copy_background_to_icon_dir,
    month_day_to_label,
    normalize_background_settings_payload,
    normalize_month_day,
)
from rem_card.ui.shared.base_dialog import BaseStyledDialog
from rem_card.ui.shared.custom_message_box import CustomMessageBox


_PREVIEW_PIXMAP_CACHE: dict[str, tuple[float | None, QPixmap]] = {}


def _load_preview_pixmap(path: str) -> QPixmap:
    normalized = os.path.abspath(os.path.normpath(str(path or "")))
    try:
        mtime = os.path.getmtime(normalized)
    except OSError:
        mtime = None

    cached = _PREVIEW_PIXMAP_CACHE.get(normalized)
    if cached is not None and cached[0] == mtime:
        return QPixmap(cached[1])

    pixmap = QPixmap(normalized)
    if not pixmap.isNull():
        _PREVIEW_PIXMAP_CACHE[normalized] = (mtime, QPixmap(pixmap))
    return pixmap


def _month_day_to_qdate(value: str) -> QDate:
    normalized = normalize_month_day(value, "01-01")
    month, day = normalized.split("-", 1)
    current_year = QDate.currentDate().year()
    result = QDate(current_year, int(month), int(day))
    if result.isValid():
        return result
    return QDate(current_year, int(month), 28)


def _qdate_to_month_day(value: QDate) -> str:
    return f"{value.month():02d}-{value.day():02d}"


class BackgroundPreviewLabel(QLabel):
    def __init__(self, parent=None):
        super().__init__(parent)
        self._source_pixmap = QPixmap()
        self.setAlignment(Qt.AlignCenter)
        self.setMinimumSize(430, 230)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)

    def sizeHint(self) -> QSize:
        return QSize(430, 230)

    def minimumSizeHint(self) -> QSize:
        return QSize(430, 230)

    def set_preview_pixmap(self, pixmap: QPixmap):
        self._source_pixmap = QPixmap(pixmap)
        self.setText("")
        self.update()

    def show_preview_message(self, text: str):
        self._source_pixmap = QPixmap()
        self.setText(text)
        self.update()

    def clear_preview(self):
        self._source_pixmap = QPixmap()
        self.setText("")
        self.update()

    def paintEvent(self, event):
        super().paintEvent(event)
        if self._source_pixmap.isNull():
            return

        available = self.contentsRect().adjusted(8, 8, -8, -8)
        if available.width() <= 0 or available.height() <= 0:
            return

        scaled = self._source_pixmap.scaled(
            available.size(),
            Qt.KeepAspectRatio,
            Qt.SmoothTransformation,
        )
        target = QRect(0, 0, scaled.width(), scaled.height())
        target.moveCenter(available.center())

        painter = QPainter(self)
        painter.setRenderHint(QPainter.SmoothPixmapTransform)
        painter.drawPixmap(target.topLeft(), scaled)


class BackgroundSettingsDialog(BaseStyledDialog):
    def __init__(self, parent=None):
        super().__init__("Изменение фона", parent)
        self.storage = BackgroundSettingsStorage()
        self.payload = self.storage.load()
        self.entries = deepcopy(self.payload.get("backgrounds") or [])
        self._loading_entry = False

        self.resize(820, 560)
        self._setup_ui()
        self._refresh_list(select_id=str(active_background_entry(self.payload).get("id") or DEFAULT_BACKGROUND_ID))

    def _setup_ui(self):
        main_layout = self.content_layout
        main_layout.setSpacing(12)

        body_layout = QHBoxLayout()
        body_layout.setSpacing(14)
        main_layout.addLayout(body_layout, 1)

        left_panel = QFrame()
        left_panel.setObjectName("BackgroundSettingsPanel")
        left_layout = QVBoxLayout(left_panel)
        left_layout.setContentsMargins(12, 12, 12, 12)
        left_layout.setSpacing(8)

        left_title = QLabel("Фоны")
        left_title.setObjectName("BackgroundSettingsTitle")
        left_layout.addWidget(left_title)

        self.backgrounds_list = QListWidget()
        self.backgrounds_list.setObjectName("BackgroundSettingsList")
        self.backgrounds_list.currentRowChanged.connect(self._on_selected_row_changed)
        left_layout.addWidget(self.backgrounds_list, 1)

        self.add_btn = QPushButton("Добавить еще один фон")
        self.add_btn.setObjectName("DialogOkBtn")
        self.add_btn.clicked.connect(self._add_background)
        left_layout.addWidget(self.add_btn)

        self.delete_btn = QPushButton("Удалить фон")
        self.delete_btn.setObjectName("DialogOkBtn")
        self.delete_btn.clicked.connect(self._delete_background)
        left_layout.addWidget(self.delete_btn)

        body_layout.addWidget(left_panel, 0)

        right_panel = QFrame()
        right_panel.setObjectName("BackgroundSettingsPanel")
        right_layout = QVBoxLayout(right_panel)
        right_layout.setContentsMargins(12, 12, 12, 12)
        right_layout.setSpacing(10)

        preview_title = QLabel("Предпросмотр текущего фона")
        preview_title.setObjectName("BackgroundSettingsTitle")
        right_layout.addWidget(preview_title)

        self.preview_label = BackgroundPreviewLabel()
        self.preview_label.setObjectName("BackgroundPreview")
        right_layout.addWidget(self.preview_label, 1)

        self.file_label = QLabel()
        self.file_label.setObjectName("BackgroundFileLabel")
        self.file_label.setTextInteractionFlags(Qt.TextSelectableByMouse)
        right_layout.addWidget(self.file_label)

        dates_row = QHBoxLayout()
        dates_row.setSpacing(8)
        dates_row.addWidget(QLabel("Используется с"))

        self.start_date_edit = QDateEdit()
        self.start_date_edit.setDisplayFormat("dd.MM")
        self.start_date_edit.setCalendarPopup(True)
        self._prepare_date_edit(self.start_date_edit)
        self.start_date_edit.dateChanged.connect(self._on_dates_changed)
        dates_row.addWidget(self.start_date_edit)
        dates_row.addWidget(QLabel("по"))
        self.end_date_edit = QDateEdit()
        self.end_date_edit.setDisplayFormat("dd.MM")
        self.end_date_edit.setCalendarPopup(True)
        self._prepare_date_edit(self.end_date_edit)
        self.end_date_edit.dateChanged.connect(self._on_dates_changed)
        dates_row.addWidget(self.end_date_edit)
        dates_row.addStretch()
        right_layout.addLayout(dates_row)

        self.load_btn = QPushButton("Загрузить новый фон")
        self.load_btn.setObjectName("DialogOkBtn")
        self.load_btn.clicked.connect(self._load_background)
        right_layout.addWidget(self.load_btn, 0, Qt.AlignLeft)

        body_layout.addWidget(right_panel, 1)

        footer = QHBoxLayout()
        footer.addStretch()
        cancel_btn = QPushButton("Отмена")
        cancel_btn.setObjectName("DialogOkBtn")
        cancel_btn.clicked.connect(self.reject)
        self.save_btn = QPushButton("Сохранить")
        self.save_btn.setObjectName("DialogOkBtn")
        self.save_btn.clicked.connect(self._save)
        footer.addWidget(cancel_btn)
        footer.addWidget(self.save_btn)
        main_layout.addLayout(footer)

        self.setStyleSheet(
            self.styleSheet()
            + """
            QFrame#BackgroundSettingsPanel {
                background-color: #ffffff;
                border: 1px solid #c7d1da;
                border-radius: 6px;
            }
            QLabel#BackgroundSettingsTitle {
                font-weight: bold;
                color: #2c3e50;
            }
            QListWidget#BackgroundSettingsList {
                background-color: #ffffff;
                border: 1px solid #d7dfe7;
                border-radius: 5px;
                outline: 0;
            }
            QListWidget#BackgroundSettingsList::item {
                padding: 8px;
                border-bottom: 1px solid #edf1f5;
            }
            QListWidget#BackgroundSettingsList::item:selected {
                background-color: #d7eaf8;
                color: #1f3447;
            }
            QLabel#BackgroundPreview {
                background-color: #f5f8fb;
                border: 1px solid #d7dfe7;
                border-radius: 5px;
                color: #6c7a89;
            }
            QLabel#BackgroundFileLabel {
                color: #2c3e50;
            }
            """
        )

    def _prepare_date_edit(self, edit: QDateEdit):
        current_year = QDate.currentDate().year()
        edit.setDateRange(QDate(current_year, 1, 1), QDate(current_year, 12, 31))
        calendar = edit.calendarWidget()
        if calendar is None:
            return
        calendar.setMinimumDate(QDate(current_year, 1, 1))
        calendar.setMaximumDate(QDate(current_year, 12, 31))
        calendar.setFirstDayOfWeek(Qt.Monday)
        calendar.setStyleSheet(
            """
            QCalendarWidget QWidget {
                background-color: #ffffff;
                color: #212529;
            }
            QCalendarWidget QWidget#qt_calendar_navigationbar {
                background-color: #f5f8fb;
                border-bottom: 1px solid #d7dfe7;
            }
            QCalendarWidget QToolButton {
                background-color: transparent;
                color: #2c3e50;
                border: none;
                border-radius: 4px;
                padding: 4px 8px;
            }
            QCalendarWidget QToolButton:hover {
                background-color: #e8f2fb;
            }
            QCalendarWidget QSpinBox {
                background-color: #ffffff;
                color: #2c3e50;
                border: 1px solid #c7d1da;
                border-radius: 4px;
                padding: 2px 4px;
            }
            QCalendarWidget QMenu {
                background-color: #ffffff;
                color: #212529;
                border: 1px solid #c7d1da;
            }
            QCalendarWidget QMenu::item:selected {
                background-color: #d7eaf8;
                color: #1f3447;
            }
            QCalendarWidget QAbstractItemView:enabled {
                background-color: #ffffff;
                color: #212529;
                selection-background-color: #d7eaf8;
                selection-color: #1f3447;
                outline: 0;
            }
            QCalendarWidget QAbstractItemView:disabled {
                color: #9aa7b3;
            }
            """
        )

    def _entry_by_id(self, entry_id: str | None) -> dict | None:
        for entry in self.entries:
            if str(entry.get("id") or "") == str(entry_id or ""):
                return entry
        return None

    def _selected_entry_id(self) -> str | None:
        item = self.backgrounds_list.currentItem()
        if item is None:
            return None
        return str(item.data(Qt.UserRole) or "")

    def _selected_entry(self) -> dict | None:
        return self._entry_by_id(self._selected_entry_id())

    def _entry_text(self, entry: dict) -> str:
        name = str(entry.get("name") or "Фон")
        start = month_day_to_label(str(entry.get("start") or "01-01"))
        end = month_day_to_label(str(entry.get("end") or "12-31"))
        file_name = str(entry.get("file") or "файл не выбран")
        return f"{name}\n{start} - {end} · {file_name}"

    def _refresh_list(self, select_id: str | None = None):
        current_id = select_id or self._selected_entry_id()
        self.backgrounds_list.blockSignals(True)
        try:
            self.backgrounds_list.clear()
            for entry in self.entries:
                item = QListWidgetItem(self._entry_text(entry))
                item.setData(Qt.UserRole, str(entry.get("id") or ""))
                self.backgrounds_list.addItem(item)

            target_row = 0
            if current_id:
                for row in range(self.backgrounds_list.count()):
                    item = self.backgrounds_list.item(row)
                    if str(item.data(Qt.UserRole) or "") == str(current_id):
                        target_row = row
                        break
            if self.backgrounds_list.count():
                self.backgrounds_list.setCurrentRow(target_row)
        finally:
            self.backgrounds_list.blockSignals(False)
        self._sync_selected_entry()

    def _on_selected_row_changed(self, *_args):
        self._sync_selected_entry()

    def _sync_selected_entry(self):
        entry = self._selected_entry()
        if entry is None:
            self.preview_label.show_preview_message("Фон не выбран")
            self.file_label.clear()
            self.load_btn.setEnabled(False)
            self.delete_btn.setEnabled(False)
            return

        self._loading_entry = True
        try:
            self.start_date_edit.setDate(_month_day_to_qdate(str(entry.get("start") or "01-01")))
            self.end_date_edit.setDate(_month_day_to_qdate(str(entry.get("end") or "12-31")))
        finally:
            self._loading_entry = False

        is_default = str(entry.get("id") or "") == DEFAULT_BACKGROUND_ID
        self.start_date_edit.setEnabled(not is_default)
        self.end_date_edit.setEnabled(not is_default)
        self.delete_btn.setEnabled(not is_default)
        self.load_btn.setEnabled(True)
        self._update_preview(entry)

    def _update_preview(self, entry: dict):
        file_name = str(entry.get("file") or "").strip()
        if not file_name:
            self.file_label.setText("Файл: не выбран")
            self.preview_label.show_preview_message("Файл не выбран")
            return

        path = background_entry_file_path(entry)
        self.file_label.setText(f"Файл: {file_name}")
        pixmap = _load_preview_pixmap(path)
        if pixmap.isNull():
            self.preview_label.show_preview_message("Файл не найден или не является изображением")
            return

        self.preview_label.set_preview_pixmap(pixmap)

    def resizeEvent(self, event):
        super().resizeEvent(event)

    def _on_dates_changed(self, *_args):
        if self._loading_entry:
            return
        entry = self._selected_entry()
        if entry is None or str(entry.get("id") or "") == DEFAULT_BACKGROUND_ID:
            return
        entry["start"] = _qdate_to_month_day(self.start_date_edit.date())
        entry["end"] = _qdate_to_month_day(self.end_date_edit.date())
        self._refresh_list(str(entry.get("id") or ""))

    def _choose_image_file(self) -> str | None:
        dialog = QFileDialog(self, "Загрузить фон")
        dialog.setFileMode(QFileDialog.FileMode.ExistingFile)
        dialog.setNameFilter("Изображения (*.png *.jpg *.jpeg *.bmp *.gif *.webp)")
        dialog.setAcceptMode(QFileDialog.AcceptMode.AcceptOpen)
        try:
            dialog.setLabelText(QFileDialog.DialogLabel.Accept, "Загрузить")
        except Exception:
            pass
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return None
        files = dialog.selectedFiles()
        return files[0] if files else None

    def _load_background(self):
        entry = self._selected_entry()
        if entry is None:
            return
        source_path = self._choose_image_file()
        if not source_path:
            return
        pixmap = QPixmap(source_path)
        if pixmap.isNull():
            CustomMessageBox.warning(self, "Изменение фона", "Выбранный файл не является изображением.")
            return
        try:
            file_name = copy_background_to_icon_dir(source_path)
        except Exception as exc:
            CustomMessageBox.critical(self, "Ошибка", f"Не удалось загрузить фон: {exc}")
            return

        entry["file"] = file_name
        if str(entry.get("id") or "") != DEFAULT_BACKGROUND_ID:
            start = month_day_to_label(str(entry.get("start") or "01-01"))
            end = month_day_to_label(str(entry.get("end") or "12-31"))
            entry["name"] = f"Фон {start}-{end}"
        self._refresh_list(str(entry.get("id") or ""))

    def _add_background(self):
        today = date.today()
        entry_id = f"background_{time.strftime('%Y%m%d_%H%M%S')}"
        entry = {
            "id": entry_id,
            "name": f"Дополнительный фон {len(self.entries)}",
            "file": "",
            "start": f"{today.month:02d}-{today.day:02d}",
            "end": f"{today.month:02d}-{today.day:02d}",
            "locked": False,
        }
        self.entries.append(entry)
        self._refresh_list(entry_id)

    def _delete_background(self):
        entry = self._selected_entry()
        if entry is None:
            return
        if str(entry.get("id") or "") == DEFAULT_BACKGROUND_ID:
            CustomMessageBox.warning(self, "Изменение фона", "Стандартный фон удалить нельзя.")
            return
        entry_id = str(entry.get("id") or "")
        self.entries = [item for item in self.entries if str(item.get("id") or "") != entry_id]
        self._refresh_list(DEFAULT_BACKGROUND_ID)

    def _validate(self) -> bool:
        normalized = normalize_background_settings_payload({"backgrounds": self.entries})
        for entry in normalized.get("backgrounds") or []:
            file_name = str(entry.get("file") or "").strip()
            if not file_name:
                CustomMessageBox.warning(self, "Изменение фона", "У каждого фона должен быть выбран файл.")
                return False
            pixmap = QPixmap(background_entry_file_path(entry))
            if pixmap.isNull():
                CustomMessageBox.warning(
                    self,
                    "Изменение фона",
                    f"Файл «{file_name}» не найден в папке icon или не является изображением.",
                )
                return False
        overlap = self._find_custom_range_overlap(normalized["backgrounds"])
        if overlap is not None:
            first, second = overlap
            CustomMessageBox.warning(
                self,
                "Изменение фона",
                "Дополнительные фоны не должны перекрывать друг друга.\n\n"
                f"Пересекаются периоды:\n"
                f"«{first.get('name')}»: {month_day_to_label(first.get('start'))} - {month_day_to_label(first.get('end'))}\n"
                f"«{second.get('name')}»: {month_day_to_label(second.get('start'))} - {month_day_to_label(second.get('end'))}",
            )
            return False
        self.entries = normalized["backgrounds"]
        return True

    def _find_custom_range_overlap(self, entries: list[dict]) -> tuple[dict, dict] | None:
        custom_entries = [
            entry
            for entry in entries
            if str(entry.get("id") or "") != DEFAULT_BACKGROUND_ID
        ]
        for first_index, first in enumerate(custom_entries):
            for second in custom_entries[first_index + 1:]:
                if background_ranges_overlap(first, second):
                    return first, second
        return None

    def _save(self):
        if not self._validate():
            return
        try:
            self.storage.save({"backgrounds": self.entries})
        except Exception as exc:
            CustomMessageBox.critical(self, "Ошибка", f"Не удалось сохранить настройки фона: {exc}")
            return

        self._apply_to_open_widgets()
        self.save_btn.setText("Сохранено")
        self._refresh_list(self._selected_entry_id())

    def _apply_to_open_widgets(self):
        app = QApplication.instance()
        if app is None:
            return
        for widget in app.allWidgets():
            method = getattr(widget, "apply_background_settings", None)
            if callable(method):
                try:
                    method()
                except Exception:
                    pass
