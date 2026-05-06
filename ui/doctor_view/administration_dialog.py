from rem_card.ui.shared.custom_message_box import CustomMessageBox
from rem_card.ui.shared.custom_title_bar import CustomTitleBar
from rem_card.ui.styles.theme import STYLE_CUSTOM_DIALOG
from PySide6.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QPushButton, QLabel, 
    QTextEdit, QComboBox, QDateTimeEdit, QLineEdit, QFormLayout, QDoubleSpinBox, QCheckBox,
    QFrame
)
from PySide6.QtCore import Qt, QDateTime, Signal, QPoint
from ...data.dto.remcard_dto import AdministrationDTO
from ...services.prescription_engine import engine
import re
from rem_card.ui.shared.base_dialog import BaseStyledDialog

class ManualEntryDialog(BaseStyledDialog):

    """Диалог для ручного ввода препарата (Ruki)."""
    def __init__(self, parent=None):
        super().__init__("Ручное добавление препарата", parent)
        self.setFixedWidth(500)
        self.setMinimumHeight(450)
        self.result_text = ""
        self.setup_ui_content()

    def setup_ui_content(self):
        from rem_card.ui.styles.theme import BG_CARD, BORDER_RADIUS_SM, BORDER_LIGHT

        self.content_widget.setStyleSheet(f"""
            QLabel {{ background-color: transparent; color: #2c3e50; }}
            QLineEdit, QComboBox, QDoubleSpinBox, QCheckBox {{ 
                background-color: {BG_CARD}; 
                border: 1px solid {BORDER_LIGHT};
                border-radius: {BORDER_RADIUS_SM};
                padding: 4px;
                color: #2c3e50;
            }}
        """)

        layout = self.content_layout
        form = QFormLayout()

        # 1. Название
        self.name_input = QLineEdit()
        self.name_input.setPlaceholderText('Например, "Препарат Н"')
        form.addRow("Введите название препарата:", self.name_input)

        # 2. Форма препарата
        self.form_combo = QComboBox()
        self.form_combo.addItem("— Не выбрана —", "")
        for f_key, f_info in sorted(engine.forms.items(), key=lambda x: x[1].get('name_ru', x[0])):
            self.form_combo.addItem(f_info.get("name_ru", f_key), f_key)
        self.form_combo.currentIndexChanged.connect(self.on_form_changed)
        form.addRow("Выберите форму препарата:", self.form_combo)

        # 2.5 Путь введения
        self.route_combo = QComboBox()
        self.route_combo.addItem("— Не выбран —", "")
        for r_key, r_info in sorted(engine.admin_types.items(), key=lambda x: x[1].get('name_ru', x[0])):
            self.route_combo.addItem(r_info.get("name_ru", r_key), r_key)
        form.addRow("Выберите путь введения:", self.route_combo)

        # 3. Доза
        self.dose_spin = QDoubleSpinBox()
        self.dose_spin.setRange(0, 10000)
        self.dose_spin.setDecimals(2)
        
        self.unit_combo = QComboBox()
        self.unit_combo.addItems(["мг", "мкг", "г", "мл", "ЕД", "шт"])
        
        dose_layout = QHBoxLayout()
        dose_layout.addWidget(self.dose_spin)
        dose_layout.addWidget(self.unit_combo)
        form.addRow("Введите дозировку:", dose_layout)
        
        # 4. Длительность введения
        duration_layout = QHBoxLayout()
        self.duration_combo = QComboBox()
        durations = [0, 5, 10, 15, 20, 30, 45, 60, 90, 120, 180, 240]
        for d in durations:
            self.duration_combo.addItem(f"{d} мин" if d > 0 else "Болюс", d)
        self.duration_combo.setEditable(True)
        self.duration_combo.setCurrentIndex(0)
            
        duration_layout.addWidget(self.duration_combo)
        
        self.end_of_day_cb = QCheckBox("до конца суток")
        self.end_of_day_cb.toggled.connect(self.on_end_of_day_toggled)
        duration_layout.addWidget(self.end_of_day_cb)
        
        self.form_layout_ref = form
        self.duration_row_index = form.rowCount()
        form.addRow("Длительность введения:", duration_layout)

        # 5. Растворитель
        self.diluent_combo = QComboBox()
        self.diluent_combo.addItem("Без растворителя", "")
        
        for d_key, d_info in sorted(engine.dilutions.items(), key=lambda x: x[1].get('display', x[0])):
            if d_key == "none": continue
            display_base = d_info.get("display", d_key)
            for vol in d_info.get("default_volumes", [100]):
                dil_str = f"S. {display_base} - {vol}мл"
                self.diluent_combo.addItem(f"{display_base} {vol} мл", dil_str)
        
        self.diluent_row_index = form.rowCount()
        form.addRow("Растворитель:", self.diluent_combo)

        layout.addLayout(form)
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
        layout.addLayout(btns)

    def on_form_changed(self):
        form_key = self.form_combo.currentData()
        can_dilute = True
        if form_key:
            form_info = engine.forms.get(form_key, {})
            can_dilute = form_info.get("can_dilute", False)
        
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
            can_dilute = form_info.get("can_dilute", False)
            
        if can_dilute:
            self.duration_combo.setEnabled(not checked)
            
        if checked:
            self.duration_combo.setEditText("До конца суток")

    def on_add(self):
        name = self.name_input.text().strip()
        if not name:
            CustomMessageBox.warning(self, "Ошибка", "Пожалуйста, введите название препарата.")
            return

        parts = []
        form_key = self.form_combo.currentData()
        form_info = engine.forms.get(form_key, {}) if form_key else {}
        prefix = form_info.get("latin_abbr", "").strip()
        
        diluent = self.diluent_combo.currentData()
        is_real_diluent = diluent and "Без растворителя" not in diluent
        
        if form_key == "lyophilisate" and is_real_diluent:
            prefix = "S."

        if prefix:
            if not prefix.endswith("."):
                prefix += "."
            full_drug_name = f"{prefix} {name}"
        else:
            full_drug_name = name
            
        parts.append(full_drug_name)
        
        dose_val = self.dose_spin.value()
        unit = self.unit_combo.currentText()
        main_line = " ".join(parts)
        
        can_dilute = form_info.get("can_dilute", False) if form_key else True
        if can_dilute:
            diluent = self.diluent_combo.currentData()
            if diluent:
                if not diluent.startswith("S. "):
                    diluent = f"S. {diluent}"
                if " - - " in diluent:
                    diluent = re.sub(r'\s*-\s*-+\s*', ' - ', diluent)
                if " - " not in diluent:
                    diluent = re.sub(r'\s+(\d+)\s*(мл|ml)', r' - \1 \2', diluent)
                main_line += f" + {diluent}"

        if dose_val > 0:
            main_line += f" [DOSE:{dose_val:g}] [UNIT:{unit}]"
            
        route_key = self.route_combo.currentData()
        if route_key:
            route_name = self.route_combo.currentText()
            main_line += f" [ROUTE:{route_name}]"
            
        if can_dilute:
            duration_text = self.duration_combo.currentText()
            if self.end_of_day_cb.isChecked() or duration_text == "До конца суток":
                main_line += " [DUR:-1]"
            else:
                try:
                    nums = re.findall(r"\d+", duration_text)
                    if nums:
                        duration_val = int(nums[0])
                        if duration_val > 0:
                            main_line += f" [DUR:{duration_val}]"
                    else:
                        duration_val = self.duration_combo.currentData()
                        if duration_val and duration_val > 0:
                            main_line += f" [DUR:{duration_val}]"
                except:
                    pass
        else:
            main_line += " [DUR:0]"

        # Для диалога выбора препарата (DrugCharacteristicsDialog) сохраняем
        # исходный ключ, чтобы downstream-логика не теряла категорию.
        if getattr(self, "drug_key", None):
            main_line += f" [KEY:{self.drug_key}]"
        
        main_line += " [RU]"
        self.result_text = main_line
        self.accept()

class MultiCompCharacteristicsDialog(BaseStyledDialog):
    """Диалог для многокомпонентного препарата."""
    def __init__(self, drug_key, parent=None):
        self.drug_data = engine.drugs.get(drug_key, {})
        title = "Настройка: " + self.drug_data.get("latin", drug_key)
        super().__init__(title, parent)
        self.drug_key = drug_key
        self.result_text = ""
        self.setFixedWidth(550)
        self.setup_ui_content()

    def setup_ui_content(self):
        from rem_card.ui.styles.theme import BG_CARD, BORDER_RADIUS_SM, BORDER_LIGHT

        self.content_widget.setStyleSheet(f"""
            QLabel {{ background-color: transparent; color: #2c3e50; }}
            QLineEdit, QComboBox, QDoubleSpinBox, QCheckBox {{ 
                background-color: {BG_CARD}; 
                border: 1px solid {BORDER_LIGHT};
                border-radius: {BORDER_RADIUS_SM};
                padding: 4px;
                color: #2c3e50;
            }}
        """)

        layout = self.content_layout
        form = QFormLayout()

        # Тип введения
        self.route_combo = QComboBox()
        self.route_combo.addItem("— Не выбран —", "")
        for r_key, r_info in sorted(engine.admin_types.items(), key=lambda x: x[1].get('name_ru', x[0])):
            self.route_combo.addItem(r_info.get("name_ru", r_key), r_key)
        admin_type = self.drug_data.get("admin_type")
        if admin_type:
            idx = self.route_combo.findData(admin_type)
            if idx >= 0: self.route_combo.setCurrentIndex(idx)
        form.addRow("Путь введения:", self.route_combo)

        # Компоненты
        self.comp_spins = []
        for i, comp in enumerate(self.drug_data.get("components", [])):
            c_key = comp.get("drug_key")
            c_drug = engine.drugs.get(c_key, {})
            c_lat = c_drug.get("latin", c_key)
            c_unit = c_drug.get("unit", "")
            
            spin = QDoubleSpinBox()
            spin.setRange(0, 10000)
            spin.setDecimals(2)
            spin.setValue(comp.get("default_dose", 0))
            
            lbl = QLabel(f"{c_lat} ({c_unit}):")
            form.addRow(lbl, spin)
            self.comp_spins.append((comp, spin, c_drug))

        # Длительность
        duration_layout = QHBoxLayout()
        self.duration_combo = QComboBox()
        durations = [0, 5, 10, 15, 20, 30, 45, 60, 90, 120, 180, 240]
        for d in durations:
            self.duration_combo.addItem(f"{d} мин" if d > 0 else "Болюс", d)
        self.duration_combo.setEditable(True)
        
        def_dur = self.drug_data.get("duration_min", 0)
        idx = self.duration_combo.findData(def_dur)
        if idx >= 0: self.duration_combo.setCurrentIndex(idx)
        else: self.duration_combo.setCurrentText(str(def_dur))
        
        duration_layout.addWidget(self.duration_combo)
        self.end_of_day_cb = QCheckBox("до конца суток")
        duration_layout.addWidget(self.end_of_day_cb)
        form.addRow("Длительность:", duration_layout)

        # Растворитель
        self.diluent_combo = QComboBox()
        self.diluent_combo.addItem("Без растворителя", "")
        for d_key, d_info in sorted(engine.dilutions.items(), key=lambda x: x[1].get('display', x[0])):
            if d_key == "none": continue
            display_base = d_info.get("display", d_key)
            for vol in d_info.get("default_volumes", [100]):
                dil_str = f"S. {display_base} - {vol} мл"
                self.diluent_combo.addItem(f"{display_base} {vol} мл", dil_str)
                
        def_dil = self.drug_data.get("default_dilution")
        if def_dil:
            d_base = def_dil.get("base")
            d_vol = def_dil.get("volume")
            for i in range(self.diluent_combo.count()):
                d_str = self.diluent_combo.itemData(i)
                if isinstance(d_str, str) and str(d_vol) in d_str and self.diluent_combo.itemText(i).startswith(engine.dilutions.get(d_base, {}).get("display", "")):
                    self.diluent_combo.setCurrentIndex(i)
                    break
                    
        form.addRow("Растворитель:", self.diluent_combo)
        layout.addLayout(form)

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
        layout.addLayout(btns)

    def on_add(self):
        form_key = self.drug_data.get("form_key")
        form_info = engine.forms.get(form_key, {})
        prefix = form_info.get("latin_abbr", "").strip()
        if prefix and not prefix.endswith("."):
            prefix += "."
            
        comp_strs = []
        for comp, spin, c_drug in self.comp_spins:
            c_key = comp.get("drug_key")
            c_dose = spin.value()
            c_lat = c_drug.get("latin", c_key)
            c_unit = c_drug.get("unit", "")
            
            c_str = f"{prefix} {c_lat} - {c_dose:g} {c_unit}" if prefix else f"{c_lat} - {c_dose:g} {c_unit}"
            comp_strs.append(c_str)
            
        main_line = " + ".join(comp_strs)
        
        diluent = self.diluent_combo.currentData()
        if diluent:
            diluent = re.sub(r'\s*-\s*-+\s*', ' - ', diluent)
            main_line += f" [DIL:{diluent}]"
            
        main_line += f" [KEY:{self.drug_key}]"
        
        route_name = self.route_combo.currentText()
        if route_name and route_name != "— Не выбран —":
            main_line += f" [ROUTE:{route_name}]"
            
        if self.end_of_day_cb.isChecked() or self.duration_combo.currentText() == "До конца суток":
            main_line += " [DUR:-1]"
        else:
            duration_text = self.duration_combo.currentText()
            try:
                nums = re.findall(r"\d+", duration_text)
                if nums:
                    val = int(nums[0])
                    if val > 0:
                        main_line += f" [DUR:{val}]"
            except: pass
            
        self.result_text = main_line
        self.accept()

class DrugCharacteristicsDialog(ManualEntryDialog):
    """Диалог для выбора характеристик выбранного препарата (копия ManualEntryDialog)."""
    def __init__(self, drug_key, initial_dose=None, parent=None):
        super().__init__(parent)
        self.title_bar.title_label.setText("Выберите характеристики препарата")
        self.setFixedWidth(500)
        self.setMinimumHeight(450)
        self.result_text = ""
        
        self.drug_key = drug_key
        self.drug_data = engine.drugs.get(drug_key, {})
        
        self.fill_from_drug_data(initial_dose)

    def fill_from_drug_data(self, initial_dose):
        name = self.drug_data.get("latin", self.drug_key)
        self.name_input.setText(name)

        default_form = self.drug_data.get("form_key")
        idx = self.form_combo.findData(default_form)
        if idx >= 0:
            self.form_combo.setCurrentIndex(idx)

        default_admin = self.drug_data.get("admin_type", "bolus")
        idx = self.route_combo.findData(default_admin)
        if idx >= 0:
            self.route_combo.setCurrentIndex(idx)

        default_dose = initial_dose if initial_dose is not None else self.drug_data.get("default_dose", 0.0)
        self.dose_spin.setValue(default_dose)
        
        default_unit = self.drug_data.get("unit", "mg")
        unit_map_ru = {"mg": "мг", "mcg": "мкг", "g": "г", "ml": "мл", "IU": "ЕД", "шт": "шт"}
        unit_ru = unit_map_ru.get(default_unit, default_unit)
        self.unit_combo.setCurrentText(unit_ru)

        default_duration = self.drug_data.get("duration_min", 0)
        idx = self.duration_combo.findData(default_duration)
        if idx >= 0:
            self.duration_combo.setCurrentIndex(idx)
        else:
             self.duration_combo.setEditText(f"{default_duration} мин")

        default_dil = self.drug_data.get("default_dilution")
        if default_dil:
            d_base = default_dil.get("base")
            d_vol = default_dil.get("volume")
            dil_info = engine.dilutions.get(d_base, {})
            dil_display = dil_info.get("display", d_base)
            target_str = f"S. {dil_display} - {d_vol}мл"
            
            for i in range(self.diluent_combo.count()):
                if self.diluent_combo.itemData(i) == target_str:
                    self.diluent_combo.setCurrentIndex(i)
                    break

        self.on_form_changed()

class AdministrationDialog(BaseStyledDialog):
    def __init__(self, order, planned_time, parent=None):
        super().__init__(f"Выполнение: {order.latin}", parent)
        self.order = order
        self.planned_time = planned_time
        self.result_admin = None
        
        self.setFixedWidth(400)
        self.setup_ui_content()

    def setup_ui_content(self):
        from rem_card.ui.styles.theme import BG_CARD, BORDER_RADIUS_SM, BORDER_LIGHT
        
        self.content_widget.setStyleSheet(f"""
            QLabel {{ background-color: transparent; border: none; color: #2c3e50; }}
            QComboBox, QDateTimeEdit, QTextEdit {{ 
                background-color: {BG_CARD}; 
                border: 1px solid {BORDER_LIGHT};
                border-radius: {BORDER_RADIUS_SM};
                color: #2c3e50;
            }}
        """)
        
        layout = self.content_layout
        dose_str = f"{self.order.dose_value:g} {self.order.dose_unit}".strip()
        if dose_str == "0":
            dose_str = ""
        info_lbl = QLabel(f"<b>{self.order.latin}</b> {dose_str}".strip())
        info_lbl.setStyleSheet("font-size: 14px;")
        layout.addWidget(info_lbl)
        
        layout.addWidget(QLabel("Статус:"))
        self.status_combo = QComboBox()
        self.status_combo.addItem("Выполнено", userData="done")
        self.status_combo.addItem("Пропущено", userData="skipped")
        self.status_combo.addItem("Отложено", userData="delayed")
        layout.addWidget(self.status_combo)
        
        layout.addWidget(QLabel("Фактическое время:"))
        self.time_edit = QDateTimeEdit(QDateTime(self.planned_time))
        self.time_edit.setDisplayFormat("dd.MM HH:mm")
        layout.addWidget(self.time_edit)
        
        layout.addWidget(QLabel("Комментарий:"))
        self.comment_edit = QTextEdit()
        self.comment_edit.setFixedHeight(60)
        layout.addWidget(self.comment_edit)
        
        # Кнопки
        btn_layout = QHBoxLayout()
        btn_save = QPushButton("СОХРАНИТЬ")
        btn_save.setObjectName("DialogOkBtn")
        btn_save.clicked.connect(self.on_save)
        
        btn_cancel = QPushButton("Отмена")
        btn_cancel.setObjectName("DialogOkBtn")
        btn_cancel.clicked.connect(self.reject)
        
        btn_layout.addWidget(btn_cancel)
        btn_layout.addWidget(btn_save)
        layout.addLayout(btn_layout)

    def on_save(self):
        self.result_admin = AdministrationDTO(
            order_id=self.order.id,
            planned_time=self.planned_time,
            actual_time=self.time_edit.dateTime().toPython(),
            status=self.status_combo.currentData(),
            comment=self.comment_edit.toPlainText().strip(),
            dose_given=self.order.dose_value
        )
        self.accept()
