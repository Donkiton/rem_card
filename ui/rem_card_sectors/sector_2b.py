from rem_card.ui.shared.base_sector import BaseSectorWidget
from rem_card.ui.shared.display_settings_storage import DisplaySettingsStorage, role_display_settings_from_payload
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
        self.tabs_container = QWidget(self)
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
        self.btn_print = self.create_tab_button("Печать", enabled=True)
        
        # Значок сохранения (savecard.png)
        self.save_icon = QLabel(self.tabs_container)
        self.save_icon.setFixedSize(24, 24)
        from rem_card.app.paths import get_icon_dir
        icon_path = os.path.join(get_icon_dir(), "savecard.png")
        if os.path.exists(icon_path):
            self.save_icon.setPixmap(QPixmap(icon_path).scaled(24, 24, Qt.KeepAspectRatio, Qt.SmoothTransformation))
        self.save_icon.hide() # По умолчанию скрыт
        self._save_icon_requested_visible = False

        self._tab_widgets = {
            "vitals": self.btn_vitals,
            "orders": self.btn_orders,
            "balance": self.btn_balance,
            "events": self.btn_events,
            "ivl": self.btn_ivl,
            "procedures": self.btn_procedures,
            "labs": self.btn_labs,
            "print": self.btn_print,
        }
        self._tab_labels = {
            "vitals": "Витальные функции",
            "orders": "Назначения",
            "balance": "Баланс жидкости",
            "events": "Движение",
            "ivl": "ИВЛ",
            "procedures": "Процедуры",
            "labs": "Анализы",
            "print": "Печать",
        }
        self._visible_tabs = {tab_id: True for tab_id in self._tab_widgets}
        self.apply_display_settings()
        
        self.set_content(self.tabs_container)

    def create_tab_button(self, text, active=False, enabled=True):
        btn = QPushButton(text, self.tabs_container)
        btn.setEnabled(enabled)
        btn.setCheckable(True)
        btn.setChecked(active)
        btn.setFixedHeight(32) # Увеличили высоту для соответствия стилю
        
        btn.setStyleSheet(build_remcard_tab_button_style(get_theme_manager().current_tokens()))
        
        if enabled:
            btn.clicked.connect(lambda: self.on_tab_clicked(text))
            
        return btn

    def _clear_tabs_layout(self):
        while self.tabs_layout.count():
            self.tabs_layout.takeAt(0)

    def _tab_id_by_name(self, tab_name: str) -> str | None:
        for tab_id, label in self._tab_labels.items():
            if label == tab_name:
                return tab_id
        return None

    def is_tab_visible(self, tab_name: str) -> bool:
        tab_id = self._tab_id_by_name(tab_name)
        return bool(tab_id and self._visible_tabs.get(tab_id, False))

    def first_visible_tab_name(self) -> str:
        for tab_id in getattr(self, "_tab_order", list(self._tab_widgets)):
            if self._visible_tabs.get(tab_id, False):
                return self._tab_labels[tab_id]
        return "Витальные функции"

    def current_tab_name(self) -> str:
        for tab_id, button in self._tab_widgets.items():
            if button.isChecked():
                return self._tab_labels[tab_id]
        return self.first_visible_tab_name()

    def apply_display_settings(self):
        previous_tab = self.current_tab_name() if hasattr(self, "_tab_widgets") else "Витальные функции"
        try:
            payload = DisplaySettingsStorage().load()
            settings = role_display_settings_from_payload(payload, "doctor")
            section = settings["remcard_tabs"]
            order = section["order"]
            visible = section["visible"]
        except Exception:
            order = list(getattr(self, "_tab_widgets", {}).keys())
            visible = {tab_id: tab_id != "print" for tab_id in order}

        self._tab_order = [tab_id for tab_id in order if tab_id in self._tab_widgets]
        self._visible_tabs = {
            tab_id: bool(visible.get(tab_id, True))
            for tab_id in self._tab_widgets
        }

        self._clear_tabs_layout()
        orders_visible = False
        for tab_id in self._tab_order:
            if not self._visible_tabs.get(tab_id, False):
                self._tab_widgets[tab_id].setVisible(False)
                continue
            button = self._tab_widgets[tab_id]
            button.setVisible(True)
            button.setEnabled(True)
            self.tabs_layout.addWidget(button)
            if tab_id == "orders":
                orders_visible = True
                self.tabs_layout.addWidget(self.save_icon)

        self.save_icon.setVisible(bool(self._save_icon_requested_visible and orders_visible))
        self.tabs_layout.addStretch()

        resolved_tab = previous_tab if self.is_tab_visible(previous_tab) else self.first_visible_tab_name()
        self.select_tab(resolved_tab, emit=resolved_tab != previous_tab)

    def select_tab(self, tab_name: str, *, emit: bool = False):
        if not self.is_tab_visible(tab_name):
            tab_name = self.first_visible_tab_name()
        for button in self._tab_widgets.values():
            button.setChecked(button.text() == tab_name)
        if emit:
            self.tab_changed.emit(tab_name)

    def on_tab_clicked(self, tab_name):
        self.select_tab(tab_name, emit=True)

    def set_save_icon_visible(self, visible: bool):
        self._save_icon_requested_visible = bool(visible)
        self.save_icon.setVisible(bool(visible and self.is_tab_visible("Назначения")))
