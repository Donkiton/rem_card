from copy import deepcopy

from rem_card.ui.shared.custom_message_box import CustomMessageBox
from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QPushButton, QLabel, 
    QTableWidget, QTableWidgetItem, QHeaderView, QLineEdit, 
    QMessageBox, QFormLayout, QDialog, QDialogButtonBox,
    QComboBox, QFrame
)
from PySide6.QtCore import Qt
from rem_card.services.prescription_engine import engine
from rem_card.ui.shared.base_dialog import BaseStyledDialog
from rem_card.ui.styles.theme_manager import get_theme_manager
from rem_card.ui.styles.theme_tokens import token

from PySide6.QtWidgets import QDoubleSpinBox, QSpinBox


def _tokens():
    return get_theme_manager().current_tokens()


def _t(key, default=""):
    return token(_tokens(), key, default)


def _admin_drugs_table_style():
    return f"""
        QTableWidget {{
            background-color: {_t("table.bg")};
            alternate-background-color: {_t("table.row_alt_bg")};
            color: {_t("text.primary")};
            border: 1px solid {_t("border.default")};
            border-radius: {_t("radius.sm")};
            gridline-color: {_t("table.grid")};
            selection-background-color: {_t("table.row_selected_bg")};
            selection-color: {_t("text.inverse")};
        }}
        QTableWidget::item {{
            padding: 5px;
        }}
        QTableWidget::item:selected {{
            background-color: {_t("table.row_selected_bg")};
            color: {_t("text.inverse")};
        }}
        QHeaderView::section {{
            background-color: {_t("table.header_bg")};
            color: {_t("table.header_text")};
            border: none;
            border-right: 1px solid {_t("table.grid")};
            border-bottom: 1px solid {_t("table.grid")};
            padding: 6px;
            font-weight: bold;
        }}
        QTableWidget QScrollBar:vertical {{
            background: {_t("surface.panel")};
            border: 1px solid {_t("border.subtle")};
            border-radius: 7px;
            width: 14px;
            margin: 0px;
        }}
        QTableWidget QScrollBar::handle:vertical {{
            background: {_t("border.default")};
            border-radius: 6px;
            min-height: 34px;
            margin: 2px;
        }}
        QTableWidget QScrollBar::handle:vertical:hover {{
            background: {_t("border.focus")};
        }}
        QTableWidget QScrollBar::add-line:vertical,
        QTableWidget QScrollBar::sub-line:vertical,
        QTableWidget QScrollBar::add-page:vertical,
        QTableWidget QScrollBar::sub-page:vertical {{
            background: transparent;
            border: none;
            height: 0px;
        }}
        QTableWidget QScrollBar:horizontal {{
            background: {_t("surface.panel")};
            border: 1px solid {_t("border.subtle")};
            border-radius: 7px;
            height: 14px;
            margin: 0px;
        }}
        QTableWidget QScrollBar::handle:horizontal {{
            background: {_t("border.default")};
            border-radius: 6px;
            min-width: 34px;
            margin: 2px;
        }}
        QTableWidget QScrollBar::handle:horizontal:hover {{
            background: {_t("border.focus")};
        }}
        QTableWidget QScrollBar::add-line:horizontal,
        QTableWidget QScrollBar::sub-line:horizontal,
        QTableWidget QScrollBar::add-page:horizontal,
        QTableWidget QScrollBar::sub-page:horizontal {{
            background: transparent;
            border: none;
            width: 0px;
        }}
    """


def _volume_value(value):
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if number.is_integer():
        return int(number)
    return number


def _same_dilution(left, right):
    if not isinstance(left, dict) or not isinstance(right, dict):
        return False
    return (
        left.get("base") == right.get("base")
        and _volume_value(left.get("volume")) == _volume_value(right.get("volume"))
    )


def _format_dilution(dilution):
    if not isinstance(dilution, dict):
        return "Без растворителя"
    base = dilution.get("base")
    volume = _volume_value(dilution.get("volume"))
    diluent_info = engine.dilutions.get(base, {})
    display = diluent_info.get("display", base or "")
    if volume is None:
        return str(display or "Без растворителя")
    return f"{display} {volume:g} мл"


def _add_dilution_items(combo):
    for d_key, d_info in sorted(engine.dilutions.items(), key=lambda x: x[1].get("display", x[0])):
        if d_key == "none" or d_info.get("display") == "Без растворителя":
            continue

        display_base = d_info.get("display", d_key)
        for vol in d_info.get("default_volumes", [100]):
            volume = _volume_value(vol)
            if volume is None:
                continue
            combo.addItem(f"{display_base} {volume:g} мл", {"base": d_key, "volume": volume})


class TemplateDilutionVolumeReplaceDialog(BaseStyledDialog):
    def __init__(self, parent=None):
        super().__init__("Изменение объемов шаблонных растворов", parent)
        self.setMinimumSize(820, 620)
        self.matches = []
        self._pending_changes = {}

        self.setup_ui()
        self._update_buttons()

    def setup_ui(self):
        form_layout = QFormLayout()

        self.source_combo = QComboBox()
        _add_dilution_items(self.source_combo)
        form_layout.addRow("Найти растворитель:", self.source_combo)

        search_layout = QHBoxLayout()
        self.btn_find = QPushButton("Найти")
        self.btn_find.setObjectName("DialogOkBtn")
        self.btn_find.setFixedHeight(32)
        search_layout.addStretch()
        search_layout.addWidget(self.btn_find)
        form_layout.addRow("", search_layout)

        self.target_combo = QComboBox()
        _add_dilution_items(self.target_combo)
        form_layout.addRow("Заменить на:", self.target_combo)

        self.content_layout.addLayout(form_layout)

        self.table = QTableWidget()
        self.table.setColumnCount(5)
        self.table.setHorizontalHeaderLabels(["Препарат", "Группа", "Было", "Станет", "Статус"])
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.Stretch)
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
        return bool(self.matches) and bool(source) and bool(target) and not _same_dilution(source, target)

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

    def _refresh_table(self):
        self.table.setRowCount(0)
        target = self._selected_target()

        for row, item in enumerate(self.matches):
            self.table.insertRow(row)

            key = item.get("key")
            data = item.get("data", {})
            latin_item = QTableWidgetItem(data.get("latin", key))
            latin_item.setData(Qt.UserRole, key)
            self.table.setItem(row, 0, latin_item)

            group_key = data.get("group", "")
            group_name = engine.groups.get(group_key, {}).get("name_ru", group_key)
            self.table.setItem(row, 1, QTableWidgetItem(group_name))
            self.table.setItem(row, 2, QTableWidgetItem(_format_dilution(item.get("old_dilution"))))

            new_dilution = item.get("new_dilution")
            self.table.setItem(row, 3, QTableWidgetItem(_format_dilution(new_dilution or target)))
            status_text = "Будет заменено" if new_dilution else "Найдено"
            self.table.setItem(row, 4, QTableWidgetItem(status_text))

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
        for key, data in engine.drugs.items():
            default_dilution = data.get("default_dilution")
            if _same_dilution(default_dilution, source):
                self.matches.append(
                    {
                        "key": key,
                        "data": deepcopy(data),
                        "old_dilution": deepcopy(default_dilution),
                        "new_dilution": None,
                    }
                )

        self._refresh_table()
        count = len(self.matches)
        if count:
            self.status_label.setText(f"Найдено препаратов: {count}. Лишние строки можно убрать из замены.")
        else:
            self.status_label.setText("Препараты с таким растворителем по умолчанию не найдены.")

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
        self.status_label.setText(f"Осталось к замене: {len(self.matches)}.")

    def stage_replacement(self):
        if not self._replacement_is_valid():
            if self.matches and _same_dilution(self._selected_source(), self._selected_target()):
                CustomMessageBox.warning(self, "Ошибка", "Растворитель для замены совпадает с исходным.")
            return

        source = self._selected_source()
        target = self._selected_target()
        count = len(self.matches)
        reply = CustomMessageBox.question(
            self,
            "Подтверждение",
            (
                f"Заменить {_format_dilution(source)} на {_format_dilution(target)} "
                f"у препаратов: {count}?\n\n"
                "Запись в справочник произойдет после «Применить» или «Сохранить»."
            ),
            CustomMessageBox.Yes | CustomMessageBox.No,
            CustomMessageBox.No,
        )
        if reply != CustomMessageBox.Yes:
            return

        self._pending_changes = {}
        target_copy = deepcopy(target)
        for item in self.matches:
            key = item.get("key")
            data = deepcopy(item.get("data", {}))
            data["default_dilution"] = deepcopy(target_copy)
            item["new_dilution"] = deepcopy(target_copy)
            self._pending_changes[key] = data

        self._refresh_table()
        self.status_label.setText("Замена подготовлена. Нажмите «Применить» или «Сохранить».")

    def _save_pending_changes(self):
        if not self._pending_changes:
            return True

        try:
            engine.save_custom_drugs(list(self._pending_changes.items()))
        except Exception as exc:
            CustomMessageBox.critical(self, "Ошибка", f"Не удалось сохранить изменения: {exc}")
            return False

        saved_count = len(self._pending_changes)
        self._pending_changes = {}
        self.find_matches()
        self.status_label.setText(f"Сохранено изменений: {saved_count}.")
        return True

    def apply_changes(self):
        self._save_pending_changes()

    def save_and_close(self):
        if self._save_pending_changes():
            self.accept()

class DrugDialog(BaseStyledDialog):
    def __init__(self, key="", data=None, parent=None):
        super().__init__("Редактирование препарата", parent)
        self.setFixedSize(550, 550)
        
        if data is None:
            data = {}
            
        self.key_input = QLineEdit(key)
        self.key_input.setPlaceholderText("Например: ceftriaxone")
        if key:
            self.key_input.setEnabled(False)
            
        self.latin_input = QLineEdit(data.get("latin", ""))
        self.latin_input.setPlaceholderText("Ceftriaxoni")
        
        self.aliases_input = QLineEdit(", ".join(data.get("aliases", [])))
        self.aliases_input.setPlaceholderText("цеф, цефтриаксон")
        
        self.star_input = QLineEdit(data.get("star_alias", ""))
        self.star_input.setPlaceholderText("цеф*")
        
        self.admin_type_combo = QComboBox()
        # Сохраняем ключи как userData, берем из справочника
        for a_key, a_info in engine.admin_types.items():
            self.admin_type_combo.addItem(a_info.get("name_ru", a_key), a_key)
        
        if data.get("admin_type"):
            idx = self.admin_type_combo.findData(data["admin_type"])
            if idx >= 0:
                self.admin_type_combo.setCurrentIndex(idx)
            
        # Сохраняем исходное значение, так как настройка скрыта из UI
        self.uses_line = data.get("uses_line", False)
        
        self.duration_input = QSpinBox()
        self.duration_input.setRange(0, 1440)
        self.duration_input.setValue(data.get("duration_min", 0))
        self.duration_input.setSuffix(" мин")
        
        self.default_dose_input = QDoubleSpinBox()
        self.default_dose_input.setRange(0, 10000)
        self.default_dose_input.setDecimals(2)
        self.default_dose_input.setValue(data.get("default_dose", 0.0))
        
        self.default_unit_combo = QComboBox()
        self.default_unit_combo.addItems(["mg", "g", "mcg", "ml", "IU"])
        if data.get("unit"):
            self.default_unit_combo.setCurrentText(data["unit"])
            
        dose_layout = QHBoxLayout()
        dose_layout.addWidget(self.default_dose_input)
        dose_layout.addWidget(self.default_unit_combo)
        
        self.forms_input = QLineEdit(", ".join(map(str, data.get("forms", []))))
        self.forms_input.setPlaceholderText("0.5, 1.0, 2.0")
        
        self.forms_unit_combo = QComboBox()
        self.forms_unit_combo.addItems(["mg", "g", "mcg", "ml", "IU", "шт"])
        if data.get("form_unit"):
            self.forms_unit_combo.setCurrentText(data["form_unit"])
        elif data.get("unit"):
            self.forms_unit_combo.setCurrentText(data["unit"])
        
        forms_layout = QHBoxLayout()
        forms_layout.addWidget(self.forms_input)
        forms_layout.addWidget(self.forms_unit_combo)
        
        # ВЫБОР ЛЕКАРСТВЕННОЙ ФОРМЫ
        self.form_combo = QComboBox()
        self.form_combo.addItem("— Не выбрана —", None)
        for f_key, f_info in sorted(engine.forms.items(), key=lambda x: x[1].get('name_ru', x[0])):
            self.form_combo.addItem(f_info.get("name_ru", f_key), f_key)
        if data.get("form_key"):
            idx = self.form_combo.findData(data["form_key"])
            if idx >= 0: self.form_combo.setCurrentIndex(idx)

        # ПРЕДПОЧИТАЕМЫЙ РАСТВОРИТЕЛЬ
        self.diluent_combo = QComboBox()
        self.diluent_combo.addItem("Без растворителя", None)
        
        # Наполняем списком растворителей со всеми доступными объемами
        for d_key, d_info in sorted(engine.dilutions.items(), key=lambda x: x[1].get('display', x[0])):
            # Пропускаем "Без растворителя", если он попал в базу (чтобы не дублировать)
            if d_key == "none" or d_info.get("display") == "Без растворителя":
                continue
                
            display_base = d_info.get("display", d_key)
            for vol in d_info.get("default_volumes", [100]):
                self.diluent_combo.addItem(f"{display_base} {vol} ml", {"base": d_key, "volume": vol})
        
        # Загрузка дефолтного растворителя (если есть)
        default_dil = data.get("default_dilution")
        if default_dil:
            d_base = default_dil.get("base")
            d_vol = default_dil.get("volume")
            # Ищем точное совпадение по базе и объему
            for i in range(self.diluent_combo.count()):
                item_data = self.diluent_combo.itemData(i)
                if item_data and item_data.get("base") == d_base and item_data.get("volume") == d_vol:
                    self.diluent_combo.setCurrentIndex(i)
                    break

        self.group_combo = QComboBox()
        self.group_combo.addItem("— Без группы —", None)
        # Заполняем русскими именами
        for g_key, g_info in sorted(engine.groups.items(), key=lambda x: x[1].get('name_ru', x[0])):
            self.group_combo.addItem(g_info.get("name_ru", g_key), g_key)
            
        if data.get("group"):
            idx = self.group_combo.findData(data["group"])
            if idx >= 0:
                self.group_combo.setCurrentIndex(idx)

        # Концентрация (мг/мл) для растворов
        self.concentration_input = QDoubleSpinBox()
        self.concentration_input.setRange(0, 10000)
        self.concentration_input.setDecimals(2)
        self.concentration_input.setValue(float(data.get("concentration_mg_ml", 0.0)))
        self.concentration_label = QLabel("Концентрация (мг/мл):")
            
        form_layout = QFormLayout()
        form_layout.addRow("Ключ (англ):", self.key_input)
        form_layout.addRow("Название (Латынь):", self.latin_input)
        form_layout.addRow("Алиасы (через запятую):", self.aliases_input)
        form_layout.addRow("Быстрый ввод (*):", self.star_input)
        form_layout.addRow("Тип введения:", self.admin_type_combo)
        form_layout.addRow("Длительность (инфузия):", self.duration_input)
        form_layout.addRow("Дефолтная доза:", dose_layout)
        form_layout.addRow("Формы выпуска:", forms_layout)
        form_layout.addRow("Лекарственная форма:", self.form_combo)
        form_layout.addRow(self.concentration_label, self.concentration_input)
        form_layout.addRow("Предпочитаемый растворитель:", self.diluent_combo)
        form_layout.addRow("Группа:", self.group_combo)

        self.content_layout.addLayout(form_layout)

        # Логика видимости концентрации
        self.form_combo.currentIndexChanged.connect(self.update_concentration_visibility)
        self.update_concentration_visibility()
        
        self.buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        for btn in self.buttons.buttons():
            btn.setObjectName("DialogOkBtn")
        self.buttons.accepted.connect(self.accept)
        self.buttons.rejected.connect(self.reject)
        self.content_layout.addWidget(self.buttons)
        
    def _is_solution_form(self):
        """Проверяет, является ли выбранная форма раствором"""
        form_key = self.form_combo.currentData()
        if not form_key:
            return False
            
        if "solution" in str(form_key).lower():
            return True
            
        form_info = engine.forms.get(form_key, {})
        if "раствор" in form_info.get("name_ru", "").lower():
            return True
            
        return False

    def update_concentration_visibility(self):
        is_solution = self._is_solution_form()
        self.concentration_input.setVisible(is_solution)
        self.concentration_label.setVisible(is_solution)

    def get_data(self):
        forms_str = self.forms_input.text().strip()
        forms = []
        if forms_str:
            try:
                forms = [float(f.strip()) for f in forms_str.split(",")]
            except ValueError:
                CustomMessageBox.warning(self, "Ошибка", "Формы выпуска должны быть числами, разделенными запятыми.")
                return None, None
                
        aliases = [a.strip() for a in self.aliases_input.text().split(",") if a.strip()]
        
        data = {
            "latin": self.latin_input.text().strip(),
            "aliases": aliases,
            "star_alias": self.star_input.text().strip(),
            "admin_type": self.admin_type_combo.currentData(),
            "uses_line": self.uses_line,
            "duration_min": self.duration_input.value(),
            "default_dose": self.default_dose_input.value(),
            "forms": forms,
            "unit": self.default_unit_combo.currentText(),
            "form_unit": self.forms_unit_combo.currentText(),
            "form_key": self.form_combo.currentData(),
            "concentration_mg_ml": self.concentration_input.value() if self._is_solution_form() else 0,
            "default_dilution": None
        }

        dil_data = self.diluent_combo.currentData()
        if dil_data:
            data["default_dilution"] = dil_data
        
        group_key = self.group_combo.currentData()
        if group_key:
            data["group"] = group_key
            
        return self.key_input.text().strip(), data

class MultiCompDrugDialog(BaseStyledDialog):
    def __init__(self, key="", data=None, parent=None):
        super().__init__("Многокомпонентный препарат", parent)
        self.setFixedSize(600, 750)
        
        if data is None:
            data = {}
            
        self.key_input = QLineEdit(key)
        self.key_input.setPlaceholderText("Например: polarka")
        if key:
            self.key_input.setEnabled(False)
            
        self.latin_input = QLineEdit(data.get("latin", ""))
        self.latin_input.setPlaceholderText("Смесь...")
        
        self.aliases_input = QLineEdit(", ".join(data.get("aliases", [])))
        self.star_input = QLineEdit(data.get("star_alias", ""))
        
        self.admin_type_combo = QComboBox()
        for a_key, a_info in engine.admin_types.items():
            self.admin_type_combo.addItem(a_info.get("name_ru", a_key), a_key)
        if data.get("admin_type"):
            idx = self.admin_type_combo.findData(data["admin_type"])
            if idx >= 0: self.admin_type_combo.setCurrentIndex(idx)
            
        # Сохраняем исходное значение, так как настройка скрыта из UI
        self.uses_line = data.get("uses_line", False)
        
        self.duration_input = QSpinBox()
        self.duration_input.setRange(0, 1440)
        self.duration_input.setValue(data.get("duration_min", 0))
        self.duration_input.setSuffix(" мин")
        
        self.form_combo = QComboBox()
        self.form_combo.addItem("— Не выбрана —", None)
        for f_key, f_info in sorted(engine.forms.items(), key=lambda x: x[1].get('name_ru', x[0])):
            self.form_combo.addItem(f_info.get("name_ru", f_key), f_key)
        if data.get("form_key"):
            idx = self.form_combo.findData(data["form_key"])
            if idx >= 0: self.form_combo.setCurrentIndex(idx)

        self.diluent_combo = QComboBox()
        self.diluent_combo.addItem("Без растворителя", None)
        for d_key, d_info in sorted(engine.dilutions.items(), key=lambda x: x[1].get('display', x[0])):
            if d_key == "none" or d_info.get("display") == "Без растворителя": continue
            display_base = d_info.get("display", d_key)
            for vol in d_info.get("default_volumes", [100]):
                self.diluent_combo.addItem(f"{display_base} {vol} ml", {"base": d_key, "volume": vol})
        
        default_dil = data.get("default_dilution")
        if default_dil:
            for i in range(self.diluent_combo.count()):
                item_data = self.diluent_combo.itemData(i)
                if item_data and item_data.get("base") == default_dil.get("base") and item_data.get("volume") == default_dil.get("volume"):
                    self.diluent_combo.setCurrentIndex(i)
                    break

        self.group_combo = QComboBox()
        self.group_combo.addItem("— Без группы —", None)
        for g_key, g_info in sorted(engine.groups.items(), key=lambda x: x[1].get('name_ru', x[0])):
            self.group_combo.addItem(g_info.get("name_ru", g_key), g_key)
        if data.get("group"):
            idx = self.group_combo.findData(data["group"])
            if idx >= 0: self.group_combo.setCurrentIndex(idx)
            
        form_layout = QFormLayout()
        form_layout.addRow("Ключ (англ):", self.key_input)
        form_layout.addRow("Название (Латынь):", self.latin_input)
        form_layout.addRow("Алиасы (через запятую):", self.aliases_input)
        form_layout.addRow("Быстрый ввод (*):", self.star_input)
        form_layout.addRow("Тип введения:", self.admin_type_combo)
        form_layout.addRow("Длительность (инф):", self.duration_input)
        form_layout.addRow("Лекарственная форма:", self.form_combo)
        form_layout.addRow("Растворитель по умолчанию:", self.diluent_combo)
        form_layout.addRow("Группа:", self.group_combo)
        
        # Компоненты
        self.components = []
        saved_comps = data.get("components", [])
        
        # Подготовим список всех препаратов для выпадающих списков
        self.drug_options = []
        for d_key, d_info in sorted(engine.drugs.items(), key=lambda x: x[1].get('latin', x[0]).lower()):
            if not d_info.get("is_multicomp"):
                self.drug_options.append((d_key, f"{d_info.get('latin', d_key)} ({d_info.get('unit', '')})"))
                
        for i in range(4):
            comp_layout = QHBoxLayout()
            combo = QComboBox()
            combo.addItem("— Не выбран —", None)
            for d_key, d_disp in self.drug_options:
                combo.addItem(d_disp, d_key)
                
            spin = QDoubleSpinBox()
            spin.setRange(0, 10000)
            spin.setDecimals(2)
            
            if i < len(saved_comps):
                c_data = saved_comps[i]
                c_key = c_data.get("drug_key")
                idx = combo.findData(c_key)
                if idx >= 0:
                    combo.setCurrentIndex(idx)
                spin.setValue(c_data.get("default_dose", 0.0))
                
            comp_layout.addWidget(combo, 2)
            comp_layout.addWidget(spin, 1)
            form_layout.addRow(f"Компонент {i+1}:", comp_layout)
            self.components.append((combo, spin))
            
        self.content_layout.addLayout(form_layout)

        self.buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        for btn in self.buttons.buttons():
            btn.setObjectName("DialogOkBtn")
        self.buttons.accepted.connect(self.accept)
        self.buttons.rejected.connect(self.reject)
        self.content_layout.addWidget(self.buttons)
        
    def get_data(self):
        aliases = [a.strip() for a in self.aliases_input.text().split(",") if a.strip()]
        
        comps = []
        for combo, spin in self.components:
            d_key = combo.currentData()
            dose = spin.value()
            if d_key and engine.component_dose_is_positive(dose):
                comps.append({
                    "drug_key": d_key,
                    "default_dose": dose
                })
                
        data = {
            "is_multicomp": True,
            "latin": self.latin_input.text().strip(),
            "aliases": aliases,
            "star_alias": self.star_input.text().strip(),
            "admin_type": self.admin_type_combo.currentData(),
            "uses_line": self.uses_line,
            "duration_min": self.duration_input.value(),
            "form_key": self.form_combo.currentData(),
            "default_dilution": None,
            "components": comps,
            "unit": "ml", # Для совместимости с UI
            "form_unit": "ml",
            "forms": []   # Для совместимости с UI
        }

        dil_data = self.diluent_combo.currentData()
        if dil_data:
            data["default_dilution"] = dil_data
        
        group_key = self.group_combo.currentData()
        if group_key:
            data["group"] = group_key
            
        return self.key_input.text().strip(), data

class DrugsDictWidget(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
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
        
        header = QLabel("Справочник Препаратов")
        header.setProperty("heading", "true")
        header.setStyleSheet("border: none; background: transparent;")
        layout.addWidget(header)
        
        # Фильтры
        filter_layout = QHBoxLayout()
        filter_layout.addWidget(QLabel("Фильтр по группе:"))
        self.group_filter = QComboBox()
        self.group_filter.addItem("Все группы")
        for g_key, g_data in sorted(engine.groups.items(), key=lambda x: x[1].get('name_ru', x[0])):
            self.group_filter.addItem(g_data.get("name_ru", g_key), g_key)
        
        # Подключаем сигнал так, чтобы он не передавал индекс (int) в метод load_data
        self.group_filter.currentIndexChanged.connect(lambda: self.load_data())
        
        filter_layout.addWidget(self.group_filter)
        
        self.search_input = QLineEdit()
        self.search_input.setPlaceholderText("Поиск по названию/латыни...")
        
        # Аналогично для текстового поля
        self.search_input.textChanged.connect(lambda: self.load_data())
        filter_layout.addWidget(self.search_input)
        
        layout.addLayout(filter_layout)
        
        self.table = QTableWidget()
        self.table.setColumnCount(7)
        self.table.setHorizontalHeaderLabels(["Латынь", "Ввод", "Тип", "Линия", "Дозы", "Формы выпуска", "Группа"])
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.Stretch)
        self.table.setSelectionBehavior(QTableWidget.SelectRows)
        self.table.setSelectionMode(QTableWidget.SingleSelection)
        self.table.setEditTriggers(QTableWidget.NoEditTriggers)
        self.table.itemDoubleClicked.connect(self.edit_item)
        self.table.setStyleSheet(_admin_drugs_table_style())
        layout.addWidget(self.table)
        
        btn_layout = QHBoxLayout()
        self.btn_add = QPushButton("Добавить")
        self.btn_add_multicomp = QPushButton("Добавить многокомп. препарат")
        self.btn_edit = QPushButton("Изменить")
        self.btn_delete = QPushButton("Удалить")
        self.btn_replace_template_dilutions = QPushButton("Изменить обьемы шабл. растворов")
        
        for btn in [
            self.btn_add,
            self.btn_add_multicomp,
            self.btn_edit,
            self.btn_delete,
            self.btn_replace_template_dilutions,
        ]:
            btn.setObjectName("DialogOkBtn")
            btn.setFixedHeight(35)
            btn_layout.addWidget(btn)
            
        layout.addLayout(btn_layout)
        
        self.btn_back = QPushButton("← Вернуться в меню")
        self.btn_back.setObjectName("DialogOkBtn")
        self.btn_back.setFixedHeight(40)
        layout.addWidget(self.btn_back)

        main_layout.addWidget(self.frame)
        
        self.btn_add.clicked.connect(self.add_item)
        self.btn_add_multicomp.clicked.connect(self.add_multicomp_item)
        self.btn_edit.clicked.connect(self.edit_item)
        self.btn_delete.clicked.connect(self.delete_item)
        self.btn_replace_template_dilutions.clicked.connect(self.replace_template_dilutions)
        
    def load_data(self):
        self.table.setRowCount(0)
        
        search_text = self.search_input.text().lower()
        selected_group = self.group_filter.currentData()
        
        for row, (key, data) in enumerate(engine.drugs.items()):
            # Фильтрация
            if selected_group and data.get("group") != selected_group:
                continue
                
            latin = data.get("latin", "").lower()
            aliases = " ".join(data.get("aliases", [])).lower()
            if search_text and search_text not in latin and search_text not in aliases:
                continue
                
            self.table.insertRow(self.table.rowCount())
            r = self.table.rowCount() - 1
            
            # self.table.setItem(r, 0, QTableWidgetItem(key)) # Ключ убрали
            
            # Сохраняем ключ в UserRole первого элемента строки
            latin_item = QTableWidgetItem(data.get("latin", ""))
            latin_item.setData(Qt.UserRole, key)
            self.table.setItem(r, 0, latin_item)
            
            self.table.setItem(r, 1, QTableWidgetItem(data.get("star_alias", "")))
            
            # Перевод admin_type обратно для таблицы
            a_type = data.get("admin_type", "")
            a_name = engine.admin_types.get(a_type, {}).get("name_ru", a_type)
            self.table.setItem(r, 2, QTableWidgetItem(a_name))
            self.table.setItem(r, 3, QTableWidgetItem("Да" if data.get("uses_line") else "Нет"))
            
            if data.get("is_multicomp"):
                self.table.setItem(r, 4, QTableWidgetItem("Многокомп."))
                self.table.setItem(r, 5, QTableWidgetItem("—"))
            else:
                unit = data.get("unit", "")
                form_unit = data.get("form_unit", unit)
                
                def_dose = data.get("default_dose", 0)
                self.table.setItem(r, 4, QTableWidgetItem(f"{def_dose} {unit}"))
                
                forms_str = ", ".join(map(str, data.get("forms", [])))
                self.table.setItem(r, 5, QTableWidgetItem(f"{forms_str} {form_unit}"))
            
            group_key = data.get("group", "")
            group_name = engine.groups.get(group_key, {}).get("name_ru", group_key)
            self.table.setItem(r, 6, QTableWidgetItem(group_name))

    def add_item(self):
        dialog = DrugDialog(parent=self)
        if dialog.exec():
            key, data = dialog.get_data()
            if not key or not data:
                return
            if key in engine.drugs:
                CustomMessageBox.warning(self, "Ошибка", "Препарат с таким ключом уже существует.")
                return
            
            engine.save_custom_drug(key, data)
            self.load_data()

    def add_multicomp_item(self):
        dialog = MultiCompDrugDialog(parent=self)
        if dialog.exec():
            key, data = dialog.get_data()
            if not key or not data:
                return
            if key in engine.drugs:
                CustomMessageBox.warning(self, "Ошибка", "Препарат с таким ключом уже существует.")
                return
            
            engine.save_custom_drug(key, data)
            self.load_data()

    def edit_item(self):
        current_row = self.table.currentRow()
        if current_row < 0:
            CustomMessageBox.warning(self, "Внимание", "Выберите препарат для редактирования.")
            return
            
        key = self.table.item(current_row, 0).data(Qt.UserRole)
        data = engine.drugs.get(key, {})
        
        if data.get("is_multicomp"):
            dialog = MultiCompDrugDialog(key=key, data=data, parent=self)
        else:
            dialog = DrugDialog(key=key, data=data, parent=self)
            
        if dialog.exec():
            _, new_data = dialog.get_data()
            if not new_data:
                return
                
            engine.save_custom_drug(key, new_data)
            self.load_data()

    def delete_item(self):
        current_row = self.table.currentRow()
        if current_row < 0:
            CustomMessageBox.warning(self, "Внимание", "Выберите препарат для удаления.")
            return
            
        key = self.table.item(current_row, 0).data(Qt.UserRole)
        
        reply = CustomMessageBox.question(self, 'Удаление', 
                                     f"Вы уверены, что хотите удалить '{key}'?",
                                     CustomMessageBox.Yes | CustomMessageBox.No, CustomMessageBox.No)
                                     
        if reply == CustomMessageBox.Yes:
            engine.delete_custom_drug(key)
            self.load_data()

    def replace_template_dilutions(self):
        dialog = TemplateDilutionVolumeReplaceDialog(parent=self)
        dialog.exec()
        self.load_data()
