from __future__ import annotations

from typing import Any

from PySide6.QtCore import QDate, Qt, QTime
from PySide6.QtWidgets import (
    QAbstractItemView,
    QCheckBox,
    QComboBox,
    QDateEdit,
    QDialog,
    QDoubleSpinBox,
    QFileDialog,
    QFormLayout,
    QFrame,
    QHeaderView,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QSpinBox,
    QTableWidget,
    QTableWidgetItem,
    QTimeEdit,
    QVBoxLayout,
    QWidget,
)

from rem_card.ui.shared.base_dialog import BaseStyledDialog
from rem_card.ui.shared.custom_message_box import CustomMessageBox
from rem_card.ui.shared.decor_overlay import DecorPreviewFrame
from rem_card.ui.shared.decor_settings import (
    DECOR_ZONES,
    DecorSettingsStorage,
    DecorSettingsValidationError,
    copy_decor_asset_to_assets_dir,
    default_decor_settings_payload,
    event_period_label,
    normalize_decor_event,
    normalize_decor_settings_payload,
    normalize_month_day,
)


class DecorSettingsDialog(BaseStyledDialog):
    def __init__(self, parent=None):
        super().__init__("Настройка декора", parent)
        self.storage = DecorSettingsStorage()
        self._loading = False
        self._current_index = -1
        self._events: list[dict[str, Any]] = []
        self.resize(1180, 760)
        self._setup_ui()
        self._load_settings()

    def _setup_ui(self):
        root = self.content_layout
        root.setSpacing(12)

        body = QHBoxLayout()
        body.setSpacing(14)
        root.addLayout(body, 1)

        left = QVBoxLayout()
        left.setSpacing(10)
        body.addLayout(left, 0)

        left_title = QLabel("События")
        left_title.setObjectName("DecorSectionTitle")
        left.addWidget(left_title)

        self.events_table = QTableWidget(0, 4)
        self.events_table.setObjectName("DecorEventsTable")
        self.events_table.setHorizontalHeaderLabels(["Название", "Период", "Зона", "Вкл"])
        self.events_table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.events_table.setSelectionMode(QAbstractItemView.SingleSelection)
        self.events_table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.events_table.verticalHeader().hide()
        self.events_table.horizontalHeader().setSectionResizeMode(0, QHeaderView.Stretch)
        self.events_table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeToContents)
        self.events_table.horizontalHeader().setSectionResizeMode(2, QHeaderView.ResizeToContents)
        self.events_table.horizontalHeader().setSectionResizeMode(3, QHeaderView.ResizeToContents)
        self.events_table.setMinimumWidth(430)
        self.events_table.itemSelectionChanged.connect(self._on_event_selection_changed)
        left.addWidget(self.events_table, 1)

        event_buttons = QHBoxLayout()
        self.btn_add_event = QPushButton("Добавить")
        self.btn_duplicate_event = QPushButton("Дублировать")
        self.btn_delete_event = QPushButton("Удалить")
        for button in (self.btn_add_event, self.btn_duplicate_event, self.btn_delete_event):
            button.setObjectName("DialogOkBtn")
            event_buttons.addWidget(button)
        left.addLayout(event_buttons)
        self.btn_add_event.clicked.connect(self._add_event)
        self.btn_duplicate_event.clicked.connect(self._duplicate_event)
        self.btn_delete_event.clicked.connect(self._delete_event)

        right = QVBoxLayout()
        right.setSpacing(10)
        body.addLayout(right, 1)

        form_frame = QFrame()
        form_frame.setObjectName("DecorFormFrame")
        form_layout = QVBoxLayout(form_frame)
        form_layout.setContentsMargins(14, 12, 14, 12)
        form_layout.setSpacing(10)
        right.addWidget(form_frame, 0)

        form_title = QLabel("Параметры события")
        form_title.setObjectName("DecorSectionTitle")
        form_layout.addWidget(form_title)

        form = QFormLayout()
        form.setLabelAlignment(Qt.AlignRight | Qt.AlignVCenter)
        form.setFormAlignment(Qt.AlignTop)
        form.setHorizontalSpacing(12)
        form.setVerticalSpacing(8)
        form_layout.addLayout(form)

        self.name_edit = QLineEdit()
        self.enabled_check = QCheckBox("Событие активно")
        self.zone_combo = QComboBox()
        for zone_key, label in DECOR_ZONES.items():
            self.zone_combo.addItem(label, zone_key)

        self.start_date = QDateEdit()
        self.start_date.setCalendarPopup(True)
        self.start_date.setDisplayFormat("dd.MM")
        self.start_time = QTimeEdit()
        self.start_time.setDisplayFormat("HH:mm")
        self.end_date = QDateEdit()
        self.end_date.setCalendarPopup(True)
        self.end_date.setDisplayFormat("dd.MM")
        self.end_time = QTimeEdit()
        self.end_time.setDisplayFormat("HH:mm")

        start_row = QHBoxLayout()
        start_row.addWidget(self.start_date)
        start_row.addWidget(self.start_time)
        start_widget = QWidget()
        start_widget.setLayout(start_row)
        end_row = QHBoxLayout()
        end_row.addWidget(self.end_date)
        end_row.addWidget(self.end_time)
        end_widget = QWidget()
        end_widget.setLayout(end_row)

        self.intensity_spin = QSpinBox()
        self.intensity_spin.setRange(0, 100)
        self.intensity_spin.setSuffix(" %")
        self.wind_spin = QSpinBox()
        self.wind_spin.setRange(0, 100)
        self.wind_spin.setSuffix(" %")
        self.drifts_check = QCheckBox("Насыпать по нижнему краю и поверхностям")
        self.max_drift_spin = QSpinBox()
        self.max_drift_spin.setRange(0, 120)
        self.max_drift_spin.setSuffix(" px")
        self.accumulation_spin = QSpinBox()
        self.accumulation_spin.setRange(0, 100)
        self.accumulation_spin.setSuffix(" %")
        self.surface_spin = QSpinBox()
        self.surface_spin.setRange(0, 100)
        self.surface_spin.setSuffix(" %")

        form.addRow("Название:", self.name_edit)
        form.addRow("", self.enabled_check)
        form.addRow("Где падать:", self.zone_combo)
        form.addRow("Начало:", start_widget)
        form.addRow("Окончание:", end_widget)
        form.addRow("Интенсивность:", self.intensity_spin)
        form.addRow("Ветер от курсора:", self.wind_spin)
        form.addRow("", self.drifts_check)
        form.addRow("Макс. сугробы:", self.max_drift_spin)
        form.addRow("Накопление:", self.accumulation_spin)
        form.addRow("На поверхностях:", self.surface_spin)

        particles_title = QLabel("Падающие изображения")
        particles_title.setObjectName("DecorSectionTitle")
        right.addWidget(particles_title)

        self.particles_table = QTableWidget(0, 3)
        self.particles_table.setObjectName("DecorParticlesTable")
        self.particles_table.setHorizontalHeaderLabels(["Файл", "Размер", "Вес"])
        self.particles_table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.particles_table.setSelectionMode(QAbstractItemView.SingleSelection)
        self.particles_table.verticalHeader().hide()
        self.particles_table.horizontalHeader().setSectionResizeMode(0, QHeaderView.Stretch)
        self.particles_table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeToContents)
        self.particles_table.horizontalHeader().setSectionResizeMode(2, QHeaderView.ResizeToContents)
        self.particles_table.setMinimumHeight(145)
        right.addWidget(self.particles_table)

        particle_buttons = QHBoxLayout()
        self.btn_add_particle = QPushButton("Загрузить картинки")
        self.btn_remove_particle = QPushButton("Удалить картинку")
        self.btn_preview = QPushButton("Обновить предпросмотр")
        for button in (self.btn_add_particle, self.btn_remove_particle, self.btn_preview):
            button.setObjectName("DialogOkBtn")
            particle_buttons.addWidget(button)
        particle_buttons.addStretch()
        right.addLayout(particle_buttons)
        self.btn_add_particle.clicked.connect(self._add_particles)
        self.btn_remove_particle.clicked.connect(self._remove_particle)
        self.btn_preview.clicked.connect(self._refresh_preview)

        preview_title = QLabel("Предпросмотр")
        preview_title.setObjectName("DecorSectionTitle")
        right.addWidget(preview_title)
        self.preview_frame = DecorPreviewFrame()
        right.addWidget(self.preview_frame, 1)

        footer = QHBoxLayout()
        footer.addStretch()
        self.btn_cancel = QPushButton("Закрыть")
        self.btn_save = QPushButton("Сохранить")
        self.btn_cancel.setObjectName("DialogOkBtn")
        self.btn_save.setObjectName("DialogOkBtn")
        self.btn_cancel.clicked.connect(self.reject)
        self.btn_save.clicked.connect(self._save)
        footer.addWidget(self.btn_cancel)
        footer.addWidget(self.btn_save)
        root.addLayout(footer)

        self._apply_style()

    def _apply_style(self):
        self.setStyleSheet(
            self.styleSheet()
            + """
            QLabel#DecorSectionTitle {
                color: #26394d;
                font-size: 14px;
                font-weight: 700;
            }
            QFrame#DecorFormFrame {
                background: #f6f9fc;
                border: 1px solid #d4dde6;
                border-radius: 8px;
            }
            QLineEdit, QComboBox, QDateEdit, QTimeEdit, QSpinBox, QDoubleSpinBox {
                background: #ffffff;
                border: 1px solid #c7d1da;
                border-radius: 6px;
                color: #26394d;
                padding: 6px 8px;
                min-height: 28px;
            }
            QLineEdit:focus, QComboBox:focus, QDateEdit:focus, QTimeEdit:focus,
            QSpinBox:focus, QDoubleSpinBox:focus {
                border-color: #7f9fbd;
            }
            QCheckBox {
                color: #26394d;
                spacing: 8px;
            }
            QTableWidget#DecorEventsTable, QTableWidget#DecorParticlesTable {
                background: #ffffff;
                color: #26394d;
                border: 1px solid #c7d1da;
                border-radius: 6px;
                gridline-color: #d7dfe7;
            }
            QTableWidget::item {
                padding: 6px;
            }
            QTableWidget::item:selected {
                background: #dceaf7;
                color: #1f2d3d;
            }
            QHeaderView::section {
                background: #eef3f7;
                color: #26394d;
                border: none;
                border-right: 1px solid #c7d1da;
                border-bottom: 1px solid #c7d1da;
                padding: 7px;
                font-weight: 700;
            }
            """
        )

    def _load_settings(self):
        self._loading = True
        try:
            payload = self.storage.load()
            self._events = list(payload.get("events") or [])
            if not self._events:
                self._events = list(default_decor_settings_payload()["events"])
            self._populate_events_table()
            if self._events:
                self.events_table.selectRow(0)
                self._current_index = 0
                self._load_event_to_form(self._events[0])
        finally:
            self._loading = False
        self._refresh_preview()

    def _populate_events_table(self):
        self.events_table.setRowCount(len(self._events))
        for row, event in enumerate(self._events):
            self._update_event_row(row, event)

    def _update_event_row(self, row: int, event: dict[str, Any]):
        normalized = normalize_decor_event(event, row + 1)
        values = [
            str(normalized.get("name") or ""),
            event_period_label(normalized),
            DECOR_ZONES.get(str(normalized.get("zone") or "all"), "Вся РЕМ карта и оперблок"),
            "Да" if normalized.get("enabled") else "Нет",
        ]
        for column, value in enumerate(values):
            item = QTableWidgetItem(value)
            item.setToolTip(value)
            self.events_table.setItem(row, column, item)
        self.events_table.setRowHeight(row, 44)

    def _on_event_selection_changed(self):
        if self._loading:
            return
        selected = self.events_table.selectedItems()
        if not selected:
            return
        row = selected[0].row()
        if row == self._current_index:
            return
        self._store_current_event()
        self._current_index = row
        self._load_event_to_form(self._events[row])
        self._refresh_preview()

    def _load_event_to_form(self, event: dict[str, Any]):
        self._loading = True
        try:
            event = normalize_decor_event(event)
            self.name_edit.setText(str(event.get("name") or ""))
            self.enabled_check.setChecked(bool(event.get("enabled")))
            zone = str(event.get("zone") or "all")
            zone_index = self.zone_combo.findData(zone)
            self.zone_combo.setCurrentIndex(max(0, zone_index))
            self._set_date_edit(self.start_date, str(event.get("start") or "12-31"))
            self.start_time.setTime(QTime.fromString(str(event.get("start_time") or "00:00"), "HH:mm"))
            self._set_date_edit(self.end_date, str(event.get("end") or "01-01"))
            self.end_time.setTime(QTime.fromString(str(event.get("end_time") or "23:59"), "HH:mm"))
            self.intensity_spin.setValue(int(event.get("intensity") or 0))
            self.wind_spin.setValue(int(event.get("wind_strength") or 0))
            snowdrifts = event.get("snowdrifts") or {}
            self.drifts_check.setChecked(bool(snowdrifts.get("enabled", True)))
            self.max_drift_spin.setValue(int(snowdrifts.get("max_height") or 0))
            self.accumulation_spin.setValue(int(snowdrifts.get("accumulation") or 0))
            self.surface_spin.setValue(int(snowdrifts.get("surface_intensity") or 0))
            self._populate_particles_table(list(event.get("particles") or []))
        finally:
            self._loading = False

    def _store_current_event(self):
        if self._current_index < 0 or self._current_index >= len(self._events):
            return
        self._events[self._current_index] = self._event_from_form(self._events[self._current_index])
        self._update_event_row(self._current_index, self._events[self._current_index])

    def _event_from_form(self, base_event: dict[str, Any] | None = None) -> dict[str, Any]:
        base = dict(base_event or {})
        base.update(
            {
                "name": self.name_edit.text().strip() or "Событие декора",
                "enabled": self.enabled_check.isChecked(),
                "zone": str(self.zone_combo.currentData() or "all"),
                "start": self._date_edit_value(self.start_date),
                "start_time": self.start_time.time().toString("HH:mm"),
                "end": self._date_edit_value(self.end_date),
                "end_time": self.end_time.time().toString("HH:mm"),
                "intensity": self.intensity_spin.value(),
                "wind_strength": self.wind_spin.value(),
                "snowdrifts": {
                    "enabled": self.drifts_check.isChecked(),
                    "max_height": self.max_drift_spin.value(),
                    "accumulation": self.accumulation_spin.value(),
                    "surface_intensity": self.surface_spin.value(),
                },
                "particles": self._particles_from_table(),
            }
        )
        return normalize_decor_event(base, self._current_index + 1 if self._current_index >= 0 else 1)

    def _populate_particles_table(self, particles: list[dict[str, Any]]):
        self.particles_table.setRowCount(0)
        for particle in particles:
            self._append_particle_row(particle)

    def _append_particle_row(self, particle: dict[str, Any]):
        row = self.particles_table.rowCount()
        self.particles_table.insertRow(row)
        file_name = str(particle.get("file") or "")
        file_item = QTableWidgetItem(file_name)
        file_item.setData(Qt.UserRole, file_name)
        file_item.setToolTip(file_name)
        self.particles_table.setItem(row, 0, file_item)

        size_spin = QSpinBox()
        size_spin.setRange(6, 160)
        size_spin.setValue(int(particle.get("size") or 24))
        size_spin.setSuffix(" px")
        self.particles_table.setCellWidget(row, 1, size_spin)

        weight_spin = QDoubleSpinBox()
        weight_spin.setRange(0.1, 5.0)
        weight_spin.setDecimals(2)
        weight_spin.setSingleStep(0.1)
        weight_spin.setValue(float(particle.get("weight") or 1.0))
        self.particles_table.setCellWidget(row, 2, weight_spin)
        self.particles_table.setRowHeight(row, 40)

    def _particles_from_table(self) -> list[dict[str, Any]]:
        particles: list[dict[str, Any]] = []
        for row in range(self.particles_table.rowCount()):
            file_item = self.particles_table.item(row, 0)
            file_name = str(file_item.data(Qt.UserRole) if file_item is not None else "").strip()
            if not file_name:
                continue
            size_widget = self.particles_table.cellWidget(row, 1)
            weight_widget = self.particles_table.cellWidget(row, 2)
            particles.append(
                {
                    "id": f"particle_{row + 1}",
                    "name": file_name,
                    "file": file_name,
                    "size": int(size_widget.value()) if isinstance(size_widget, QSpinBox) else 24,
                    "weight": float(weight_widget.value()) if isinstance(weight_widget, QDoubleSpinBox) else 1.0,
                }
            )
        return particles

    def _add_event(self):
        self._store_current_event()
        event = normalize_decor_event(
            {
                "id": f"decor_event_{len(self._events) + 1}",
                "name": "Новое событие",
                "enabled": True,
                "zone": "all",
                "start": "03-08",
                "start_time": "12:00",
                "end": "03-08",
                "end_time": "14:00",
                "intensity": 24,
                "wind_strength": 45,
                "snowdrifts": {"enabled": True, "max_height": 26, "accumulation": 20, "surface_intensity": 18},
                "particles": [],
            },
            len(self._events) + 1,
        )
        self._events.append(event)
        self._populate_events_table()
        self.events_table.selectRow(len(self._events) - 1)

    def _duplicate_event(self):
        self._store_current_event()
        if self._current_index < 0 or self._current_index >= len(self._events):
            return
        event = dict(self._events[self._current_index])
        event["id"] = f"{event.get('id') or 'decor_event'}_copy"
        event["name"] = f"{event.get('name') or 'Событие'} копия"
        self._events.append(normalize_decor_event(event, len(self._events) + 1))
        self._populate_events_table()
        self.events_table.selectRow(len(self._events) - 1)

    def _delete_event(self):
        if self._current_index < 0 or self._current_index >= len(self._events):
            return
        answer = CustomMessageBox.question(self, "Настройка декора", "Удалить выбранное событие декора?")
        if answer != CustomMessageBox.Yes:
            return
        del self._events[self._current_index]
        self._current_index = min(self._current_index, len(self._events) - 1)
        self._populate_events_table()
        if self._current_index >= 0:
            self.events_table.selectRow(self._current_index)
            self._load_event_to_form(self._events[self._current_index])
        else:
            self._load_event_to_form(normalize_decor_event({}))

    def _add_particles(self):
        dialog = QFileDialog(self, "Выберите декоративные изображения")
        dialog.setFileMode(QFileDialog.FileMode.ExistingFiles)
        dialog.setNameFilter("Изображения (*.png *.jpg *.jpeg *.bmp *.gif *.webp *.svg)")
        dialog.setOption(QFileDialog.Option.DontUseNativeDialog, True)
        try:
            dialog.setLabelText(QFileDialog.DialogLabel.Accept, "Загрузить")
        except Exception:
            pass
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        for source_path in dialog.selectedFiles():
            try:
                file_name = copy_decor_asset_to_assets_dir(source_path)
            except Exception as exc:
                CustomMessageBox.warning(self, "Загрузка декора", f"Не удалось загрузить картинку:\n{exc}")
                continue
            self._append_particle_row({"file": file_name, "size": 26, "weight": 1.0})
        self._refresh_preview()

    def _remove_particle(self):
        rows = sorted({item.row() for item in self.particles_table.selectedItems()}, reverse=True)
        for row in rows:
            self.particles_table.removeRow(row)
        self._refresh_preview()

    def _refresh_preview(self):
        if self._loading:
            return
        event = self._event_from_form(self._events[self._current_index] if 0 <= self._current_index < len(self._events) else {})
        self.preview_frame.set_event(event)

    def _save(self):
        self._store_current_event()
        payload = normalize_decor_settings_payload({"events": self._events})
        for event in payload.get("events") or []:
            if event.get("enabled") and not event.get("particles"):
                CustomMessageBox.warning(
                    self,
                    "Настройка декора",
                    f"В событии «{event.get('name')}» нет ни одной картинки для падения.",
                )
                return
        try:
            self.storage.save(payload)
        except DecorSettingsValidationError as exc:
            CustomMessageBox.warning(self, "Пересечение событий", exc.conflict.message())
            return
        except Exception as exc:
            CustomMessageBox.warning(self, "Настройка декора", f"Не удалось сохранить настройки:\n{exc}")
            return
        CustomMessageBox.information(self, "Настройка декора", "Настройки декора сохранены.")
        self.accept()

    @staticmethod
    def _set_date_edit(widget: QDateEdit, month_day: str):
        normalized = normalize_month_day(month_day, "01-01")
        month, day = (int(part) for part in normalized.split("-", 1))
        widget.setDate(QDate(2000, month, day))

    @staticmethod
    def _date_edit_value(widget: QDateEdit) -> str:
        date = widget.date()
        return f"{date.month():02d}-{date.day():02d}"
