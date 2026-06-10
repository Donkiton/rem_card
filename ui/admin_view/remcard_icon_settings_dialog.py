from __future__ import annotations

import os
from dataclasses import asdict

from PySide6.QtCore import QRect, QSize, Qt
from PySide6.QtGui import QPainter, QPixmap
from PySide6.QtWidgets import (
    QApplication,
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
    QWidget,
)

from rem_card.services.remcard_icon_defaults import REMCARD_ICON_DEFINITIONS, RemCardIconDefinition
from rem_card.services.settings.settings_service import get_settings_service
from rem_card.ui.shared.base_dialog import BaseStyledDialog
from rem_card.ui.shared.custom_message_box import CustomMessageBox
from rem_card.ui.shared.remcard_icon_settings import (
    current_remcard_icon_source,
    invalidate_remcard_icon_cache,
    load_remcard_icon_pixmap,
)


class IconPreviewLabel(QLabel):
    def __init__(self, parent=None):
        super().__init__(parent)
        self._source_pixmap = QPixmap()
        self.setAlignment(Qt.AlignCenter)
        self.setMinimumSize(150, 130)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)

    def sizeHint(self) -> QSize:
        return QSize(180, 140)

    def set_preview_pixmap(self, pixmap: QPixmap):
        self._source_pixmap = QPixmap(pixmap)
        self.setText("")
        self.update()

    def show_preview_message(self, text: str):
        self._source_pixmap = QPixmap()
        self.setText(text)
        self.update()

    def paintEvent(self, event):
        super().paintEvent(event)
        if self._source_pixmap.isNull():
            return
        available = self.contentsRect().adjusted(10, 10, -10, -10)
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


def _descriptor_from_definition(definition: RemCardIconDefinition) -> dict:
    data = asdict(definition)
    data["icon_keys"] = [definition.icon_key]
    return data


class RemCardIconSettingsDialog(BaseStyledDialog):
    def __init__(self, parent=None):
        super().__init__("Настройка иконок рем карты", parent)
        self._selected_descriptor: dict | None = None
        self._pending_image_path = ""
        self._ui_ready = False
        self._icon_records = self._load_icon_records()
        self.resize(760, 500)
        self._setup_ui()
        self._select_first_item()

    def _setup_ui(self):
        layout = self.content_layout
        layout.setSpacing(12)

        body = QHBoxLayout()
        body.setSpacing(14)
        layout.addLayout(body, 1)

        list_page = QWidget()
        list_layout = QVBoxLayout(list_page)
        list_layout.setContentsMargins(8, 8, 8, 8)
        list_layout.setSpacing(8)
        list_layout.addWidget(QLabel("Иконки предпросмотра карточки пациента"))
        self.icon_list = QListWidget()
        self.icon_list.setObjectName("RemCardIconList")
        list_layout.addWidget(self.icon_list, 1)
        self._populate_icon_list()
        self.icon_list.currentRowChanged.connect(self._on_icon_selected)
        body.addWidget(list_page, 0)

        detail = QFrame()
        detail.setObjectName("RemCardIconDetail")
        detail_layout = QVBoxLayout(detail)
        detail_layout.setContentsMargins(14, 14, 14, 14)
        detail_layout.setSpacing(10)

        self.title_label = QLabel()
        self.title_label.setObjectName("RemCardIconTitle")
        self.title_label.setWordWrap(True)
        detail_layout.addWidget(self.title_label)

        self.description_label = QLabel()
        self.description_label.setWordWrap(True)
        detail_layout.addWidget(self.description_label)

        self.file_label = QLabel()
        self.file_label.setTextInteractionFlags(Qt.TextSelectableByMouse)
        detail_layout.addWidget(self.file_label)

        previews = QHBoxLayout()
        previews.setSpacing(12)
        previews.addWidget(self._preview_block("Текущая иконка", current=True), 1)
        previews.addWidget(self._preview_block("Новая иконка", current=False), 1)
        detail_layout.addLayout(previews)

        buttons = QHBoxLayout()
        buttons.addStretch(1)
        self.change_btn = QPushButton("Изменить")
        self.change_btn.setObjectName("DialogOkBtn")
        self.change_btn.clicked.connect(self._choose_icon)
        self.cancel_btn = QPushButton("Отмена")
        self.cancel_btn.setObjectName("DialogOkBtn")
        self.cancel_btn.clicked.connect(self._cancel_pending)
        self.save_btn = QPushButton("Сохранить")
        self.save_btn.setObjectName("DialogOkBtn")
        self.save_btn.clicked.connect(self._save_icon)
        buttons.addWidget(self.change_btn)
        buttons.addWidget(self.cancel_btn)
        buttons.addWidget(self.save_btn)
        detail_layout.addLayout(buttons)

        detail_layout.addStretch(1)
        body.addWidget(detail, 1)

        footer = QHBoxLayout()
        footer.addStretch(1)
        close_btn = QPushButton("Закрыть")
        close_btn.setObjectName("DialogOkBtn")
        close_btn.clicked.connect(self.accept)
        footer.addWidget(close_btn)
        layout.addLayout(footer)

        self.setStyleSheet(
            self.styleSheet()
            + """
            QFrame#RemCardIconDetail {
                background-color: #ffffff;
                border: 1px solid #c7d1da;
                border-radius: 6px;
            }
            QLabel#RemCardIconTitle {
                font-size: 15px;
                font-weight: 700;
                color: #2c3e50;
            }
            QLabel#RemCardIconPreviewTitle {
                font-weight: 600;
                color: #2c3e50;
            }
            QLabel#RemCardIconPreview {
                background-color: #f5f8fb;
                border: 1px solid #d7dfe7;
                border-radius: 5px;
                color: #6c7a89;
            }
            QListWidget#RemCardIconList {
                min-width: 300px;
                background-color: #ffffff;
                border: 1px solid #d7dfe7;
                border-radius: 5px;
                outline: 0;
            }
            QListWidget#RemCardIconList::item {
                padding: 8px;
                border-bottom: 1px solid #edf1f5;
            }
            QListWidget#RemCardIconList::item:selected {
                background-color: #d7eaf8;
                color: #1f3447;
            }
            """
        )
        self._ui_ready = True

    def _preview_block(self, title: str, *, current: bool) -> QWidget:
        block = QWidget()
        layout = QVBoxLayout(block)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(6)
        label = QLabel(title)
        label.setObjectName("RemCardIconPreviewTitle")
        layout.addWidget(label)
        preview = IconPreviewLabel()
        preview.setObjectName("RemCardIconPreview")
        layout.addWidget(preview)
        source = QLabel()
        source.setWordWrap(True)
        source.setTextInteractionFlags(Qt.TextSelectableByMouse)
        layout.addWidget(source)
        if current:
            self.current_preview = preview
            self.current_source_label = source
        else:
            self.new_preview = preview
            self.new_source_label = source
        return block

    def _populate_icon_list(self):
        self.icon_list.clear()
        for definition in REMCARD_ICON_DEFINITIONS:
            text = f"{definition.name}\n{definition.description or definition.default_file}"
            item = QListWidgetItem(text)
            item.setData(Qt.UserRole, _descriptor_from_definition(definition))
            self.icon_list.addItem(item)

    def _load_icon_records(self) -> dict[str, dict]:
        try:
            records = get_settings_service().list_remcard_icons()
        except Exception:
            return {}
        return records if isinstance(records, dict) else {}

    def _select_first_item(self):
        if self.icon_list.count():
            self.icon_list.setCurrentRow(0)
            self._on_icon_selected(self.icon_list.currentRow())

    def _on_icon_selected(self, row: int):
        if not self._ui_ready:
            return
        item = self.icon_list.item(row)
        descriptor = item.data(Qt.UserRole) if item is not None else None
        self._set_descriptor(descriptor if isinstance(descriptor, dict) else None)

    def _set_descriptor(self, descriptor: dict | None):
        self._selected_descriptor = dict(descriptor or {}) if descriptor else None
        self._pending_image_path = ""
        if hasattr(self, "save_btn"):
            self.save_btn.setText("Сохранить")
        self._refresh_detail()

    def _refresh_detail(self):
        descriptor = self._selected_descriptor
        if not descriptor:
            self.title_label.setText("Иконка не выбрана")
            self.description_label.clear()
            self.file_label.clear()
            self.current_preview.show_preview_message("Нет выбора")
            self.current_source_label.clear()
            self.new_preview.show_preview_message("Новая иконка не выбрана")
            self.new_source_label.clear()
            self.change_btn.setEnabled(False)
            self.save_btn.setEnabled(False)
            self.cancel_btn.setEnabled(False)
            return

        icon_keys = descriptor.get("icon_keys") or [descriptor.get("icon_key")]
        fallback_file = str(descriptor.get("default_file") or "")
        self.title_label.setText(str(descriptor.get("name") or "Иконка"))
        self.description_label.setText(str(descriptor.get("description") or ""))
        self.file_label.setText(f"Стандартный файл: {fallback_file}")
        current_pixmap = load_remcard_icon_pixmap(icon_keys, fallback_file=fallback_file)
        if current_pixmap.isNull():
            self.current_preview.show_preview_message("Иконка недоступна")
        else:
            self.current_preview.set_preview_pixmap(current_pixmap)
        self.current_source_label.setText(
            f"Сейчас: {current_remcard_icon_source(icon_keys, fallback_file=fallback_file)}"
        )
        self._refresh_new_preview()
        self.change_btn.setEnabled(True)

    def _refresh_new_preview(self):
        if not self._pending_image_path:
            self.new_preview.show_preview_message("Новая иконка не выбрана")
            self.new_source_label.clear()
            self.save_btn.setEnabled(False)
            self.cancel_btn.setEnabled(False)
            return
        pixmap = QPixmap(self._pending_image_path)
        if pixmap.isNull():
            self.new_preview.show_preview_message("Файл не является изображением")
            self.save_btn.setEnabled(False)
        else:
            self.new_preview.set_preview_pixmap(pixmap)
            self.save_btn.setEnabled(True)
        self.new_source_label.setText(os.path.basename(self._pending_image_path))
        self.cancel_btn.setEnabled(True)

    def _choose_image_file(self) -> str | None:
        dialog = QFileDialog(self, "Выбрать иконку")
        dialog.setFileMode(QFileDialog.FileMode.ExistingFile)
        dialog.setNameFilter("Изображения (*.png *.jpg *.jpeg *.bmp *.gif *.webp *.svg)")
        dialog.setAcceptMode(QFileDialog.AcceptMode.AcceptOpen)
        try:
            dialog.setLabelText(QFileDialog.DialogLabel.Accept, "Выбрать")
        except Exception:
            pass
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return None
        files = dialog.selectedFiles()
        return files[0] if files else None

    def _choose_icon(self):
        if not self._selected_descriptor:
            return
        path = self._choose_image_file()
        if not path:
            return
        pixmap = QPixmap(path)
        if pixmap.isNull():
            CustomMessageBox.warning(self, "Иконки рем карты", "Выбранный файл не является изображением.")
            return
        self._pending_image_path = path
        self.save_btn.setText("Сохранить")
        self._refresh_new_preview()

    def _cancel_pending(self):
        self._pending_image_path = ""
        if hasattr(self, "new_preview"):
            self._refresh_new_preview()

    def _save_icon(self):
        descriptor = self._selected_descriptor
        if not descriptor or not self._pending_image_path:
            return
        try:
            service = get_settings_service()
            icon_key = str(descriptor.get("icon_key") or "").strip()
            service.save_remcard_icon(
                icon_key=icon_key,
                category=str(descriptor.get("category") or "remcard"),
                target_key=str(descriptor.get("target_key") or icon_key),
                name=str(descriptor.get("name") or icon_key or "Иконка"),
                default_file=str(descriptor.get("default_file") or ""),
                image_path=self._pending_image_path,
                sort_order=int(descriptor.get("sort_order") or 0),
            )
        except Exception as exc:
            CustomMessageBox.critical(self, "Ошибка", f"Не удалось сохранить иконку: {exc}")
            return
        invalidate_remcard_icon_cache()
        self._icon_records = self._load_icon_records()
        self._pending_image_path = ""
        self._refresh_detail()
        self._apply_to_open_widgets()
        self.save_btn.setText("Сохранено")

    def _apply_to_open_widgets(self):
        app = QApplication.instance()
        if app is None:
            return
        for widget in app.allWidgets():
            method = getattr(widget, "apply_remcard_icon_settings", None)
            if callable(method):
                try:
                    method()
                except Exception:
                    pass
