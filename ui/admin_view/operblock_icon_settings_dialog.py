from __future__ import annotations

import os
from dataclasses import asdict

from PySide6.QtCore import QRect, QSize, Qt
from PySide6.QtGui import QPainter, QPixmap
from PySide6.QtWidgets import (
    QApplication,
    QComboBox,
    QDialog,
    QFileDialog,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QPushButton,
    QSizePolicy,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from rem_card.services.operblock_icon_defaults import (
    DEFAULT_ICON_DEFINITIONS,
    OperBlockIconDefinition,
    default_drug_icon_file,
    drug_icon_candidate_keys,
    drug_icon_key_for_identity,
)
from rem_card.services.operblock_medication_presets import (
    load_operblock_medication_presets,
    normalize_operblock_medication_preset_kind,
    operblock_medication_preset_display_name,
)
from rem_card.services.settings.settings_service import get_settings_service
from rem_card.ui.shared.base_dialog import BaseStyledDialog
from rem_card.ui.shared.custom_message_box import CustomMessageBox
from rem_card.ui.shared.operblock_icon_settings import (
    current_operblock_icon_source,
    invalidate_operblock_icon_cache,
    load_operblock_icon_pixmap,
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


def _descriptor_from_definition(definition: OperBlockIconDefinition) -> dict:
    data = asdict(definition)
    data["icon_keys"] = [definition.icon_key]
    return data


class OperBlockIconSettingsDialog(BaseStyledDialog):
    def __init__(self, parent=None):
        super().__init__("Настройка иконок оперблока", parent)
        self._selected_descriptor: dict | None = None
        self._pending_image_path = ""
        self._ui_ready = False
        self._drug_descriptors = self._load_drug_descriptors()
        self._icon_records = self._load_icon_records()
        self.resize(840, 560)
        self._setup_ui()
        self._select_first_item()

    def _setup_ui(self):
        layout = self.content_layout
        layout.setSpacing(12)

        body = QHBoxLayout()
        body.setSpacing(14)
        layout.addLayout(body, 1)

        self.tabs = QTabWidget()
        self.tabs.setObjectName("OperBlockIconTabs")

        type_page = QWidget()
        type_layout = QVBoxLayout(type_page)
        type_layout.setContentsMargins(8, 8, 8, 8)
        self.type_list = QListWidget()
        self.type_list.setObjectName("OperBlockIconList")
        type_layout.addWidget(self.type_list, 1)
        self._populate_type_list()
        self.type_list.currentRowChanged.connect(self._on_type_selected)
        self.tabs.addTab(type_page, "Основные")

        drug_page = QWidget()
        drug_layout = QVBoxLayout(drug_page)
        drug_layout.setContentsMargins(8, 8, 8, 8)
        drug_layout.setSpacing(8)

        drug_layout.addWidget(QLabel("Поиск препарата"))
        self.drug_search = QLineEdit()
        self.drug_search.setObjectName("OperBlockDrugIconSearch")
        self.drug_search.setPlaceholderText("Введите название препарата...")
        drug_layout.addWidget(self.drug_search)

        self.changed_drug_label = QLabel("Измененные препараты")
        self.changed_drug_label.setObjectName("OperBlockChangedDrugIconLabel")
        drug_layout.addWidget(self.changed_drug_label)
        self.changed_drug_list = QListWidget()
        self.changed_drug_list.setObjectName("OperBlockChangedDrugIconList")
        self.changed_drug_list.setMaximumHeight(150)
        drug_layout.addWidget(self.changed_drug_list)

        drug_layout.addWidget(QLabel("Все препараты"))
        self.drug_combo = QComboBox()
        self.drug_combo.setObjectName("OperBlockDrugIconCombo")
        drug_layout.addWidget(self.drug_combo)
        self._populate_drug_combo()
        self._populate_changed_drug_list()
        self.drug_search.textChanged.connect(self._on_drug_search_changed)
        self.changed_drug_list.currentRowChanged.connect(self._on_changed_drug_selected)
        self.drug_combo.currentIndexChanged.connect(self._on_drug_selected)
        self.drug_combo.activated.connect(self._on_drug_selected)
        drug_layout.addStretch(1)
        self.tabs.addTab(drug_page, "Препараты")

        body.addWidget(self.tabs, 0)

        detail = QFrame()
        detail.setObjectName("OperBlockIconDetail")
        detail_layout = QVBoxLayout(detail)
        detail_layout.setContentsMargins(14, 14, 14, 14)
        detail_layout.setSpacing(10)

        self.title_label = QLabel()
        self.title_label.setObjectName("OperBlockIconTitle")
        self.title_label.setWordWrap(True)
        detail_layout.addWidget(self.title_label)

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
            QFrame#OperBlockIconDetail {
                background-color: #ffffff;
                border: 1px solid #c7d1da;
                border-radius: 6px;
            }
            QLabel#OperBlockIconTitle {
                font-size: 15px;
                font-weight: 700;
                color: #2c3e50;
            }
            QLabel#OperBlockIconPreviewTitle {
                font-weight: 600;
                color: #2c3e50;
            }
            QLabel#OperBlockIconPreview {
                background-color: #f5f8fb;
                border: 1px solid #d7dfe7;
                border-radius: 5px;
                color: #6c7a89;
            }
            QListWidget#OperBlockIconList {
                min-width: 300px;
                background-color: #ffffff;
                border: 1px solid #d7dfe7;
                border-radius: 5px;
                outline: 0;
            }
            QListWidget#OperBlockIconList::item {
                padding: 8px;
                border-bottom: 1px solid #edf1f5;
            }
            QListWidget#OperBlockIconList::item:selected {
                background-color: #d7eaf8;
                color: #1f3447;
            }
            QComboBox#OperBlockDrugIconCombo {
                min-width: 300px;
                min-height: 32px;
                background-color: #ffffff;
                border: 1px solid #c7d1da;
                border-radius: 5px;
                padding: 4px 8px;
            }
            QLineEdit#OperBlockDrugIconSearch {
                min-width: 300px;
                min-height: 32px;
                background-color: #ffffff;
                border: 1px solid #c7d1da;
                border-radius: 5px;
                padding: 4px 8px;
            }
            QLabel#OperBlockChangedDrugIconLabel {
                margin-top: 8px;
                font-weight: 600;
                color: #2c3e50;
            }
            QListWidget#OperBlockChangedDrugIconList {
                min-width: 300px;
                background-color: #ffffff;
                border: 1px solid #d7dfe7;
                border-radius: 5px;
                outline: 0;
            }
            QListWidget#OperBlockChangedDrugIconList::item {
                padding: 7px;
                border-bottom: 1px solid #edf1f5;
            }
            QListWidget#OperBlockChangedDrugIconList::item:selected {
                background-color: #d7eaf8;
                color: #1f3447;
            }
            """
        )
        self._ui_ready = True
        self.tabs.currentChanged.connect(self._on_tab_changed)

    def _preview_block(self, title: str, *, current: bool) -> QWidget:
        block = QWidget()
        layout = QVBoxLayout(block)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(6)
        label = QLabel(title)
        label.setObjectName("OperBlockIconPreviewTitle")
        layout.addWidget(label)
        preview = IconPreviewLabel()
        preview.setObjectName("OperBlockIconPreview")
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

    def _populate_type_list(self):
        self.type_list.clear()
        for definition in DEFAULT_ICON_DEFINITIONS:
            item = QListWidgetItem(f"{definition.name}\n{definition.default_file}")
            item.setData(Qt.UserRole, _descriptor_from_definition(definition))
            self.type_list.addItem(item)

    def _populate_drug_combo(self):
        search_text = self._drug_search_text()
        items = [descriptor for descriptor in self._drug_descriptors if self._descriptor_matches_search(descriptor, search_text)]
        self.drug_combo.clear()
        if not self._drug_descriptors:
            self.drug_combo.addItem("Препараты не найдены", None)
            self.drug_combo.setEnabled(False)
            return
        if not items:
            self.drug_combo.addItem("Ничего не найдено", None)
            self.drug_combo.setEnabled(False)
            return
        self.drug_combo.setEnabled(True)
        for descriptor in items:
            label = descriptor.get("display_label") or descriptor.get("name") or descriptor.get("target_key")
            self.drug_combo.addItem(str(label), descriptor)

    def _load_icon_records(self) -> dict[str, dict]:
        try:
            records = get_settings_service().list_operblock_icons()
        except Exception:
            return {}
        return records if isinstance(records, dict) else {}

    def _load_drug_descriptors(self) -> list[dict]:
        try:
            presets = load_operblock_medication_presets(include_disabled=True)
        except Exception as exc:
            CustomMessageBox.warning(self, "Иконки препаратов", f"Не удалось загрузить препараты оперблока: {exc}")
            return []

        descriptors: list[dict] = []
        for index, preset in enumerate(presets or [], start=1):
            if not isinstance(preset, dict):
                continue
            display_name = operblock_medication_preset_display_name(preset)
            if not display_name:
                continue
            kind = normalize_operblock_medication_preset_kind(preset.get("kind"))
            preset_id = str(preset.get("preset_id") or "").strip()
            source_drug_id = str(preset.get("source_drug_id") or "").strip()
            icon_key = drug_icon_key_for_identity(
                preset_id=preset_id,
                source_drug_id=source_drug_id,
                label=display_name,
            )
            candidates = drug_icon_candidate_keys(
                preset_id=preset_id,
                source_drug_id=source_drug_id,
                label=display_name,
            )
            descriptors.append(
                {
                    "icon_key": icon_key,
                    "icon_keys": candidates,
                    "category": "drug",
                    "target_key": preset_id or source_drug_id or display_name,
                    "name": f"Иконка препарата: {display_name}",
                    "display_label": f"{display_name} · {kind}",
                    "default_file": default_drug_icon_file(kind),
                    "sort_order": 10000 + index,
                }
            )
        descriptors.sort(key=lambda item: str(item.get("display_label") or "").casefold())
        return descriptors

    def _drug_search_text(self) -> str:
        if not hasattr(self, "drug_search"):
            return ""
        return str(self.drug_search.text() or "").strip().casefold()

    def _descriptor_matches_search(self, descriptor: dict, search_text: str) -> bool:
        if not search_text:
            return True
        values = [
            descriptor.get("display_label"),
            descriptor.get("name"),
            descriptor.get("target_key"),
            descriptor.get("icon_key"),
            *(descriptor.get("icon_keys") or []),
        ]
        haystack = " ".join(str(value or "") for value in values).casefold()
        return search_text in haystack

    def _icon_record_for_descriptor(self, descriptor: dict | None) -> dict | None:
        if not descriptor:
            return None
        for key in descriptor.get("icon_keys") or [descriptor.get("icon_key")]:
            record = self._icon_records.get(str(key or "").strip())
            if isinstance(record, dict) and record.get("image_blob"):
                return record
        return None

    def _changed_drug_descriptors(self) -> list[tuple[dict, dict]]:
        search_text = self._drug_search_text()
        result: list[tuple[dict, dict]] = []
        seen_keys: set[str] = set()
        for descriptor in self._drug_descriptors:
            if not self._descriptor_matches_search(descriptor, search_text):
                continue
            record = self._icon_record_for_descriptor(descriptor)
            if not record:
                continue
            icon_key = str(descriptor.get("icon_key") or "").strip()
            if icon_key in seen_keys:
                continue
            seen_keys.add(icon_key)
            result.append((descriptor, record))
        return result

    @staticmethod
    def _changed_drug_item_text(descriptor: dict, record: dict) -> str:
        label = str(descriptor.get("display_label") or descriptor.get("name") or descriptor.get("target_key") or "")
        value = record.get("value") if isinstance(record.get("value"), dict) else {}
        source_file = str(value.get("source_file") or "").strip()
        if not source_file:
            source_file = str(record.get("image_hash") or record.get("icon_key") or "").strip()
        return f"{label}\n{source_file or 'из БД'}"

    def _populate_changed_drug_list(self):
        if not hasattr(self, "changed_drug_list"):
            return
        self.changed_drug_list.blockSignals(True)
        self.changed_drug_list.clear()
        changed = self._changed_drug_descriptors()
        if not changed:
            item = QListWidgetItem("Измененных препаратов нет")
            item.setData(Qt.UserRole, None)
            item.setFlags(item.flags() & ~Qt.ItemIsSelectable & ~Qt.ItemIsEnabled)
            self.changed_drug_list.addItem(item)
            self.changed_drug_list.setEnabled(False)
        else:
            self.changed_drug_list.setEnabled(True)
            for descriptor, record in changed:
                item = QListWidgetItem(self._changed_drug_item_text(descriptor, record))
                item.setData(Qt.UserRole, descriptor)
                self.changed_drug_list.addItem(item)
        self.changed_drug_list.blockSignals(False)

    def _select_first_item(self):
        if self.type_list.count():
            self.type_list.setCurrentRow(0)
            self._on_type_selected(self.type_list.currentRow())
            return
        if self.drug_combo.count():
            self.tabs.setCurrentIndex(1)
            self._on_drug_selected(self.drug_combo.currentIndex())

    def _on_tab_changed(self, index: int):
        if not self._ui_ready:
            return
        self._cancel_pending()
        if index == 0:
            self._on_type_selected(self.type_list.currentRow())
        else:
            self._on_drug_selected(self.drug_combo.currentIndex())

    def _on_type_selected(self, row: int):
        if not self._ui_ready:
            return
        if self.tabs.currentIndex() != 0:
            return
        item = self.type_list.item(row)
        descriptor = item.data(Qt.UserRole) if item is not None else None
        self._set_descriptor(descriptor if isinstance(descriptor, dict) else None)

    def _on_drug_selected(self, index: int):
        if not self._ui_ready:
            return
        if self.tabs.currentIndex() != 1:
            return
        descriptor = self.drug_combo.itemData(index)
        self._set_descriptor(descriptor if isinstance(descriptor, dict) else None)

    def _on_changed_drug_selected(self, row: int):
        if not self._ui_ready:
            return
        if self.tabs.currentIndex() != 1:
            return
        item = self.changed_drug_list.item(row)
        descriptor = item.data(Qt.UserRole) if item is not None else None
        if isinstance(descriptor, dict):
            self._select_drug_descriptor(descriptor, clear_search=True)

    def _on_drug_search_changed(self):
        if not self._ui_ready:
            return
        current_descriptor = self._selected_descriptor if self.tabs.currentIndex() == 1 else None
        self.drug_combo.blockSignals(True)
        self._populate_drug_combo()
        self.drug_combo.blockSignals(False)
        self._populate_changed_drug_list()
        if isinstance(current_descriptor, dict) and self._set_combo_to_descriptor(current_descriptor):
            self._set_descriptor(current_descriptor)
        elif self.drug_combo.isEnabled() and self.drug_combo.count():
            self.drug_combo.blockSignals(True)
            self.drug_combo.setCurrentIndex(0)
            self.drug_combo.blockSignals(False)
            self._on_drug_selected(0)
        else:
            self._set_descriptor(None)

    def _set_combo_to_descriptor(self, descriptor: dict) -> bool:
        target_key = str(descriptor.get("icon_key") or "")
        for index in range(self.drug_combo.count()):
            item_descriptor = self.drug_combo.itemData(index)
            if not isinstance(item_descriptor, dict):
                continue
            if str(item_descriptor.get("icon_key") or "") == target_key:
                self.drug_combo.setCurrentIndex(index)
                return True
        return False

    def _select_drug_descriptor(self, descriptor: dict, *, clear_search: bool = False):
        if clear_search and hasattr(self, "drug_search") and self.drug_search.text():
            self.drug_search.blockSignals(True)
            self.drug_search.clear()
            self.drug_search.blockSignals(False)
            self.drug_combo.blockSignals(True)
            self._populate_drug_combo()
            self.drug_combo.blockSignals(False)
            self._populate_changed_drug_list()
        self._set_combo_to_descriptor(descriptor)
        self._set_descriptor(descriptor)

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
        self.file_label.setText(f"Стандартный файл: {fallback_file}")
        current_pixmap = load_operblock_icon_pixmap(icon_keys, fallback_file=fallback_file)
        if current_pixmap.isNull():
            self.current_preview.show_preview_message("Иконка недоступна")
        else:
            self.current_preview.set_preview_pixmap(current_pixmap)
        self.current_source_label.setText(
            f"Сейчас: {current_operblock_icon_source(icon_keys, fallback_file=fallback_file)}"
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
            CustomMessageBox.warning(self, "Иконки оперблока", "Выбранный файл не является изображением.")
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
            icon_keys: list[str] = []
            for key in descriptor.get("icon_keys") or [descriptor.get("icon_key")]:
                clean_key = str(key or "").strip()
                if clean_key and clean_key not in icon_keys:
                    icon_keys.append(clean_key)
            if not icon_keys:
                icon_keys = [str(descriptor.get("icon_key") or "").strip()]
            for offset, icon_key in enumerate(icon_keys):
                service.save_operblock_icon(
                    icon_key=icon_key,
                    category=str(descriptor.get("category") or "custom"),
                    target_key=str(descriptor.get("target_key") or descriptor.get("icon_key") or icon_key),
                    name=str(descriptor.get("name") or descriptor.get("icon_key") or "Иконка"),
                    default_file=str(descriptor.get("default_file") or ""),
                    image_path=self._pending_image_path,
                    sort_order=int(descriptor.get("sort_order") or 0) + offset,
                )
        except Exception as exc:
            CustomMessageBox.critical(self, "Ошибка", f"Не удалось сохранить иконку: {exc}")
            return
        invalidate_operblock_icon_cache()
        self._icon_records = self._load_icon_records()
        self._pending_image_path = ""
        self._refresh_detail()
        self._populate_changed_drug_list()
        self._apply_to_open_widgets()
        self.save_btn.setText("Сохранено")

    def _apply_to_open_widgets(self):
        app = QApplication.instance()
        if app is None:
            return
        for widget in app.allWidgets():
            method = getattr(widget, "apply_operblock_icon_settings", None)
            if callable(method):
                try:
                    method()
                except Exception:
                    pass
