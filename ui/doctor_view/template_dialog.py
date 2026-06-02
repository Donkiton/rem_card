from PySide6.QtWidgets import (
    QVBoxLayout, QHBoxLayout, QPushButton, QLabel, 
    QComboBox
)
from PySide6.QtCore import Qt
from ...services.prescription_engine import engine
from rem_card.ui.shared.base_dialog import BaseStyledDialog

class TemplateSelectionDialog(BaseStyledDialog):
    def __init__(self, parent=None):
        super().__init__("Выбор клинического протокола (шаблона)", parent)
        self.setFixedWidth(450)
        self.selected_template_key = None
        
        self.setup_ui_content()
        
    def setup_ui_content(self):
        from rem_card.ui.styles.theme import BG_CARD, BORDER_RADIUS_SM, BORDER_LIGHT

        engine.reload_if_changed(force_check=True)
        
        self.content_widget.setStyleSheet(f"""
            QLabel {{ font-size: 14px; font-weight: bold; color: #2c3e50; background-color: transparent; }}
            QComboBox {{ 
                background-color: {BG_CARD}; 
                border: 1px solid {BORDER_LIGHT};
                border-radius: {BORDER_RADIUS_SM};
                padding: 4px;
                color: #2c3e50;
            }}
        """)
        
        layout = self.content_layout
        layout.setSpacing(15)
        
        layout.addWidget(QLabel("Выберите шаблон для загрузки:"))
        
        self.template_combo = QComboBox()
        for k, v in engine.ordered_templates_items():
            self.template_combo.addItem(v.get("name", k), k)
        
        layout.addWidget(self.template_combo)
        layout.addStretch()
        
        # --- Кнопки ---
        btn_layout = QHBoxLayout()
        btn_cancel = QPushButton("Отмена")
        btn_cancel.setObjectName("DialogOkBtn")
        btn_cancel.clicked.connect(self.reject)
        
        self.btn_apply = QPushButton("Загрузить шаблон")
        self.btn_apply.setObjectName("DialogOkBtn")
        self.btn_apply.clicked.connect(self.apply_selection)
        
        # Если шаблонов нет
        if self.template_combo.count() == 0:
            self.btn_apply.setEnabled(False)
            self.template_combo.addItem("— Нет доступных шаблонов —")
            self.template_combo.setEnabled(False)
        
        btn_layout.addWidget(btn_cancel)
        btn_layout.addStretch()
        btn_layout.addWidget(self.btn_apply)
        layout.addLayout(btn_layout)
        
    def apply_selection(self):
        t_key = self.template_combo.currentData()
        if t_key:
            self.selected_template_key = t_key
            self.accept()
