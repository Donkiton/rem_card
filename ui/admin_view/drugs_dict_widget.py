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

from PySide6.QtWidgets import QDoubleSpinBox, QSpinBox

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
        self.table.setStyleSheet("background-color: white;")
        self.table.setColumnCount(7)
        self.table.setHorizontalHeaderLabels(["Латынь", "Ввод", "Тип", "Линия", "Дозы", "Формы выпуска", "Группа"])
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.Stretch)
        self.table.setSelectionBehavior(QTableWidget.SelectRows)
        self.table.setSelectionMode(QTableWidget.SingleSelection)
        self.table.setEditTriggers(QTableWidget.NoEditTriggers)
        self.table.itemDoubleClicked.connect(self.edit_item)
        layout.addWidget(self.table)
        
        btn_layout = QHBoxLayout()
        self.btn_add = QPushButton("Добавить")
        self.btn_add_multicomp = QPushButton("Добавить многокомп. препарат")
        self.btn_edit = QPushButton("Изменить")
        self.btn_delete = QPushButton("Удалить")
        
        for btn in [self.btn_add, self.btn_add_multicomp, self.btn_edit, self.btn_delete]:
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
