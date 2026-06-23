import os
from PySide6.QtWidgets import QWidget, QHBoxLayout, QVBoxLayout, QPushButton
from PySide6.QtGui import QIcon
from PySide6.QtCore import QSize, Qt
from rem_card.ui.styles.theme import (COLOR_S2_SAVE_BG, COLOR_S2_SAVE_TEXT,
                                     COLOR_S2_CLEAN_SHEET_BG, COLOR_S2_CLEAN_SHEET_TEXT,
                                     COLOR_S2_CLEAR_BG, COLOR_S2_CLEAR_TEXT,
                                     COLOR_S2_YESTERDAY_BG, COLOR_S2_YESTERDAY_TEXT,
                                     COLOR_S2_ROLLBACK_BG, COLOR_S2_ROLLBACK_TEXT)

class ControlPanel(QWidget):
    """Панель управления с кнопками (Сохранить, Очистить лист, Очистить назначения, Вчерашние назначения, Отменить изменения)."""
    def __init__(self, parent=None, orientation=Qt.Horizontal):
        super().__init__(parent)
        self.orientation = orientation
        self.icon_dir = os.path.join(os.path.dirname(__file__), "..", "..", "..", "icon")
        self.icon_dir = os.path.normpath(self.icon_dir)
        self.init_ui()

    def init_ui(self):
        self.layout = QVBoxLayout(self) if self.orientation == Qt.Vertical else QHBoxLayout(self)
        self.layout.setContentsMargins(0, 0, 0, 0)
        self.layout.setSpacing(4 if self.orientation == Qt.Vertical else 8)
        button_min_h = 26 if self.orientation == Qt.Vertical else 32

        # Общий стиль для кнопок
        button_style = """
            QPushButton {
                font-size: 13px; 
                font-weight: bold; 
                padding: 4px 12px; 
                background-color: #ecf0f1; 
                color: #2c3e50; 
                border-radius: 5px; 
                border: 1.5px solid #bdc3c7;
            }
            QPushButton:hover {
                background-color: #dcdde1;
            }
            QPushButton:pressed {
                background-color: #bdc3c7;
            }
            QPushButton:disabled {
                background-color: #f1f2f6;
                color: #a4b0be;
                border: 1px solid #dcdde1;
                font-weight: normal;
            }
        """

        # 1. Кнопка Сохранить
        self.btn_save = QPushButton(" Сохранить")
        self.btn_save.setIconSize(QSize(18, 18))
        self.btn_save.setMinimumHeight(button_min_h)
        self.btn_save.setStyleSheet(button_style)

        # 2. Кнопка Очистить лист
        self.btn_clean_sheet = QPushButton(" Очистить лист")
        self.btn_clean_sheet.setIconSize(QSize(18, 18))
        self.btn_clean_sheet.setMinimumHeight(button_min_h)
        self.btn_clean_sheet.setStyleSheet(button_style)
        clean_icon = os.path.join(self.icon_dir, "clean.png")
        self.btn_clean_sheet.setIcon(QIcon(clean_icon))
        
        # 3. Кнопка Очистить назначения
        self.btn_clear = QPushButton(" Очистить назначения")
        self.btn_clear.setIconSize(QSize(18, 18))
        self.btn_clear.setMinimumHeight(button_min_h)
        self.btn_clear.setStyleSheet(button_style)
        clear_orders_icon = os.path.join(self.icon_dir, "garbage.png") # Используем иконку мусорки
        self.btn_clear.setIcon(QIcon(clear_orders_icon))

        # 4. Кнопка Вчерашние назначения
        self.btn_yesterday = QPushButton(" Вчерашние назначения")
        self.btn_yesterday.setIconSize(QSize(18, 18))
        self.btn_yesterday.setMinimumHeight(button_min_h)
        self.btn_yesterday.setStyleSheet(button_style)
        yesterday_icon = os.path.join(self.icon_dir, "yesterday.png")
        self.btn_yesterday.setIcon(QIcon(yesterday_icon))

        # 5. Кнопка Отменить изменения
        self.btn_rollback = QPushButton(" Отменить изменения")
        self.btn_rollback.setIconSize(QSize(18, 18))
        self.btn_rollback.setMinimumHeight(button_min_h)
        self.btn_rollback.setStyleSheet(button_style)
        rollback_icon = os.path.join(self.icon_dir, "otmena.png")
        self.btn_rollback.setIcon(QIcon(rollback_icon))

        # 6. Кнопка Шаблоны
        self.btn_templates = QPushButton(" Шаблоны")
        self.btn_templates.setIconSize(QSize(18, 18))
        self.btn_templates.setMinimumHeight(button_min_h)
        self.btn_templates.setStyleSheet(button_style)
        # Иконка для шаблонов (например, archivepacient или другая)
        templates_icon = os.path.join(self.icon_dir, "archivepacient.png")
        if os.path.exists(templates_icon):
            self.btn_templates.setIcon(QIcon(templates_icon))

        # 7. Кнопка Показатели
        self.btn_pokaz = QPushButton(" Показатели")
        self.btn_pokaz.setIconSize(QSize(18, 18))
        self.btn_pokaz.setMinimumHeight(button_min_h)
        self.btn_pokaz.setStyleSheet(button_style)
        pokaz_icon = os.path.join(self.icon_dir, "pokaz.png")
        if os.path.exists(pokaz_icon):
            self.btn_pokaz.setIcon(QIcon(pokaz_icon))
        self.btn_pokaz.clicked.connect(self.open_vital_settings)

        self.layout.addWidget(self.btn_save)
        self.layout.addWidget(self.btn_clean_sheet)
        self.layout.addWidget(self.btn_clear)
        self.layout.addWidget(self.btn_yesterday)
        self.layout.addWidget(self.btn_rollback)
        self.layout.addWidget(self.btn_templates)
        self.layout.addWidget(self.btn_pokaz)
        if self.orientation == Qt.Vertical:
            self.layout.addStretch(1)
        
        # По умолчанию кнопки неактивны
        self.set_save_active(False)
        self.set_clean_active(False)
        self.set_rollback_active(False)
        self.btn_templates.setEnabled(True)

    def set_save_active(self, active: bool):
        """Меняет состояние и иконку кнопки сохранения."""
        self.btn_save.setEnabled(active)
        icon_name = "savecard.png" if active else "nosave.png"
        icon_path = os.path.join(self.icon_dir, icon_name)
        self.btn_save.setIcon(QIcon(icon_path))

    def set_clean_active(self, active: bool):
        """Меняет активность кнопки Очистить лист."""
        self.btn_clean_sheet.setEnabled(active)

    def set_rollback_active(self, active: bool):
        """Меняет активность кнопки Отменить изменения."""
        self.btn_rollback.setEnabled(active)

    def set_clear_active(self, active: bool):
        """Меняет активность кнопки Очистить назначения."""
        self.btn_clear.setEnabled(active)

    def set_yesterday_active(self, active: bool):
        """Меняет активность кнопки Вчерашние назначения."""
        self.btn_yesterday.setEnabled(active)

    def set_templates_active(self, active: bool):
        """Меняет активность кнопки Шаблоны."""
        self.btn_templates.setEnabled(active)

    def open_vital_settings(self):
        """Открывает окно настроек показателей."""
        # Нам нужен доступ к сервису и текущему admission_id и date
        parent = self.parent()
        while parent:
            # DoctorRemCardWidget использует 'service', NurseMainWidget использует 'remcard_service'
            # Также проверяем наличие admission_id и _current_date
            service = getattr(parent, 'service', None) or getattr(parent, 'remcard_service', None)
            adm_id = getattr(parent, 'admission_id', None)
            curr_date = getattr(parent, '_current_date', None)

            if service and adm_id and curr_date:
                from rem_card.ui.shared.components.vital_settings_dialog import VitalSettingsDialog
                dialog = VitalSettingsDialog(
                    service, 
                    adm_id, 
                    curr_date.strftime('%Y-%m-%d'), 
                    self
                )
                if hasattr(parent, 'refresh_data'):
                    dialog.settings_saved.connect(parent.refresh_data)
                    dialog.cvp_order_changed.connect(parent.refresh_data)
                dialog.exec()
                break
            parent = parent.parent()
