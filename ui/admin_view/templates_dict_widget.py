from copy import deepcopy
import re
from rem_card.ui.shared.custom_message_box import CustomMessageBox
from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QPushButton, QLabel, 
    QListWidget, QListWidgetItem, QLineEdit, QSplitter,
    QTableWidget, QTableWidgetItem, QHeaderView, QComboBox,
    QDoubleSpinBox, QSpinBox, QFormLayout, QGroupBox, QDialog, QCheckBox, QFrame,
    QApplication, QAbstractItemView
)
from PySide6.QtCore import Qt, Signal, QEvent, QPoint, QTimer
from rem_card.services.prescription_engine import engine
from rem_card.ui.admin_view.drugs_dict_widget import _admin_drugs_table_style
from rem_card.ui.shared.base_dialog import BaseStyledDialog
from rem_card.ui.shared.duration_combo import configure_duration_combo, set_end_of_day_duration_text


def _template_volume_value(value):
    try:
        number = float(str(value).replace(",", "."))
    except (TypeError, ValueError):
        return None
    if number.is_integer():
        return int(number)
    return number


def _template_same_dilution(left, right):
    if not isinstance(left, dict) or not isinstance(right, dict):
        return False
    return (
        left.get("base") == right.get("base")
        and _template_volume_value(left.get("volume")) == _template_volume_value(right.get("volume"))
    )


def _template_format_dilution(dilution):
    if not isinstance(dilution, dict):
        return "Без растворителя"
    base = dilution.get("base")
    volume = _template_volume_value(dilution.get("volume"))
    diluent_info = engine.dilutions.get(base, {})
    display = diluent_info.get("display", base or "")
    if volume is None:
        return str(display or "Без растворителя")
    return f"{display} {volume:g} мл"


def _template_format_raw_dilution(dilution):
    if not isinstance(dilution, dict):
        return ""
    base = dilution.get("base")
    volume = _template_volume_value(dilution.get("volume"))
    diluent_info = engine.dilutions.get(base, {})
    display = diluent_info.get("display", base or "")
    short = str(diluent_info.get("short") or "S.").strip() or "S."
    if volume is None:
        return f"{short} {display}".strip()
    return f"{short} {display} - {volume:g} мл".strip()


def _add_template_dilution_items(combo):
    for d_key, d_info in sorted(engine.dilutions.items(), key=lambda x: x[1].get("display", x[0])):
        if d_key == "none" or d_info.get("display") == "Без растворителя":
            continue

        display_base = d_info.get("display", d_key)
        for vol in d_info.get("default_volumes", [100]):
            volume = _template_volume_value(vol)
            if volume is None:
                continue
            combo.addItem(f"{display_base} {volume:g} мл", {"base": d_key, "volume": volume})


def _raw_text_dilution(raw_text):
    match = re.search(r"\[DIL:(.*?)\]", str(raw_text or ""))
    if not match:
        return None

    text = match.group(1).strip()
    volume_match = re.search(r"(\d+(?:[\.,]\d+)?)\s*(?:мл|ml)\b", text, flags=re.IGNORECASE)
    volume = _template_volume_value(volume_match.group(1)) if volume_match else None
    text_lower = text.lower()

    for d_key, d_info in engine.dilutions.items():
        variants = [
            str(d_info.get("display") or ""),
            str(d_info.get("latin") or ""),
            str(d_key or ""),
        ]
        if any(variant and variant.lower() in text_lower for variant in variants):
            return {"base": d_key, "volume": volume}

    return None


def _replace_raw_text_dilution(raw_text, target_dilution):
    replacement = f"[DIL:{_template_format_raw_dilution(target_dilution)}]"
    return re.sub(r"\[DIL:.*?\]", replacement, str(raw_text or ""), count=1)


def _template_drug_display(item):
    if item.get("is_multicomp"):
        return re.sub(r"\[.*?\]", "", str(item.get("raw_text") or "")).strip() or "Многокомпонентный препарат"
    drug_key = item.get("drug", "")
    return engine.drugs.get(drug_key, {}).get("latin", drug_key) or drug_key


class TemplateDilutionReplaceDialog(BaseStyledDialog):
    def __init__(self, parent=None):
        super().__init__("Изменение объемов растворов в шаблонах", parent)
        self.setMinimumSize(860, 620)
        self.matches = []
        self._pending_changes = {}

        self.setup_ui()
        self._update_buttons()

    def setup_ui(self):
        form_layout = QFormLayout()

        self.source_combo = QComboBox()
        _add_template_dilution_items(self.source_combo)
        form_layout.addRow("Найти растворитель:", self.source_combo)

        search_layout = QHBoxLayout()
        self.btn_find = QPushButton("Найти")
        self.btn_find.setObjectName("DialogOkBtn")
        self.btn_find.setFixedHeight(32)
        search_layout.addStretch()
        search_layout.addWidget(self.btn_find)
        form_layout.addRow("", search_layout)

        self.target_combo = QComboBox()
        _add_template_dilution_items(self.target_combo)
        form_layout.addRow("Заменить на:", self.target_combo)

        self.content_layout.addLayout(form_layout)

        self.table = QTableWidget()
        self.table.setColumnCount(6)
        self.table.setHorizontalHeaderLabels(["Шаблон", "Препараты", "Совп.", "Было", "Станет", "Статус"])
        self.table.horizontalHeader().setSectionResizeMode(0, QHeaderView.Stretch)
        self.table.horizontalHeader().setSectionResizeMode(1, QHeaderView.Stretch)
        self.table.setSelectionBehavior(QTableWidget.SelectRows)
        self.table.setSelectionMode(QTableWidget.SingleSelection)
        self.table.setEditTriggers(QTableWidget.NoEditTriggers)
        self.table.setStyleSheet(_admin_drugs_table_style())
        self.table.itemSelectionChanged.connect(self._update_buttons)
        self.content_layout.addWidget(self.table, 1)

        tools_layout = QHBoxLayout()
        self.status_label = QLabel("Выберите растворитель и нажмите «Найти».")
        self.status_label.setStyleSheet("border: none; background: transparent;")
        tools_layout.addWidget(self.status_label, 1)

        self.btn_remove = QPushButton("Убрать из замены")
        self.btn_remove.setObjectName("DialogOkBtn")
        self.btn_remove.setFixedHeight(32)
        tools_layout.addWidget(self.btn_remove)
        self.content_layout.addLayout(tools_layout)

        buttons_layout = QHBoxLayout()
        buttons_layout.addStretch()

        self.btn_replace = QPushButton("Заменить")
        self.btn_apply = QPushButton("Применить")
        self.btn_save = QPushButton("Сохранить")
        self.btn_cancel = QPushButton("Отмена")

        for btn in [self.btn_replace, self.btn_apply, self.btn_save, self.btn_cancel]:
            btn.setObjectName("DialogOkBtn")
            btn.setFixedHeight(35)
            buttons_layout.addWidget(btn)

        self.content_layout.addLayout(buttons_layout)

        self.btn_find.clicked.connect(self.find_matches)
        self.btn_remove.clicked.connect(self.remove_selected_match)
        self.btn_replace.clicked.connect(self.stage_replacement)
        self.btn_apply.clicked.connect(self.apply_changes)
        self.btn_save.clicked.connect(self.save_and_close)
        self.btn_cancel.clicked.connect(self.reject)
        self.source_combo.currentIndexChanged.connect(self._clear_results)
        self.target_combo.currentIndexChanged.connect(self._on_target_changed)

    def _selected_source(self):
        return self.source_combo.currentData()

    def _selected_target(self):
        return self.target_combo.currentData()

    def _has_pending_changes(self):
        return bool(self._pending_changes)

    def _replacement_is_valid(self):
        source = self._selected_source()
        target = self._selected_target()
        return bool(self.matches) and bool(source) and bool(target) and not _template_same_dilution(source, target)

    def _set_controls_locked(self, locked):
        self.source_combo.setEnabled(not locked)
        self.target_combo.setEnabled(not locked)
        self.btn_find.setEnabled(not locked)
        self.btn_remove.setEnabled(not locked and self.table.currentRow() >= 0)

    def _update_buttons(self):
        pending = self._has_pending_changes()
        self._set_controls_locked(pending)
        self.btn_remove.setEnabled(not pending and self.table.currentRow() >= 0)
        self.btn_replace.setEnabled(not pending and self._replacement_is_valid())
        self.btn_apply.setEnabled(pending)
        self.btn_save.setEnabled(True)

    def _clear_results(self):
        if self._has_pending_changes():
            return
        self.matches = []
        self.table.setRowCount(0)
        self.status_label.setText("Выберите растворитель и нажмите «Найти».")
        self._update_buttons()

    def _on_target_changed(self):
        if self._has_pending_changes():
            self._update_buttons()
            return
        if self.matches:
            self._refresh_table()
            return
        self._update_buttons()

    def _matching_drug_indexes(self, template_data, source):
        indexes = []
        for index, item in enumerate(template_data.get("drugs", []) or []):
            if not isinstance(item, dict):
                continue
            if _template_same_dilution(item.get("diluent"), source):
                indexes.append(index)
                continue
            if item.get("is_multicomp") and _template_same_dilution(_raw_text_dilution(item.get("raw_text")), source):
                indexes.append(index)
        return indexes

    def _refresh_table(self):
        self.table.setRowCount(0)
        target = self._selected_target()

        for row, item in enumerate(self.matches):
            self.table.insertRow(row)

            key = item.get("key")
            data = item.get("data", {})
            name_item = QTableWidgetItem(data.get("name", key))
            name_item.setData(Qt.UserRole, key)
            self.table.setItem(row, 0, name_item)

            drug_names = item.get("drug_names") or []
            self.table.setItem(row, 1, QTableWidgetItem(", ".join(drug_names[:3]) + ("..." if len(drug_names) > 3 else "")))
            self.table.setItem(row, 2, QTableWidgetItem(str(len(item.get("indexes") or []))))
            self.table.setItem(row, 3, QTableWidgetItem(_template_format_dilution(item.get("old_dilution"))))

            new_dilution = item.get("new_dilution")
            self.table.setItem(row, 4, QTableWidgetItem(_template_format_dilution(new_dilution or target)))
            status_text = "Будет заменено" if new_dilution else "Найдено"
            self.table.setItem(row, 5, QTableWidgetItem(status_text))

        self._update_buttons()

    def find_matches(self):
        if self._has_pending_changes():
            return

        source = self._selected_source()
        if not source:
            CustomMessageBox.warning(self, "Ошибка", "Выберите растворитель для поиска.")
            return

        engine.reload_if_changed(force_check=True)
        self.matches = []
        for key, data in engine.templates.items():
            indexes = self._matching_drug_indexes(data, source)
            if not indexes:
                continue
            drugs = data.get("drugs", []) or []
            self.matches.append(
                {
                    "key": key,
                    "data": deepcopy(data),
                    "indexes": indexes,
                    "drug_names": [_template_drug_display(drugs[index]) for index in indexes if index < len(drugs)],
                    "old_dilution": deepcopy(source),
                    "new_dilution": None,
                }
            )

        self._refresh_table()
        count = len(self.matches)
        if count:
            total = sum(len(item.get("indexes") or []) for item in self.matches)
            self.status_label.setText(f"Найдено шаблонов: {count}, назначений: {total}. Лишние строки можно убрать из замены.")
        else:
            self.status_label.setText("Шаблоны с таким растворителем не найдены.")

    def remove_selected_match(self):
        if self._has_pending_changes():
            return

        row = self.table.currentRow()
        if row < 0 or row >= len(self.matches):
            return

        del self.matches[row]
        self._refresh_table()
        if self.matches:
            self.table.selectRow(min(row, len(self.matches) - 1))
        self.status_label.setText(f"Осталось к замене: {len(self.matches)} шабл.")

    def stage_replacement(self):
        if not self._replacement_is_valid():
            if self.matches and _template_same_dilution(self._selected_source(), self._selected_target()):
                CustomMessageBox.warning(self, "Ошибка", "Растворитель для замены совпадает с исходным.")
            return

        source = self._selected_source()
        target = self._selected_target()
        template_count = len(self.matches)
        drug_count = sum(len(item.get("indexes") or []) for item in self.matches)
        reply = CustomMessageBox.question(
            self,
            "Подтверждение",
            (
                f"Заменить {_template_format_dilution(source)} на {_template_format_dilution(target)} "
                f"в шаблонах: {template_count}, назначениях: {drug_count}?\n\n"
                "Запись в справочник произойдет после «Применить» или «Сохранить»."
            ),
            CustomMessageBox.Yes | CustomMessageBox.No,
            CustomMessageBox.No,
        )
        if reply != CustomMessageBox.Yes:
            return

        self._pending_changes = {}
        target_copy = deepcopy(target)
        for match in self.matches:
            key = match.get("key")
            data = deepcopy(match.get("data", {}))
            drugs = data.get("drugs", []) or []
            for index in match.get("indexes", []):
                if index < 0 or index >= len(drugs) or not isinstance(drugs[index], dict):
                    continue
                if _template_same_dilution(drugs[index].get("diluent"), source):
                    drugs[index]["diluent"] = deepcopy(target_copy)
                elif drugs[index].get("is_multicomp") and _template_same_dilution(
                    _raw_text_dilution(drugs[index].get("raw_text")),
                    source,
                ):
                    drugs[index]["raw_text"] = _replace_raw_text_dilution(drugs[index].get("raw_text"), target_copy)
            data["drugs"] = drugs
            match["new_dilution"] = deepcopy(target_copy)
            self._pending_changes[key] = data

        self._refresh_table()
        self.status_label.setText("Замена подготовлена. Нажмите «Применить» или «Сохранить».")

    def _save_pending_changes(self):
        if not self._pending_changes:
            return True

        try:
            engine.save_custom_templates(list(self._pending_changes.items()))
        except Exception as exc:
            CustomMessageBox.critical(self, "Ошибка", f"Не удалось сохранить изменения: {exc}")
            return False

        saved_count = len(self._pending_changes)
        self._pending_changes = {}
        self.find_matches()
        self.status_label.setText(f"Сохранено шаблонов: {saved_count}.")
        return True

    def apply_changes(self):
        self._save_pending_changes()

    def save_and_close(self):
        if self._save_pending_changes():
            self.accept()


class TemplateDrugCharacteristicsDialog(BaseStyledDialog):
    """Диалог для выбора характеристик препарата для шаблона."""
    def __init__(self, drug_key, initial_data=None, parent=None):
        super().__init__("Характеристики препарата (Шаблон)", parent)
        self.setFixedSize(500, 550)
        
        self.drug_key = drug_key
        self.drug_data = engine.drugs.get(drug_key, {})
        self.result_dict = None
        
        self.setup_ui()
        self.fill_from_drug_data(initial_data)

    def setup_ui(self):
        form = QFormLayout()

        # 1. Название
        self.name_input = QLineEdit()
        self.name_input.setReadOnly(True)
        form.addRow("Препарат:", self.name_input)

        # 2. Форма препарата
        self.form_combo = QComboBox()
        self.form_combo.addItem("— Не выбрана —", "")
        for f_key, f_info in sorted(engine.forms.items(), key=lambda x: x[1].get('name_ru', x[0])):
            self.form_combo.addItem(f_info.get("name_ru", f_key), f_key)
        self.form_combo.currentIndexChanged.connect(self.on_form_changed)
        form.addRow("Форма препарата:", self.form_combo)

        # 3. Путь введения
        self.route_combo = QComboBox()
        self.route_combo.addItem("— Не выбран —", "")
        for r_key, r_info in sorted(engine.admin_types.items(), key=lambda x: x[1].get('name_ru', x[0])):
            self.route_combo.addItem(r_info.get("name_ru", r_key), r_key)
        form.addRow("Путь введения:", self.route_combo)

        # 4. Доза
        self.dose_spin = QDoubleSpinBox()
        self.dose_spin.setRange(0, 10000)
        self.dose_spin.setDecimals(2)
        
        self.unit_combo = QComboBox()
        self.unit_combo.addItems(["mg", "mcg", "g", "ml", "IU", "шт"])
        
        dose_layout = QHBoxLayout()
        dose_layout.addWidget(self.dose_spin)
        dose_layout.addWidget(self.unit_combo)
        form.addRow("Дозировка:", dose_layout)
        
        # 5. Длительность введения
        duration_layout = QHBoxLayout()
        self.duration_combo = QComboBox()
        durations = [0, 5, 10, 15, 20, 30, 45, 60, 90, 120, 180, 240]
        for d in durations:
            self.duration_combo.addItem(f"{d} мин" if d > 0 else "Болюс", d)
        self.duration_combo.setEditable(True)
        configure_duration_combo(self.duration_combo)
        self.duration_combo.setCurrentIndex(0)
            
        duration_layout.addWidget(self.duration_combo, 1)
        
        self.end_of_day_cb = QCheckBox("до конца суток")
        self.end_of_day_cb.toggled.connect(self.on_end_of_day_toggled)
        duration_layout.addWidget(self.end_of_day_cb)
        
        self.form_layout_ref = form
        self.duration_row_index = form.rowCount()
        form.addRow("Длительность введения:", duration_layout)

        # 6. Растворитель
        self.diluent_combo = QComboBox()
        self.diluent_combo.addItem("Без растворителя", None)
        
        for d_key, d_info in sorted(engine.dilutions.items(), key=lambda x: x[1].get('display', x[0])):
            if d_key == "none": continue
            display_base = d_info.get("display", d_key)
            for vol in d_info.get("default_volumes", [100]):
                self.diluent_combo.addItem(f"{display_base} {vol} мл", {"base": d_key, "volume": vol})
        
        self.diluent_row_index = form.rowCount()
        form.addRow("Растворитель:", self.diluent_combo)

        # 7. Кратность (закомментировано для простых шаблонов)
        # self.freq_spin = QSpinBox()
        # self.freq_spin.setRange(1, 24)
        # self.freq_spin.setSuffix(" р/д")
        # form.addRow("Кратность:", self.freq_spin)

        self.content_layout.addLayout(form)
        self.on_form_changed()

        # Кнопки
        btns = QHBoxLayout()
        btn_add = QPushButton("ОК")
        btn_add.setObjectName("DialogOkBtn")
        btn_add.clicked.connect(self.on_add)
        
        btn_cancel = QPushButton("Отмена")
        btn_cancel.setObjectName("DialogOkBtn")
        btn_cancel.clicked.connect(self.reject)
        
        btns.addStretch()
        btns.addWidget(btn_add)
        btns.addWidget(btn_cancel)
        self.content_layout.addLayout(btns)

    def fill_from_drug_data(self, initial_data):
        name = self.drug_data.get("latin", self.drug_key)
        self.name_input.setText(name)

        if initial_data:
            # Загружаем из сохраненных данных (редактирование)
            form_key = initial_data.get("form_key", self.drug_data.get("form_key"))
            idx = self.form_combo.findData(form_key)
            if idx >= 0: self.form_combo.setCurrentIndex(idx)

            admin_type = initial_data.get("admin_type", self.drug_data.get("admin_type", "bolus"))
            idx = self.route_combo.findData(admin_type)
            if idx >= 0: self.route_combo.setCurrentIndex(idx)

            self.dose_spin.setValue(float(initial_data.get("dose", 0)))
            unit = initial_data.get("unit", "mg")
            idx = self.unit_combo.findText(unit)
            if idx >= 0: self.unit_combo.setCurrentIndex(idx)

            duration = initial_data.get("duration_min", 0)
            if duration == -1:
                self.end_of_day_cb.setChecked(True)
            else:
                idx = self.duration_combo.findData(duration)
                if idx >= 0: self.duration_combo.setCurrentIndex(idx)
                else: self.duration_combo.setEditText(f"{duration} мин")

            diluent = initial_data.get("diluent")
            if diluent:
                for i in range(self.diluent_combo.count()):
                    d_data = self.diluent_combo.itemData(i)
                    if d_data and d_data.get("base") == diluent.get("base") and d_data.get("volume") == diluent.get("volume"):
                        self.diluent_combo.setCurrentIndex(i)
                        break

            # self.freq_spin.setValue(initial_data.get("freq", 1))
            pass
        else:
            # Загружаем дефолты из справочника
            form_key = self.drug_data.get("form_key")
            idx = self.form_combo.findData(form_key)
            if idx >= 0: self.form_combo.setCurrentIndex(idx)

            admin_type = self.drug_data.get("admin_type", "bolus")
            idx = self.route_combo.findData(admin_type)
            if idx >= 0: self.route_combo.setCurrentIndex(idx)

            self.dose_spin.setValue(float(self.drug_data.get("default_dose", 0)))
            unit = self.drug_data.get("unit", "mg")
            idx = self.unit_combo.findText(unit)
            if idx >= 0: self.unit_combo.setCurrentIndex(idx)

            duration = self.drug_data.get("duration_min", 0)
            idx = self.duration_combo.findData(duration)
            if idx >= 0: self.duration_combo.setCurrentIndex(idx)
            else: self.duration_combo.setEditText(f"{duration} мин")

            diluent = self.drug_data.get("default_dilution")
            if diluent:
                for i in range(self.diluent_combo.count()):
                    d_data = self.diluent_combo.itemData(i)
                    if d_data and d_data.get("base") == diluent.get("base") and d_data.get("volume") == diluent.get("volume"):
                        self.diluent_combo.setCurrentIndex(i)
                        break

        self.on_form_changed()

    def on_form_changed(self):
        form_key = self.form_combo.currentData()
        can_dilute = True
        if form_key:
            form_info = engine.forms.get(form_key, {})
            can_dilute = form_info.get("can_dilute", True)
        
        if hasattr(self, 'form_layout_ref'):
            if can_dilute:
                self.form_layout_ref.setRowVisible(self.duration_row_index, True)
                self.form_layout_ref.setRowVisible(self.diluent_row_index, True)
                self.duration_combo.setEnabled(not self.end_of_day_cb.isChecked())
                self.end_of_day_cb.setEnabled(True)
                self.diluent_combo.setEnabled(True)
            else:
                self.form_layout_ref.setRowVisible(self.duration_row_index, False)
                self.form_layout_ref.setRowVisible(self.diluent_row_index, False)
                self.duration_combo.setEnabled(False)
                self.end_of_day_cb.setEnabled(False)
                self.diluent_combo.setEnabled(False)
                self.duration_combo.setCurrentIndex(0)
                self.end_of_day_cb.setChecked(False)
                self.diluent_combo.setCurrentIndex(0)

    def on_end_of_day_toggled(self, checked):
        form_key = self.form_combo.currentData()
        can_dilute = True
        if form_key:
            form_info = engine.forms.get(form_key, {})
            can_dilute = form_info.get("can_dilute", True)
            
        if can_dilute:
            self.duration_combo.setEnabled(not checked)
            
        if checked:
            set_end_of_day_duration_text(self.duration_combo)

    def on_add(self):
        form_key = self.form_combo.currentData()
        admin_type = self.route_combo.currentData()
        
        if not admin_type:
            admin_type = "bolus" # фолбэк

        duration_val = 0
        form_info = engine.forms.get(form_key, {}) if form_key else {}
        can_dilute = form_info.get("can_dilute", True)
        
        if can_dilute:
            if self.end_of_day_cb.isChecked():
                duration_val = -1
            else:
                duration_text = self.duration_combo.currentText()
                try:
                    nums = re.findall(r"\d+", duration_text)
                    if nums:
                        duration_val = int(nums[0])
                    else:
                        d_val = self.duration_combo.currentData()
                        if d_val is not None:
                            duration_val = d_val
                except: pass

        self.result_dict = {
            "drug": self.drug_key,
            "form_key": form_key,
            "dose": self.dose_spin.value(),
            "unit": self.unit_combo.currentText(),
            "admin_type": admin_type,
            "duration_min": duration_val,
            "freq": 1 #self.freq_spin.value()
        }

        if can_dilute:
            dil = self.diluent_combo.currentData()
            if dil:
                self.result_dict["diluent"] = dil

        self.accept()


class TemplateDrugSearchWidget(QWidget):
    """Поисковая строка для добавления препарата в шаблон."""
    drug_selected = Signal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.matches = []
        self.setup_ui()
        
    def focusOutEvent(self, event):
        self.list_widget.hide()
        super().focusOutEvent(event)
        
    def hide_list(self):
        if hasattr(self, "list_widget") and not self.list_widget.isHidden():
            self.list_widget.hide()
        
    def setup_ui(self):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        
        self.input_field = QLineEdit()
        self.input_field.setPlaceholderText("Поиск препарата для добавления...")
        self.input_field.setStyleSheet("""
            QLineEdit {
                font-size: 14px; 
                padding: 6px;
                border: 1px solid #ced4da;
                border-radius: 4px;
            }
            QLineEdit:focus { border: 2px solid #bdc4c8; }
        """)
        layout.addWidget(self.input_field)
        
        self.list_widget = QListWidget(self)
        self.list_widget.setWindowFlags(Qt.ToolTip | Qt.FramelessWindowHint)
        self.list_widget.setFocusPolicy(Qt.NoFocus)
        self.list_widget.setStyleSheet("border: 1px solid gray; background-color: white; font-size: 14px;")
        self.list_widget.hide()
        
        self.input_field.textChanged.connect(self.on_text_changed)
        self.input_field.returnPressed.connect(self.on_enter)
        self.list_widget.itemClicked.connect(self.on_item_clicked)
        self.input_field.installEventFilter(self)

    def eventFilter(self, obj, event):
        if obj is self.input_field and event.type() == QEvent.FocusOut:
            QTimer.singleShot(100, self.hide_list)
        return super().eventFilter(obj, event)

    def on_text_changed(self, text):
        if not text:
            self.list_widget.hide()
            return
            
        self.list_widget.clear()
        self.matches = []
        text_lower = text.lower().strip()
        
        for key, data in engine.drugs.items():
            latin = data.get("latin", "").lower()
            aliases = [a.lower() for a in data.get("aliases", [])]
            if text_lower in latin or any(text_lower in a for a in aliases):
                self.matches.append((key, data))
                display_name = data.get("latin", key)
                self.list_widget.addItem(display_name)
                
        if self.matches:
            pos = self.input_field.mapToGlobal(self.input_field.rect().bottomLeft())
            self.list_widget.setGeometry(pos.x(), pos.y(), self.input_field.width(), 150)
            self.list_widget.show()
        else:
            self.list_widget.hide()

    def on_item_clicked(self, *_args):
        idx = self.list_widget.currentRow()
        if 0 <= idx < len(self.matches):
            key, _ = self.matches[idx]
            self.finish_selection(key)
            
    def on_enter(self):
        if not self.list_widget.isHidden() and self.list_widget.currentRow() >= 0:
            self.on_item_clicked()
            return
        if self.matches:
            key, _ = self.matches[0]
            self.finish_selection(key)

    def finish_selection(self, key):
        self.input_field.blockSignals(True)
        try:
            self.input_field.clear()
        finally:
            self.input_field.blockSignals(False)
        self.list_widget.hide()
        self.matches = []
        QTimer.singleShot(0, lambda selected_key=key: self._emit_drug_selected(selected_key))

    def _emit_drug_selected(self, key):
        if hasattr(self, "list_widget"):
            self.list_widget.clear()
        self.drug_selected.emit(key)


class TemplatesDictWidget(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.current_drugs = []
        self._loading_templates = False
        self._row_drag_state = None
        self._row_drag_ghost = None
        self._row_drag_indicator = None
        self.setup_ui()
        self.load_data()

    def setup_ui(self):
        main_layout = QVBoxLayout(self)
        main_layout.setContentsMargins(3, 3, 3, 3)

        self.frame = QFrame()
        self.frame.setObjectName("adminDictFrame")
        self.frame.setStyleSheet("""
            QFrame#adminDictFrame {
                border: 1.5px solid #bdc3c7;
                border-radius: 5px;
                background-color: transparent;
            }
        """)
        layout = QVBoxLayout(self.frame)
        layout.setContentsMargins(5, 5, 5, 5)
        layout.setSpacing(5)
        self.setStyleSheet("QLabel { background: transparent; } QGroupBox { background: transparent; }")
        
        header_layout = QHBoxLayout()
        header = QLabel("Справочник шаблонов (Клин. протоколов)")
        header.setProperty("heading", "true")
        header.setStyleSheet("border: none; background: transparent;")
        header_layout.addWidget(header)
        header_layout.addStretch()
        
        self.btn_back = QPushButton("← Назад")
        self.btn_back.setObjectName("DialogOkBtn")
        self.btn_back.setFixedSize(100, 35)
        header_layout.addWidget(self.btn_back)
        layout.addLayout(header_layout)
        
        splitter = QSplitter(Qt.Horizontal)
        
        # --- ЛЕВАЯ ПАНЕЛЬ: Список ---
        left_widget = QWidget()
        left_layout = QVBoxLayout(left_widget)
        left_layout.setContentsMargins(0, 0, 5, 0)
        left_layout.setSpacing(5)
        
        self.list_widget = QListWidget()
        self.list_widget.setSelectionMode(QAbstractItemView.SingleSelection)
        self.list_widget.setDragDropMode(QAbstractItemView.InternalMove)
        self.list_widget.setDefaultDropAction(Qt.MoveAction)
        self.list_widget.setDropIndicatorShown(True)
        self.list_widget.itemSelectionChanged.connect(self.on_template_selected)
        self.list_widget.itemSelectionChanged.connect(self._update_template_order_buttons)
        self.list_widget.model().rowsMoved.connect(self._on_template_rows_moved)
        left_layout.addWidget(self.list_widget)
        
        btn_list_layout = QHBoxLayout()
        self.btn_new_tpl = QPushButton("Новый")
        self.btn_del_tpl = QPushButton("Удалить")
        self.btn_tpl_up = QPushButton("↑")
        self.btn_tpl_down = QPushButton("↓")
        for b in [self.btn_new_tpl, self.btn_del_tpl, self.btn_tpl_up, self.btn_tpl_down]:
            b.setObjectName("DialogOkBtn")
            b.setFixedHeight(35)
            btn_list_layout.addWidget(b)
        self.btn_tpl_up.setFixedWidth(45)
        self.btn_tpl_down.setFixedWidth(45)
        self.btn_new_tpl.clicked.connect(self.create_new)
        self.btn_del_tpl.clicked.connect(self.delete_selected)
        self.btn_tpl_up.clicked.connect(self.move_selected_template_up)
        self.btn_tpl_down.clicked.connect(self.move_selected_template_down)
        left_layout.addLayout(btn_list_layout)

        self.btn_replace_template_dilutions = QPushButton("Изменить обьемы шабл. растворов")
        self.btn_replace_template_dilutions.setObjectName("DialogOkBtn")
        self.btn_replace_template_dilutions.setFixedHeight(35)
        self.btn_replace_template_dilutions.clicked.connect(self.replace_template_dilutions)
        left_layout.addWidget(self.btn_replace_template_dilutions)
        
        # --- ПРАВАЯ ПАНЕЛЬ: Редактор ---
        right_widget = QWidget()
        right_layout = QVBoxLayout(right_widget)
        right_layout.setContentsMargins(5, 0, 0, 0)
        right_layout.setSpacing(5)
        
        # 1. Инфо
        tpl_info_layout = QHBoxLayout()
        self.key_input = QLineEdit()
        self.key_input.setPlaceholderText("Ключ (англ)...")
        self.name_input = QLineEdit()
        self.name_input.setPlaceholderText("Название протокола (рус)...")
        self.type_combo = QComboBox()
        self.type_combo.addItem("Простой", "simple")
        # self.type_combo.addItem("Сложный", "complex")
        
        tpl_info_layout.addWidget(QLabel("ID:"))
        tpl_info_layout.addWidget(self.key_input, 1)
        tpl_info_layout.addWidget(QLabel("Тип:"))
        tpl_info_layout.addWidget(self.type_combo, 1)
        tpl_info_layout.addWidget(QLabel("Название:"))
        tpl_info_layout.addWidget(self.name_input, 3)
        right_layout.addLayout(tpl_info_layout)
        
        # 2. Таблица
        self.table = QTableWidget()
        self.table.setStyleSheet(_admin_drugs_table_style())
        self.table.setColumnCount(5) # Было 6, убрали Кратность
        self.table.setHorizontalHeaderLabels(["Препарат", "Доза", "Путь", "Длит.", "Растворитель"])
        self.table.horizontalHeader().setSectionResizeMode(0, QHeaderView.Stretch)
        self.table.setSelectionBehavior(QTableWidget.SelectRows)
        self.table.setSelectionMode(QTableWidget.SingleSelection)
        self.table.setEditTriggers(QTableWidget.NoEditTriggers)
        self.table.itemDoubleClicked.connect(self.edit_drug_from_table)
        self.table.itemSelectionChanged.connect(self._update_reorder_buttons)
        self.table.verticalHeader().setDefaultSectionSize(40)
        self.table.viewport().installEventFilter(self)
        right_layout.addWidget(self.table)
        
        # 3. Кнопки таблицы и поиск
        tbl_actions = QHBoxLayout()
        
        self.search_widget = TemplateDrugSearchWidget()
        self.search_widget.drug_selected.connect(self.on_drug_selected)
        tbl_actions.addWidget(self.search_widget, 2)
        
        self.btn_move_up = QPushButton("↑")
        self.btn_move_down = QPushButton("↓")
        self.btn_edit_drug = QPushButton("Правка")
        self.btn_remove_drug = QPushButton("Удалить")
        for b in [self.btn_move_up, self.btn_move_down, self.btn_edit_drug, self.btn_remove_drug]:
            b.setObjectName("DialogOkBtn")
            b.setFixedHeight(35)
            tbl_actions.addWidget(b)

        self.btn_move_up.setFixedWidth(45)
        self.btn_move_down.setFixedWidth(45)
        self.btn_move_up.clicked.connect(self.move_selected_drug_up)
        self.btn_move_down.clicked.connect(self.move_selected_drug_down)
        self.btn_edit_drug.clicked.connect(self.edit_drug_from_table)
        self.btn_remove_drug.clicked.connect(self.remove_drug_from_table)
        right_layout.addLayout(tbl_actions)
        
        # 4. Финальное сохранение
        self.btn_save_tpl = QPushButton("СОХРАНИТЬ ВЕСЬ ПРОТОКОЛ")
        self.btn_save_tpl.setObjectName("DialogOkBtn")
        self.btn_save_tpl.setFixedHeight(50)
        self.btn_save_tpl.clicked.connect(self.save_template)
        
        main_layout.addWidget(self.frame)
        right_layout.addWidget(self.btn_save_tpl)
        
        splitter.addWidget(left_widget)
        splitter.addWidget(right_widget)
        splitter.setSizes([200, 800])
        layout.addWidget(splitter)
        self._update_reorder_buttons()
        self._update_template_order_buttons()

    def load_data(self, selected_key=None):
        self._loading_templates = True
        try:
            self.list_widget.clear()
            for key, data in engine.ordered_templates_items():
                item = QListWidgetItem(data.get("name", key))
                item.setData(Qt.UserRole, key)
                self.list_widget.addItem(item)
        finally:
            self._loading_templates = False
        if selected_key and self._select_template_by_key(selected_key):
            return
        self.clear_editor()
        self._update_template_order_buttons()

    def _template_keys_in_list(self):
        return [
            self.list_widget.item(row).data(Qt.UserRole)
            for row in range(self.list_widget.count())
            if self.list_widget.item(row).data(Qt.UserRole)
        ]

    def _persist_template_order(self):
        if self._loading_templates:
            return
        engine.save_template_order(self._template_keys_in_list())

    def _select_template_by_key(self, key):
        for row in range(self.list_widget.count()):
            if self.list_widget.item(row).data(Qt.UserRole) == key:
                self.list_widget.setCurrentRow(row)
                return True
        return False

    def _update_template_order_buttons(self):
        row = self.list_widget.currentRow() if hasattr(self, "list_widget") else -1
        count = self.list_widget.count() if hasattr(self, "list_widget") else 0
        can_move = 0 <= row < count
        if hasattr(self, "btn_tpl_up"):
            self.btn_tpl_up.setEnabled(can_move and row > 0)
        if hasattr(self, "btn_tpl_down"):
            self.btn_tpl_down.setEnabled(can_move and row < count - 1)
        if hasattr(self, "btn_del_tpl"):
            self.btn_del_tpl.setEnabled(can_move)

    def _on_template_rows_moved(self, *_args):
        self._persist_template_order()
        self._update_template_order_buttons()

    def _move_template_row(self, source_row: int, target_row: int):
        count = self.list_widget.count()
        if source_row < 0 or source_row >= count:
            return
        target_row = max(0, min(int(target_row), count - 1))
        if source_row == target_row:
            return
        item = self.list_widget.takeItem(source_row)
        self.list_widget.insertItem(target_row, item)
        self.list_widget.setCurrentRow(target_row)
        self._persist_template_order()
        self._update_template_order_buttons()

    def move_selected_template_up(self):
        row = self.list_widget.currentRow()
        if row > 0:
            self._move_template_row(row, row - 1)

    def move_selected_template_down(self):
        row = self.list_widget.currentRow()
        if 0 <= row < self.list_widget.count() - 1:
            self._move_template_row(row, row + 1)

    def clear_editor(self):
        self.key_input.clear()
        self.key_input.setEnabled(True)
        self.name_input.clear()
        self.type_combo.setCurrentIndex(0)
        self.current_drugs = []
        self.refresh_table()

    def refresh_table(self, selected_row=None):
        self._cleanup_drug_row_drag()
        self.table.setRowCount(0)
        for row, item in enumerate(self.current_drugs):
            self.table.insertRow(row)
            d_key = item.get("drug", "")
            d_info = engine.drugs.get(d_key, {})

            if item.get("is_multicomp"):
                raw = item.get("raw_text", "")
                import re
                display_text = re.sub(r'\[.*?\]', '', raw).strip()
                self.table.setItem(row, 0, QTableWidgetItem(display_text))
                self.table.setItem(row, 1, QTableWidgetItem("Многокомп."))

                admin_type = item.get("admin_type", "")
                r_info = engine.admin_types.get(admin_type, {})
                self.table.setItem(row, 2, QTableWidgetItem(r_info.get("name_ru", admin_type)))

                dur = item.get("duration_min", 0)
                dur_str = "Болюс" if dur == 0 else ("До конца суток" if dur == -1 else f"{dur} мин")
                self.table.setItem(row, 3, QTableWidgetItem(dur_str))

                diluent_match = re.search(r'\[DIL:(.*?)\]', raw)
                if diluent_match:
                    self.table.setItem(row, 4, QTableWidgetItem(diluent_match.group(1)))
                else:
                    self.table.setItem(row, 4, QTableWidgetItem("Нет"))
            else:
                self.table.setItem(row, 0, QTableWidgetItem(d_info.get("latin", d_key)))

                unit = item.get("unit", d_info.get("unit", "mg"))
                self.table.setItem(row, 1, QTableWidgetItem(f"{item.get('dose', 0):g} {unit}"))

                admin_type = item.get("admin_type", d_info.get("admin_type", "bolus"))
                r_info = engine.admin_types.get(admin_type, {})
                self.table.setItem(row, 2, QTableWidgetItem(r_info.get("name_ru", admin_type)))

                dur = item.get("duration_min", 0)
                dur_str = "Болюс" if dur == 0 else ("До конца суток" if dur == -1 else f"{dur} мин")
                self.table.setItem(row, 3, QTableWidgetItem(dur_str))

                dil = item.get("diluent")
                if dil:
                    dil_info = engine.dilutions.get(dil.get("base"), {})
                    self.table.setItem(row, 4, QTableWidgetItem(f"{dil_info.get('display', dil.get('base'))} {dil.get('volume')}ml"))
                else:
                    self.table.setItem(row, 4, QTableWidgetItem("Нет"))
        if selected_row is not None:
            self._select_row(selected_row)
        self._update_reorder_buttons()

    def _select_row(self, row):
        if not self.current_drugs:
            self.table.clearSelection()
            return
        row = max(0, min(int(row), len(self.current_drugs) - 1))
        self.table.setCurrentCell(row, 0)
        self.table.selectRow(row)

    def _update_reorder_buttons(self):
        row = self.table.currentRow() if hasattr(self, "table") else -1
        has_rows = bool(self.current_drugs)
        can_move = 0 <= row < len(self.current_drugs)
        if hasattr(self, "btn_move_up"):
            self.btn_move_up.setEnabled(has_rows and can_move and row > 0)
        if hasattr(self, "btn_move_down"):
            self.btn_move_down.setEnabled(has_rows and can_move and row < len(self.current_drugs) - 1)
        if hasattr(self, "btn_edit_drug"):
            self.btn_edit_drug.setEnabled(has_rows and can_move)
        if hasattr(self, "btn_remove_drug"):
            self.btn_remove_drug.setEnabled(has_rows and can_move)

    def _format_template_drug_text(self, item):
        if item.get("is_multicomp"):
            raw = item.get("raw_text", "")
            return re.sub(r'\[.*?\]', '', raw).strip() or "Многокомпонентный препарат"

        d_key = item.get("drug", "")
        d_info = engine.drugs.get(d_key, {})
        name = d_info.get("latin", d_key) or "Препарат"
        dose = item.get("dose", 0)
        unit = item.get("unit", d_info.get("unit", "mg"))
        try:
            dose_text = f"{float(dose):g} {unit}".strip()
        except (TypeError, ValueError):
            dose_text = f"{dose} {unit}".strip()
        return f"{name} {dose_text}".strip()

    def _drag_target_row(self, pos: QPoint) -> int:
        if not self.current_drugs:
            return 0
        index = self.table.indexAt(pos)
        if not index.isValid():
            return 0 if pos.y() < 0 else len(self.current_drugs)
        row = index.row()
        rect = self.table.visualRect(self.table.model().index(row, 0))
        if pos.y() < rect.center().y():
            return row
        return row + 1

    def _ensure_drag_indicator(self):
        if self._row_drag_indicator is not None:
            return self._row_drag_indicator
        indicator = QFrame(self.table.viewport())
        indicator.setFixedHeight(3)
        indicator.setStyleSheet("background-color: #2f80ed; border-radius: 1px;")
        indicator.hide()
        self._row_drag_indicator = indicator
        return indicator

    def _begin_drug_row_drag(self, event):
        state = self._row_drag_state or {}
        source_row = state.get("source_row")
        if source_row is None or source_row >= len(self.current_drugs):
            return

        rect = self.table.visualRect(self.table.model().index(source_row, 0))
        ghost = QLabel(self._format_template_drug_text(self.current_drugs[source_row]), self.table.viewport())
        ghost.setAttribute(Qt.WA_TransparentForMouseEvents, True)
        ghost.setFixedSize(max(160, self.table.viewport().width() - 16), max(28, rect.height() - 6))
        ghost.setStyleSheet(
            "background-color: rgba(255, 255, 255, 235);"
            "border: 1.5px solid #2f80ed;"
            "border-radius: 6px;"
            "padding-left: 8px;"
            "font-size: 9pt;"
            "color: #1f2d3d;"
        )
        self._row_drag_ghost = ghost
        state["active"] = True
        self._row_drag_state = state
        ghost.show()
        ghost.raise_()
        self._update_drug_row_drag(event.pos())

    def _update_drug_row_drag(self, pos: QPoint):
        state = self._row_drag_state
        if not state:
            return

        offset = state.get("offset", QPoint(0, 0))
        if self._row_drag_ghost is not None:
            self._row_drag_ghost.move(pos - offset)
            self._row_drag_ghost.raise_()

        target_row = self._drag_target_row(pos)
        state["target_row"] = target_row
        indicator = self._ensure_drag_indicator()
        if target_row <= 0:
            y = 0
        elif target_row >= len(self.current_drugs):
            last_rect = self.table.visualRect(self.table.model().index(len(self.current_drugs) - 1, 0))
            y = last_rect.bottom()
        else:
            y = self.table.visualRect(self.table.model().index(target_row, 0)).top()
        indicator.setGeometry(0, max(0, y), self.table.viewport().width(), 3)
        indicator.show()
        indicator.raise_()

    def _finish_drug_row_drag(self, pos: QPoint):
        state = self._row_drag_state or {}
        was_active = bool(state.get("active"))
        source_row = state.get("source_row")
        target_row = state.get("target_row", self._drag_target_row(pos))
        self._cleanup_drug_row_drag()

        if not was_active or source_row is None:
            return False
        if source_row < 0 or source_row >= len(self.current_drugs):
            return True

        final_row = max(0, min(int(target_row), len(self.current_drugs)))
        if final_row > source_row:
            final_row -= 1
        final_row = max(0, min(final_row, len(self.current_drugs) - 1))
        self._move_drug_row(source_row, final_row)
        return True

    def _cleanup_drug_row_drag(self):
        if self._row_drag_ghost is not None:
            self._row_drag_ghost.hide()
            self._row_drag_ghost.deleteLater()
            self._row_drag_ghost = None
        if self._row_drag_indicator is not None:
            self._row_drag_indicator.hide()
        self._row_drag_state = None

    def hideEvent(self, event):
        self._cleanup_drug_row_drag()
        if hasattr(self, "search_widget"):
            self.search_widget.hide_list()
        super().hideEvent(event)

    def _move_drug_row(self, source_row: int, target_row: int) -> bool:
        if source_row < 0 or source_row >= len(self.current_drugs):
            return False
        target_row = max(0, min(int(target_row), len(self.current_drugs) - 1))
        if source_row == target_row:
            self._select_row(source_row)
            return False
        item = self.current_drugs.pop(source_row)
        self.current_drugs.insert(target_row, item)
        self.refresh_table(selected_row=target_row)
        return True

    def move_selected_drug_up(self):
        row = self.table.currentRow()
        if row <= 0:
            return
        self._move_drug_row(row, row - 1)

    def move_selected_drug_down(self):
        row = self.table.currentRow()
        if row < 0 or row >= len(self.current_drugs) - 1:
            return
        self._move_drug_row(row, row + 1)

    def eventFilter(self, obj, event):
        if obj is self.table.viewport() and event.type() == QEvent.MouseButtonPress:
            index = self.table.indexAt(event.pos())
            if index.isValid() and index.column() == 0 and event.button() == Qt.LeftButton:
                rect = self.table.visualRect(index)
                self._row_drag_state = {
                    "source_row": index.row(),
                    "press_pos": event.pos(),
                    "offset": event.pos() - rect.topLeft(),
                    "active": False,
                    "target_row": index.row(),
                }
        if obj is self.table.viewport() and event.type() == QEvent.MouseMove:
            if self._row_drag_state:
                press_pos = self._row_drag_state.get("press_pos", event.pos())
                if not self._row_drag_state.get("active"):
                    if (event.pos() - press_pos).manhattanLength() >= QApplication.startDragDistance():
                        self._begin_drug_row_drag(event)
                else:
                    self._update_drug_row_drag(event.pos())
                return True
        if obj is self.table.viewport() and event.type() == QEvent.MouseButtonRelease:
            if self._row_drag_state and event.button() == Qt.LeftButton:
                if self._row_drag_state.get("active"):
                    return self._finish_drug_row_drag(event.pos())
                self._cleanup_drug_row_drag()
        if obj is self.table.viewport() and event.type() in (QEvent.Leave, QEvent.Hide):
            if self._row_drag_state and not self._row_drag_state.get("active"):
                self._cleanup_drug_row_drag()
        return super().eventFilter(obj, event)

    def on_drug_selected(self, drug_key):
        d_info = engine.drugs.get(drug_key, {})
        if d_info.get("is_multicomp"):
            from rem_card.ui.doctor_view.administration_dialog import MultiCompCharacteristicsDialog
            dlg = MultiCompCharacteristicsDialog(drug_key, parent=self)
            if dlg.exec():
                raw_text = dlg.result_text
                import re
                route_match = re.search(r'\[ROUTE:(.*?)\]', raw_text)
                admin_type = d_info.get("admin_type", "bolus")
                if route_match:
                    route_name = route_match.group(1)
                    for k, v in engine.admin_types.items():
                        if v.get("name_ru") == route_name:
                            admin_type = k
                            break
                dur_match = re.search(r'\[DUR:(.*?)\]', raw_text)
                dur = 0
                if dur_match:
                    try: dur = int(dur_match.group(1))
                    except: pass
                    
                self.current_drugs.append({
                    "drug": drug_key,
                    "is_multicomp": True,
                    "raw_text": raw_text,
                    "admin_type": admin_type,
                    "duration_min": dur,
                    "dose": 0,
                    "unit": ""
                })
                self.refresh_table(selected_row=len(self.current_drugs) - 1)
        else:
            dlg = TemplateDrugCharacteristicsDialog(drug_key, parent=self)
            if dlg.exec():
                self.current_drugs.append(dlg.result_dict)
                self.refresh_table(selected_row=len(self.current_drugs) - 1)

    def edit_drug_from_table(self):
        row = self.table.currentRow()
        if row < 0: return
        
        item = self.current_drugs[row]
        if item.get("is_multicomp"):
            from rem_card.ui.doctor_view.administration_dialog import MultiCompCharacteristicsDialog
            dlg = MultiCompCharacteristicsDialog(item.get("drug"), parent=self)
            if dlg.exec():
                raw_text = dlg.result_text
                import re
                route_match = re.search(r'\[ROUTE:(.*?)\]', raw_text)
                admin_type = item.get("admin_type", "bolus")
                if route_match:
                    route_name = route_match.group(1)
                    for k, v in engine.admin_types.items():
                        if v.get("name_ru") == route_name:
                            admin_type = k
                            break
                dur_match = re.search(r'\[DUR:(.*?)\]', raw_text)
                dur = 0
                if dur_match:
                    try: dur = int(dur_match.group(1))
                    except: pass
                
                self.current_drugs[row] = {
                    "drug": item.get("drug"),
                    "is_multicomp": True,
                    "raw_text": raw_text,
                    "admin_type": admin_type,
                    "duration_min": dur,
                    "dose": 0,
                    "unit": ""
                }
                self.refresh_table(selected_row=row)
        else:
            dlg = TemplateDrugCharacteristicsDialog(item.get("drug"), initial_data=item, parent=self)
            if dlg.exec():
                self.current_drugs[row] = dlg.result_dict
                self.refresh_table(selected_row=row)

    def remove_drug_from_table(self):
        row = self.table.currentRow()
        if row < 0: return
        del self.current_drugs[row]
        self.refresh_table(selected_row=min(row, len(self.current_drugs) - 1))

    def on_template_selected(self):
        item = self.list_widget.currentItem()
        if not item: return
        key = item.data(Qt.UserRole)
        if not key or key not in engine.templates: return
        
        t = engine.templates[key]
        self.key_input.setText(key)
        self.key_input.setEnabled(False)
        self.name_input.setText(t.get("name", ""))
        
        t_type = t.get("template_type", "simple")
        idx = self.type_combo.findData(t_type)
        if idx >= 0: self.type_combo.setCurrentIndex(idx)
        
        self.current_drugs = t.get("drugs", []).copy()
        self.refresh_table()

    def create_new(self):
        self.list_widget.clearSelection()
        self.clear_editor()
        self.key_input.setFocus()

    def replace_template_dilutions(self):
        current_key = None
        current_item = self.list_widget.currentItem()
        if current_item:
            current_key = current_item.data(Qt.UserRole)

        dialog = TemplateDilutionReplaceDialog(parent=self)
        dialog.exec()
        self.load_data(selected_key=current_key)

    def save_template(self):
        key = self.key_input.text().strip()
        name = self.name_input.text().strip()
        
        if not key or not name:
            CustomMessageBox.warning(self, "Ошибка", "Ключ и название обязательны.")
            return
            
        data = {
            "name": name,
            "template_type": self.type_combo.currentData(),
            "drugs": self.current_drugs
        }
        engine.save_custom_template(key, data)
        order = self._template_keys_in_list()
        if key not in order:
            order.append(key)
        engine.save_template_order(order)
        CustomMessageBox.information(self, "Успех", f"Протокол '{name}' сохранен.")
        
        self.load_data(selected_key=key)

    def delete_selected(self):
        item = self.list_widget.currentItem()
        if not item: return
        key = item.data(Qt.UserRole)
        if CustomMessageBox.question(self, 'Удаление', f"Удалить шаблон '{key}'?") == CustomMessageBox.Yes:
            order = [existing_key for existing_key in self._template_keys_in_list() if existing_key != key]
            engine.delete_custom_template(key)
            engine.save_template_order(order)
            self.load_data()
