from PySide6.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QLabel, QScrollArea, QWidget, QFrame, QPushButton
)
from PySide6.QtCore import Qt
from datetime import datetime, timedelta
import re
import math

from rem_card.services.prescription_engine import engine
from rem_card.services.order_domain_service import NURSE_MARK_EXECUTED

class PatientStatCard(QFrame):
    def __init__(self, patient_name, history_number, current_hour_data, total_day_data):
        super().__init__()
        self.setObjectName("patientStatCard")
        self.setStyleSheet("""
            QFrame#patientStatCard {
                background-color: #ffffff;
                border: 1px solid #ced4da;
                border-radius: 8px;
                margin-bottom: 10px;
            }
            QLabel { background: transparent; }
        """)
        
        layout = QVBoxLayout(self)
        layout.setContentsMargins(15, 15, 15, 15)
        
        header = QLabel(f"<b>№{history_number}</b> | {patient_name}")
        header.setStyleSheet("font-size: 14px; color: #2c3e50;")
        layout.addWidget(header)
        
        content_layout = QHBoxLayout()
        curr_hour_widget = self.create_data_block("Текущий час (выполнено)", current_hour_data)
        content_layout.addWidget(curr_hour_widget)
        
        line = QFrame()
        line.setFrameShape(QFrame.VLine)
        line.setFrameShadow(QFrame.Sunken)
        line.setStyleSheet("color: #ced4da;")
        content_layout.addWidget(line)
        
        total_day_widget = self.create_data_block("На конец суток (ожидаемый расход)", total_day_data)
        content_layout.addWidget(total_day_widget)
        
        layout.addLayout(content_layout)
        
    def create_data_block(self, title, data):
        w = QWidget()
        l = QVBoxLayout(w)
        l.setContentsMargins(0,0,0,0)
        
        lbl_title = QLabel(f"<b>{title}</b>")
        lbl_title.setStyleSheet("color: #495057;")
        l.addWidget(lbl_title)
        
        if not data:
            lbl_empty = QLabel("Нет расхода")
            lbl_empty.setStyleSheet("color: #adb5bd; font-style: italic;")
            l.addWidget(lbl_empty)
        else:
            text = ""
            for item in data:
                text += f"• {item}\n"
            lbl_data = QLabel(text.strip())
            lbl_data.setStyleSheet("color: #212529;")
            l.addWidget(lbl_data)
            
        l.addStretch()
        return w

from ..shared.custom_title_bar import CustomTitleBar
from ..styles.theme import BG_MAIN, CUSTOM_DIALOG_RADIUS

class NurseStatisticsDialog(QDialog):
    def __init__(self, patient_service, remcard_service, parent=None):
        super().__init__(parent)
        self.patient_service = patient_service
        self.remcard_service = remcard_service
        
        self.setWindowTitle("Отчет по препаратам")
        self.setWindowFlags(Qt.FramelessWindowHint | Qt.WindowMinMaxButtonsHint | Qt.Dialog)
        self.setAttribute(Qt.WA_TranslucentBackground)
        
        self.setMinimumSize(900, 700)
        
        self.current_hour_dt = datetime.now()
        self.setup_ui()
        self.load_data()

    def setup_ui(self):
        # Основной контейнер с рамкой и скруглением как в программе
        self.main_frame = QFrame(self)
        self.main_frame.setObjectName("MainFrame")
        self.main_frame.setStyleSheet(f"""
            QFrame#MainFrame {{ 
                background-color: {BG_MAIN}; 
                border: 1.5px solid #bdc3c7; 
                border-radius: {CUSTOM_DIALOG_RADIUS}; 
            }}
        """)
        
        outer_layout = QVBoxLayout(self)
        outer_layout.setContentsMargins(0, 0, 0, 0)
        outer_layout.addWidget(self.main_frame)

        main_layout = QVBoxLayout(self.main_frame)
        main_layout.setContentsMargins(0, 0, 0, 10)
        main_layout.setSpacing(0)
        
        # Кастомный заголовок окна
        self.title_bar = CustomTitleBar(self)
        main_layout.addWidget(self.title_bar)
        
        # Контентная область
        content_wrapper = QVBoxLayout()
        content_wrapper.setContentsMargins(15, 10, 15, 0)
        main_layout.addLayout(content_wrapper)
        
        # Заголовок внутри отчета
        date_str = self.current_hour_dt.strftime("%d.%m.%Y")
        lbl_title = QLabel(f"Отчет по препаратам на {date_str}")
        lbl_title.setStyleSheet("font-size: 20px; font-weight: bold; color: #5c6770; margin-bottom: 5px;")
        content_wrapper.addWidget(lbl_title)
        
        self.scroll = QScrollArea()
        self.scroll.setWidgetResizable(True)
        self.scroll.setStyleSheet("QScrollArea { border: none; background: transparent; }")
        
        self.content_widget = QWidget()
        self.content_widget.setStyleSheet("background: transparent;")
        self.content_layout = QVBoxLayout(self.content_widget)
        self.content_layout.setAlignment(Qt.AlignTop)
        
        self.scroll.setWidget(self.content_widget)
        main_layout.addWidget(self.scroll)
        
        btn_layout = QHBoxLayout()
        btn_layout.addStretch()
        btn_close = QPushButton("Закрыть")
        btn_close.setFixedWidth(120)
        btn_close.setStyleSheet("QPushButton { background-color: #6c757d; color: white; border-radius: 4px; padding: 6px; } QPushButton:hover { background-color: #5a6268; }")
        btn_close.clicked.connect(self.close)
        btn_layout.addWidget(btn_close)
        btn_layout.addSpacing(10) # Сдвиг влево от правого края на 10 пикселей
        main_layout.addLayout(btn_layout)

    def load_data(self):
        start_dt, end_dt = self.remcard_service.get_day_period(self.current_hour_dt)
        active_patients = self.patient_service.get_active_patients()
        
        patient_ids = [p.id for p in active_patients]
        rows = []
        if patient_ids:
            rows = self.remcard_service.get_nurse_statistics_rows(patient_ids)
        
        patient_data = {p.id: {"name": p.get_display_name(), "history": p.history_number, 
                               "current_hour": {"drugs": {}}, "total": {"drugs": {}}} for p in active_patients}
        
        diluent_keys = ['fizrastvor', 'glucozka5', 'glucozka10', 'Ringersol', 'ringer', 'glucose_5', 'glucose_10', 'nacl_09']

        for row in rows:
            adm_id = row['admission_id']
            if adm_id not in patient_data: continue
                
            if row['comment'] == 'nurse_not_executed' or row['admin_status'] in ['cancelled', 'not_executed']:
                continue
                
            if row['cell_role'] not in ['start', 'single']:
                continue

            dt_str = row['actual_time'] or row['planned_time']
            try:
                dt = datetime.fromisoformat(dt_str.replace(" ", "T")).replace(tzinfo=None)
            except: continue
                
            if not (start_dt <= dt < end_dt): continue
            
            drug_key = row['drug_key']
            text = (row['text'] or "").lower()
            latin = row['latin'] or "Неизвестный препарат"
            dose = float(row['dose_value'] or 0)
            unit = row['dose_unit'] or ""
            
            if drug_key in diluent_keys: continue
            if any(x in latin.lower() or x in text for x in ['nacl', 'natrii', 'натрия', 'физ', 'glucose', 'glucosae', 'глюкоза', 'ringer', 'рингер']):
                continue
            
            drug_info = engine.drugs.get(drug_key, {})
            form_key = drug_info.get("form_key")
            if form_key in ["drops", "ointment", "comp_blood"]: continue

            is_executed = (row['comment'] == NURSE_MARK_EXECUTED or row['admin_status'] == 'executed')
            
            def add_agg(agg_dict, key, lat, ds, un):
                # Очищаем latin от возможных префиксов форм (например, "S. ", "Tab. ") для надежного сопоставления
                clean_lat = re.sub(r'^[A-Za-z]+\.\s*', '', lat.strip()).strip()
                
                # Пытаемся разрешить ручной ввод в реальный ключ словаря
                if key == 'ruchnoivvod':
                    for k, v in engine.drugs.items():
                        if v.get('latin', '').strip().lower() == clean_lat.lower():
                            key = k
                            break
                            
                # Берем эталонное латинское название из справочника, если препарат там есть
                di = engine.drugs.get(key, {})
                if di and 'latin' in di:
                    lat = di['latin']
                else:
                    lat = clean_lat

                # Приводим единицу измерения к нижнему регистру для однообразия
                un_clean = un.strip().lower()
                # Для ЕД (IU) стандартизируем, чтобы не плодить дубликаты
                if un_clean in ["iu", "ед", "ed"]:
                    un_clean = "ЕД"
                else:
                    un_clean = un.strip()

                ak = (key, lat)
                if ak not in agg_dict["drugs"]:
                    agg_dict["drugs"][ak] = {"latin": lat, "drug_key": key, "dose": 0, "unit": un_clean, "count": 0}
                agg_dict["drugs"][ak]["dose"] += ds
                agg_dict["drugs"][ak]["count"] += 1

            if drug_info.get("is_multicomp"):
                parts = str(latin).split("+")
                for part in parts:
                    part = part.strip()
                    if not part: continue
                    # Пропускаем растворители
                    if any(x in part.lower() for x in ['nacl', 'natrii', 'натрия', 'физ', 'glucose', 'glucosae', 'глюкоза', 'ringer', 'рингер']):
                        continue
                        
                    # Извлекаем все до тире как название, затем дозу и единицы
                    m = re.search(r'(.*?)\s*-\s*(\d+\.?\d*)\s*([A-Za-zА-Яа-я/]+)$', part)
                    if m:
                        raw_name = m.group(1).strip()
                        c_dose = float(m.group(2))
                        c_unit = m.group(3).strip()
                        
                        # Аккуратно отрезаем известные префиксы (S., T. и т.д.), чтобы не повредить само название (например, Vit. C)
                        for f_info in engine.forms.values():
                            pref = f_info.get("latin_abbr", "").strip()
                            if pref and raw_name.startswith(pref + " "):
                                raw_name = raw_name[len(pref):].strip()
                                break
                        
                        c_lat = raw_name
                        c_key = "ruchnoivvod"
                        for k, v in engine.drugs.items():
                            if v.get("latin", "").lower() == c_lat.lower():
                                c_key = k
                                break
                                
                        add_agg(patient_data[adm_id]["total"], c_key, c_lat, c_dose, c_unit)
                        if is_executed:
                            add_agg(patient_data[adm_id]["current_hour"], c_key, c_lat, c_dose, c_unit)
            else:
                add_agg(patient_data[adm_id]["total"], drug_key, latin, dose, unit)
                if is_executed:
                    add_agg(patient_data[adm_id]["current_hour"], drug_key, latin, dose, unit)

        def build_res(agg_dict):
            res = []
            for (dk, lat), info in agg_dict["drugs"].items():
                t_dose, un = info["dose"], info["unit"]
                di = engine.drugs.get(dk, {})
                if dk.lower() == 'ruchnoivvod':
                    for k, v in engine.drugs.items():
                        if v.get('latin') == lat: di = v; break
                
                forms = di.get("forms", [])
                fk = di.get("form_key", "")
                
                # Если форма не указана, но это антибиотик в граммах/мг - скорее всего флакон
                if not fk and di.get("group") == "antibiotics" and un.lower() in ["g", "г", "mg", "мг"]:
                    fk = "lyophilisate"
                    
                ul = "ед."
                if fk == "lyophilisate": ul = "флак."
                elif "solution" in str(fk): ul = "амп."
                elif fk == "tablet": ul = "таб."
                elif fk == "capsule": ul = "капс."
                elif fk == "suppository": ul = "св."
                
                # Формируем префикс для вывода в статистику
                prefix = ""
                if dk.lower() != 'ruchnoivvod' and not re.match(r'^[A-Za-z]+\. ', lat.strip()):
                    form_info = engine.forms.get(fk, {})
                    pref_str = form_info.get("latin_abbr", "").strip()
                    if pref_str:
                        if not pref_str.endswith("."):
                            pref_str += "."
                        prefix = pref_str + " "
                
                # Расчет количества (ампул, таблеток и т.д.)
                cnt = info["count"]
                if forms:
                    min_form = min(forms)
                    if min_form > 0:
                        # Проверяем наличие концентрации (мг/мл) для растворов
                        conc = di.get("concentration_mg_ml", 0)
                        form_unit = di.get("form_unit", "").lower()
                        dose_unit = un.lower()
                        
                        # Если доза в мг/г/мкг, а форма выпуска в мл, и есть концентрация
                        is_weight = dose_unit in ["mg", "мг", "g", "г", "mcg", "мкг"]
                        if conc > 0 and is_weight and form_unit in ["ml", "мл"]:
                            # Приводим дозу к мг для расчета, если она в граммах или мкг
                            dose_mg = t_dose
                            if dose_unit in ["g", "г"]: dose_mg = t_dose * 1000
                            elif dose_unit in ["mcg", "мкг"]: dose_mg = t_dose / 1000
                            
                            mg_in_vial = min_form * conc
                            cnt = math.ceil(round(dose_mg, 4) / mg_in_vial)
                        else:
                            # Стандартный расчет (доза / объем формы)
                            cnt = math.ceil(round(t_dose, 4) / min_form)
                
                if un.lower() in ["iu", "ед", "ed"]:
                    res.append(f"{prefix}{lat}: {t_dose:g} ЕД ({cnt} {ul})")
                else:
                    res.append(f"{prefix}{lat}: {t_dose:g} {un} ({cnt} {ul})")
            return res

        for p_id, p_data in patient_data.items():
            card = PatientStatCard(p_data["name"], p_data["history"], build_res(p_data["current_hour"]), build_res(p_data["total"]))
            self.content_layout.addWidget(card)
