"""Maintenance controls embedded in the existing Control Center navigation."""
from PySide6.QtWidgets import QLabel, QPushButton

from rem_card.ui.shared.base_dialog import BaseStyledDialog
from rem_card.ui.admin_view.dictionary_page_chrome import prepare_embedded_settings_page


class UnifiedMaintenancePage(BaseStyledDialog):
    def __init__(self, controller, parent=None):
        super().__init__('Технические работы', parent)
        self.status_label = QLabel('Проверка состояния…')
        self.status_label.setWordWrap(True)
        self.content_layout.addWidget(self.status_label)
        self.admin_label = QLabel()
        self.admin_label.setWordWrap(True)
        self.content_layout.addWidget(self.admin_label)
        self.admin_button = QPushButton()
        self.admin_button.clicked.connect(controller._toggle_local_administrator)
        self.content_layout.addWidget(self.admin_button)
        note = QLabel('Доступ администратора сохраняется только на этом ПК, для текущего пользователя, '
                      'папки программы и её версии. Он разрешает вход в роли во время техработ. '
                      'Открытая роль администратора остаётся активным подключением к базе.')
        note.setWordWrap(True)
        self.content_layout.addWidget(note)
        self.maintenance_button = QPushButton('Включить технические работы')
        self.maintenance_button.clicked.connect(controller._toggle_maintenance)
        self.content_layout.addWidget(self.maintenance_button)
        self.content_layout.addStretch()
        prepare_embedded_settings_page(self, title='Технические работы',
            description='Предупреждение рабочих мест, ограничение входа и локальный доступ администратора.')
