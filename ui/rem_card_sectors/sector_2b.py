from rem_card.ui.shared.base_sector import BaseSectorWidget
from rem_card.ui.styles.sector_styles import build_remcard_tab_button_style, build_remcard_tab_frame_style
from rem_card.ui.styles.theme_manager import get_theme_manager
from PySide6.QtWidgets import QHBoxLayout, QPushButton, QWidget, QLabel
from PySide6.QtCore import Signal, Qt
from PySide6.QtGui import QPixmap
import os

class Sector2b(BaseSectorWidget):
    tab_changed = Signal(str)

    def __init__(self, parent=None):
        super().__init__("2б (Вкладки)", parent)
        self.setFrameStyle(BaseSectorWidget.NoFrame)
        self.setContentsMargins(0, 0, 0, 0)
        tokens = get_theme_manager().current_tokens()
        
        self.setStyleSheet(build_remcard_tab_frame_style(tokens))

        self.label.hide()
        # В режиме конструктора нам может понадобиться заголовок, если мы скроем контент
        # Но по ТЗ в секторе 2б должны быть кнопки переключения вкладок
        
        # Основной контейнер с минимальной высотой для вкладок (теперь может растягиваться)
        self.tabs_container = QWidget()
        self.tabs_container.setFixedHeight(36) # Фиксируем по высоте кнопок + небольшой запас
        self.tabs_container.setStyleSheet("")
        self.tabs_layout = QHBoxLayout(self.tabs_container)
        self.tabs_layout.setContentsMargins(5, 0, 5, 0) # Убираем верхний отступ
        self.tabs_layout.setSpacing(10)
        
        # Создание вкладок
        self.btn_vitals = self.create_tab_button("Витальные функции", active=True)
        self.btn_balance = self.create_tab_button("Баланс жидкости", enabled=True) # Включено обратно
        self.btn_events = self.create_tab_button("Движение", enabled=True)
        self.btn_ivl = self.create_tab_button("ИВЛ", enabled=True)
        self.btn_orders = self.create_tab_button("Назначения", enabled=True)
        self.btn_procedures = self.create_tab_button("Процедуры", enabled=True)
        self.btn_labs = self.create_tab_button("Анализы", enabled=True)
        self.btn_print = self.create_tab_button("Печать", enabled=False)
        self.btn_print.hide()
        
        # Значок сохранения (savecard.png)
        self.save_icon = QLabel()
        self.save_icon.setFixedSize(24, 24)
        from rem_card.app.paths import get_icon_dir
        icon_path = os.path.join(get_icon_dir(), "savecard.png")
        if os.path.exists(icon_path):
            self.save_icon.setPixmap(QPixmap(icon_path).scaled(24, 24, Qt.KeepAspectRatio, Qt.SmoothTransformation))
        self.save_icon.hide() # По умолчанию скрыт

        self.tabs_layout.addWidget(self.btn_vitals)
        self.tabs_layout.addWidget(self.btn_orders)
        self.tabs_layout.addWidget(self.btn_balance)
        self.tabs_layout.addWidget(self.btn_events)
        self.tabs_layout.addWidget(self.btn_ivl)
        
        # Добавляем значок рядом с Назначениями (или в конце)
        self.tabs_layout.addWidget(self.save_icon)
        
        self.tabs_layout.addWidget(self.btn_procedures)
        self.tabs_layout.addWidget(self.btn_labs)
        self.tabs_layout.addWidget(self.btn_print)
        self.tabs_layout.addStretch()
        
        self.set_content(self.tabs_container)

    def create_tab_button(self, text, active=False, enabled=True):
        btn = QPushButton(text)
        btn.setEnabled(enabled)
        btn.setCheckable(True)
        btn.setChecked(active)
        btn.setFixedHeight(32) # Увеличили высоту для соответствия стилю
        
        btn.setStyleSheet(build_remcard_tab_button_style(get_theme_manager().current_tokens()))
        
        if enabled:
            btn.clicked.connect(lambda: self.on_tab_clicked(text))
            
        return btn

    def on_tab_clicked(self, tab_name):
        self.tab_changed.emit(tab_name)
        for i in range(self.tabs_layout.count()):
            item = self.tabs_layout.itemAt(i)
            if item and item.widget():
                w = item.widget()
                if isinstance(w, QPushButton):
                    w.setChecked(w.text() == tab_name)

    def set_save_icon_visible(self, visible: bool):
        self.save_icon.setVisible(visible)
