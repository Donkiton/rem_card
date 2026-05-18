import re
from rem_card.ui.shared.custom_message_box import CustomMessageBox
from datetime import datetime, timedelta
from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QPushButton, QLabel, 
    QLineEdit, QListWidget, QDialog, QComboBox, QMessageBox,
    QDoubleSpinBox, QSpinBox, QFormLayout, QDialogButtonBox,
    QCheckBox
)
from PySide6.QtCore import Qt, Signal
from rem_card.ui.styles.context_menu_style import (
    build_line_edit_context_menu,
    install_russian_line_edit_context_menu,
)
from ...services.prescription_engine import engine


class PrescriptionInputWidget(QWidget):
    prescription_generated = Signal(str) # Сигнал, испускаемый при готовом назначении
    
    def __init__(self, parent=None):
        super().__init__(parent)
        self.matches = []
        self.setup_ui()
        
    def focusOutEvent(self, event):
        self.list_widget.hide()
        super().focusOutEvent(event)
        
    def hide_list(self):
        if not self.list_widget.isHidden():
            self.list_widget.hide()
        
    def setup_ui(self):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        
        # Строка поиска/ввода
        self.input_field = QLineEdit()
        self.input_field.setPlaceholderText("Введите препарат...")
        self.input_field.setStyleSheet("""
            QLineEdit {
                font-size: 14px; 
                padding: 5px;
                border: 1px solid #ced4da;
                border-radius: 4px;
            }
            QLineEdit:focus {
                border: 2px solid #bdc4c8; 
                outline: none;
            }
        """)
        layout.addWidget(self.input_field)
        
        # Используем QListWidget как дочернее окно, позиционируемое абсолютно
        self.list_widget = QListWidget(self)
        self.list_widget.setWindowFlags(Qt.ToolTip | Qt.FramelessWindowHint)
        self.list_widget.setFocusPolicy(Qt.NoFocus)
        self.list_widget.setStyleSheet("border: 1px solid gray; background-color: white; font-size: 14px;")
        self.list_widget.hide()
        
        # Подключения
        self.input_field.textChanged.connect(self.on_text_changed)
        self.input_field.returnPressed.connect(self.on_enter)
        install_russian_line_edit_context_menu(self.input_field)
        self.list_widget.itemClicked.connect(self.on_item_clicked)
        
        # Переопределяем event filter для QLineEdit чтобы скрывать список при потере фокуса
        self.input_field.installEventFilter(self)

    def _build_input_context_menu(self):
        return build_line_edit_context_menu(self.input_field)

    def eventFilter(self, obj, event):
        from PySide6.QtCore import QEvent
        if obj is self.input_field and event.type() == QEvent.FocusOut:
            # Даем небольшую задержку, чтобы клик по QListWidget успел обработаться
            from PySide6.QtCore import QTimer
            QTimer.singleShot(100, self.hide_list)
        return super().eventFilter(obj, event)
        
    def on_text_changed(self, text):
        if not text:
            self.list_widget.hide()
            return
            
        self.list_widget.clear()
        self.matches = []
        
        text_lower = text.lower().strip()
        
        # Проверяем * (star_alias)
        if "*" in text_lower:
            # Звезда обрабатывается только по Enter, скрываем автокомплит
            self.list_widget.hide()
            return
            
        # Ищем совпадения по алиасам и латыни
        for key, data in engine.drugs.items():
            latin = data.get("latin", "").lower()
            aliases = [a.lower() for a in data.get("aliases", [])]
            
            if text_lower in latin or any(text_lower in a for a in aliases):
                self.matches.append((key, data))
                # Выводим красивое название
                display_name = data.get("latin", key)
                admin_type = data.get("admin_type", "")
                
                type_map = {
                    "bolus": " (болюс)",
                    "infusion": " (инфузия)",
                    "continuous": " (доз.)",
                    "per_os": " (per os)",
                    "per_rectum": " (per rectum)"
                }
                type_str = type_map.get(admin_type, "")
                
                self.list_widget.addItem(f"{display_name}{type_str}")
                
        if self.matches:
            # Позиционируем прямо под полем ввода (в глобальных координатах)
            pos = self.input_field.mapToGlobal(self.input_field.rect().bottomLeft())
            self.list_widget.setGeometry(pos.x(), pos.y(), self.input_field.width(), 150)
            self.list_widget.show()
        else:
            self.list_widget.hide()
            
    def on_item_clicked(self):
        idx = self.list_widget.currentRow()
        if 0 <= idx < len(self.matches):
            key, _ = self.matches[idx]
            # Важно: передаем ключ препарата отдельно, чтобы не полагаться на текст в поле
            self.process_prescription(key, self.input_field.text())
            
    def on_enter(self):
        text = self.input_field.text().strip()
        if not text:
            return
            
        # 1. Проверяем звездочку
        if "*" in text:
            drug_key, drug_data = engine.detect_star_drug(text)
            if drug_key:
                self.process_prescription(drug_key, text, use_default=True)
                return
                
        # 2. Если выбран элемент из списка
        if not self.list_widget.isHidden() and self.list_widget.currentRow() >= 0:
            self.on_item_clicked()
            return
            
        # 3. Если просто ввели текст, берем первое совпадение
        if self.matches:
            key, _ = self.matches[0]
            self.process_prescription(key, text)
            
    def process_prescription(self, drug_key, raw_text, use_default=False):
        from .administration_dialog import ManualEntryDialog, DrugCharacteristicsDialog
        # Специальная обработка ручного ввода
        if drug_key.lower() in ("ruchnoivvod", "ruki"):
            dlg = ManualEntryDialog(self)
            if dlg.exec():
                self.finish_prescription(dlg.result_text)
            return

        # 1. Парсим дозу из введенного текста (на случай, если она там есть)
        dose = engine.parse_dose(raw_text)
        drug_data = engine.drugs.get(drug_key, {})
        
        if not drug_data:
             CustomMessageBox.warning(self, "Ошибка", "Препарат не найден в базе!")
             return

        # 2. Если это быстрый ввод (звездочка), используем дефолты сразу
        if use_default:
            presc_text = f"{drug_key} {dose if dose is not None else drug_data.get('default_dose', 0)}"
            res = engine.build_prescription(presc_text, dilution_choice=drug_data.get("default_dilution"))
            if "result" in res:
                self.finish_prescription(res["result"])
            return

        # 3. Для обычного выбора открываем окно "Выберите характеристики препарата"
        from .administration_dialog import MultiCompCharacteristicsDialog
        
        if drug_data.get("is_multicomp"):
            dlg = MultiCompCharacteristicsDialog(drug_key, parent=self)
        else:
            dlg = DrugCharacteristicsDialog(drug_key, initial_dose=dose, parent=self)
            
        if dlg.exec():
            # Новый диалог возвращает уже готовую строку в result_text (как ManualEntryDialog)
            self.finish_prescription(dlg.result_text)
            
    def finish_prescription(self, result_text):
        self.input_field.clear()
        self.list_widget.hide()
        self.prescription_generated.emit(result_text)
