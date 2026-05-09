from PySide6.QtWidgets import (QWidget, QVBoxLayout, QHBoxLayout, QSplitter, QFrame, QStackedWidget, QApplication, QSizePolicy, QLabel)
from PySide6.QtCore import Qt, QTimer, Signal
from rem_card.app.logger import logger
from rem_card.ui.shared.display_settings_storage import (
    DisplaySettingsStorage,
    w1a_upcoming_orders_enabled,
    w1b_lower_sector_enabled,
)
from .layout_components import CurrentPageStack, SectorFactory, SplitterManager

class RemCardLayoutManager(QWidget):
    selection_mode_changed = Signal(str)

    def __init__(self, role="Врач", patient_service=None, remcard_service=None, parent=None):
        super().__init__(parent)
        self.role = role
        self.patient_service = patient_service
        self.remcard_service = remcard_service
        self.current_mode = "normal"
        self._archive_last_change_id = -1
        self._first_card_mode_switch_done = False
        self._events_status_handlers = []
        self._events_patient_context = None
        self._events_shift_context = None
        self._current_status_dto = None
        
        # Единый таймер для стабилизации размеров (предотвращает вылеты)
        self._fix_timer = QTimer(self)
        self._fix_timer.setSingleShot(True)
        self._fix_timer.timeout.connect(self._post_restore_fix)
        
        self.init_ui()

    def init_ui(self):
        self.main_layout = QVBoxLayout(self)
        self.main_layout.setContentsMargins(0, 2, 0, 0)
        self.main_layout.setSpacing(0)

        # 1. Создание секторов
        sectors = SectorFactory.create_all_sectors(
            include_optional_tabs=False,
            role_hint="doctor",
            include_balance_sections=False,
        )
            
        for name, instance in sectors.items():
            setattr(self, name, instance)

        # 2. Сборка структурных компонентов
        # Правая колонка баланса (3a, 3b, 4a)
        self.sector_3_4_container = SplitterManager.create_splitter(Qt.Vertical)
        self.sector_3_4_container.addWidget(self.sector_3a)
        self.sector_3_4_container.addWidget(self.sector_3b)
        self.sector_3_4_container.addWidget(self.sector_4a)
        self.sector_3_4_container.setChildrenCollapsible(False)
        self.sector_3_4_container.setFixedHeight(186 + 204 + 65)

        self.sector_3_4_spacer = QWidget()
        self.sector_3_4_wrapper = QWidget()
        # Фиксируем ширину 230 пикселей для всех вкладок
        self.sector_3_4_wrapper.setFixedWidth(230) 
        wrapper_layout = QVBoxLayout(self.sector_3_4_wrapper)
        # Отступ 2px справа для видимости рамки
        wrapper_layout.setContentsMargins(4, 3, 2, 4) 
        wrapper_layout.setSpacing(0)
        wrapper_layout.addWidget(self.sector_3_4_container)
        wrapper_layout.addWidget(self.sector_3_4_spacer)
        wrapper_layout.addWidget(self.sector_7na_b)

        # Центр: Стек для разных вкладок
        self.vitals_stack = QStackedWidget()
        
        # Вкладка Витальные функции
        self.vitals_splitter = SplitterManager.create_splitter(Qt.Horizontal)
        self.vitals_splitter.addWidget(self.sector_2g)
        self.vitals_splitter.addWidget(self.sector_2v)
        self.vitals_stack.addWidget(self.vitals_splitter)
        
        # Вкладка Баланс (Новая структура)
        self.balance_tab_widget = QWidget()
        self.balance_tab_layout = QVBoxLayout(self.balance_tab_widget)
        self.balance_tab_layout.setContentsMargins(0, 0, 0, 0)
        self.balance_tab_layout.setSpacing(2)
        self.balance_top_splitter = None
        self._balance_grid_wrapper = None
        self._balance_tab_initialized = False

        # Вкладка Назначения создаётся лениво: на W1 она не нужна, а импорт
        # doctor orders тянет заметную часть startup.
        self._orders_tab_placeholder = QWidget()
        self.vitals_stack.addWidget(self._orders_tab_placeholder)

        self.vitals_stack.addWidget(self.balance_tab_widget)

        # Вкладка События (ленивая инициализация содержимого)
        self.events_tab_widget = QWidget()
        self._events_layout = QVBoxLayout(self.events_tab_widget)
        self._events_layout.setContentsMargins(0, 0, 0, 0)
        self.sector_events = None
        self.vitals_stack.addWidget(self.events_tab_widget)

        # Вкладка ИВЛ (ленивая инициализация содержимого)
        self.ivl_tab_widget = QWidget()
        self._ivl_layout = QVBoxLayout(self.ivl_tab_widget)
        self._ivl_layout.setContentsMargins(0, 0, 0, 0)
        self._ivl_initialized = False
        self.vitals_stack.addWidget(self.ivl_tab_widget)

        # Вкладка Процедуры (ленивая инициализация содержимого)
        self.proc_tab_widget = QWidget()
        self._proc_layout = QVBoxLayout(self.proc_tab_widget)
        self._proc_layout.setContentsMargins(0, 0, 0, 0)
        self._proc_initialized = False
        self.vitals_stack.addWidget(self.proc_tab_widget)

        # Вкладка Анализы (ленивая инициализация содержимого)
        self.anal_tab_widget = QWidget()
        self._anal_layout = QVBoxLayout(self.anal_tab_widget)
        self._anal_layout.setContentsMargins(0, 0, 0, 0)
        self._anal_initialized = False
        self.vitals_stack.addWidget(self.anal_tab_widget)

        # Вкладка Печать (ленивая инициализация содержимого)
        self.print_tab_widget = QWidget()
        self._print_layout = QVBoxLayout(self.print_tab_widget)
        self._print_layout.setContentsMargins(0, 0, 0, 0)
        self._print_initialized = False
        self.vitals_stack.addWidget(self.print_tab_widget)

        # Основной ряд (Центр + Правая колонка)
        self.mid_row = SplitterManager.create_splitter(Qt.Horizontal)
        self.mid_row.addWidget(self.vitals_stack)
        self.mid_row.addWidget(self.sector_3_4_wrapper) 
        self.mid_row.setStretchFactor(0, 1)
        self.mid_row.setStretchFactor(1, 0)
        self.mid_row.setChildrenCollapsible(False)

        # Нижний ряд
        target_h = 160
        self.sector_5.setFixedHeight(target_h)
        self.sector_6.setFixedHeight(target_h)
        self.sector_7vit_a.setFixedHeight(target_h)
        self.sector_7vit_b.setFixedHeight(target_h)
        self.sector_7bal_a.setFixedHeight(target_h)
        self.sector_7bal_b.setFixedHeight(target_h)
        
        self.sector_7a_stack = QStackedWidget()
        self.sector_7a_stack.addWidget(self.sector_7vit_a)
        self.sector_7a_stack.addWidget(self.sector_7bal_a)
        
        self.sector_7b_stack = QStackedWidget()
        # Ширина 230 пикселей для идеального выравнивания с верхней колонкой
        self.sector_7b_stack.setFixedWidth(230) 
        self.sector_7b_stack.addWidget(self.sector_7vit_b)
        self.sector_7b_stack.addWidget(self.sector_7bal_b)
        
        self.bottom_row = SplitterManager.create_splitter(Qt.Horizontal)
        self.bottom_row.setFixedHeight(target_h)
        self.bottom_row.addWidget(self.sector_5)
        self.bottom_row.addWidget(self.sector_6)
        self.bottom_row.addWidget(self.sector_7a_stack)
        self.bottom_row.addWidget(self.sector_7b_stack)
        self.bottom_row.setStretchFactor(0, 1)
        self.bottom_row.setStretchFactor(1, 1)
        self.bottom_row.setStretchFactor(2, 1)
        self.bottom_row.setStretchFactor(3, 0)

        # Вертикальная сборка контента
        self.content_splitter = SplitterManager.create_splitter(Qt.Vertical)
        self.content_splitter.addWidget(self.mid_row)
        self.content_splitter.addWidget(self.bottom_row)
        self.content_splitter.setChildrenCollapsible(False)

        # Левая колонка и правая область.
        # Собираем 4б+4в так же, как W1-строку: без скрытого splitter-pass и зазоров.
        self.sector_4b.main_layout_v.setContentsMargins(0, 0, 0, 0)
        self.sector_4b.content_layout.setContentsMargins(0, 0, 0, 0)
        self.sector_4v.main_layout_v.setContentsMargins(0, 0, 0, 0)
        self.sector_4v.content_layout.setContentsMargins(7, 3, 10, 3)
        self.sector_4_container = QWidget()
        sector_4_layout = QVBoxLayout(self.sector_4_container)
        sector_4_layout.setContentsMargins(0, 0, 0, 0)
        sector_4_layout.setSpacing(0)
        sector_4_layout.addWidget(self.sector_4b)
        sector_4_layout.addWidget(self.sector_4v)
        self.sector_4_container.setFixedHeight(98)
        self.sector_4_container.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        
        self.tab_splitter = SplitterManager.create_splitter(Qt.Vertical)
        # self.sector_2b выносим в общий контейнер с 2a
        self.tab_splitter.addWidget(self.content_splitter)

        # Объединяем сектор 2a и 2b в один контейнер без разделителя
        self.sector_2_wrapper = QWidget()
        s2_layout = QVBoxLayout(self.sector_2_wrapper)
        s2_layout.setContentsMargins(0, 0, 0, 0)
        s2_layout.setSpacing(0)
        s2_layout.addWidget(self.sector_2a)
        s2_layout.addWidget(self.sector_2b)

        # Оригинальная правая область (Карта: W + E)
        self.right_area = QWidget()
        right_layout = QVBoxLayout(self.right_area)
        # W1 первая строка имеет верхний отступ 5px от сектора 8; карта должна
        # начинаться с тем же визуальным зазором.
        right_layout.setContentsMargins(0, 5, 0, 0)
        right_layout.setSpacing(0)
        
        # Используем обычный QVBoxLayout вместо сплиттера для устранения микро-зазоров между шапкой и контентом
        right_layout.addWidget(self.sector_4_container)
        right_layout.addSpacing(5)
        right_layout.addWidget(self.sector_2_wrapper)
        right_layout.addWidget(self.tab_splitter)
        
        # --- Режим выбора пациента (W1) ---
        self.selection_stack = QStackedWidget()
        self.selection_stack.setObjectName("selection_stack")
        
        # 0. Слой карты (W + E)
        self.selection_stack.addWidget(self.right_area)
        
        # 1. Слой списка коек (W1)
        self.beds_view = QWidget()
        beds_layout = QVBoxLayout(self.beds_view)
        beds_layout.setContentsMargins(0, 0, 0, 0)
        from ...app.logger import logger
        logger.debug(f"RemCardLayoutManager init_ui: patient_service={self.patient_service}")
        if self.patient_service:
            # Передаем оба сервиса в BedsSelectionWidget
            # remcard_service может прийти как аргумент init или быть установлен позже
            from ..doctor_view.components.beds_selection_widget import BedsSelectionWidget
            self.beds_selection_widget = BedsSelectionWidget(self.patient_service, self.remcard_service)
            beds_layout.addWidget(self.beds_selection_widget)
        else:
            logger.warning("RemCardLayoutManager init_ui: NO patient_service PROVIDED!")
        self.selection_stack.addWidget(self.beds_view)
            
        # 2. Слой общего архива
        self.archive_view = QWidget()
        self._archive_layout = QVBoxLayout(self.archive_view)
        self._archive_layout.setContentsMargins(0, 0, 0, 0)
        self.archive_widget = None
        self.selection_stack.addWidget(self.archive_view)
            
        # 3. Слой админ-панели
        self.admin_view = QWidget()
        self._admin_layout = QVBoxLayout(self.admin_view)
        self._admin_layout.setContentsMargins(0, 0, 0, 0)
        self.admin_widget = None
        self.selection_stack.addWidget(self.admin_view)

        # 4. Слой встроенного журнала
        self.journal_view = QWidget()
        self._journal_layout = QVBoxLayout(self.journal_view)
        self._journal_layout.setContentsMargins(0, 0, 0, 0)
        self.journal_widget = None
        self.selection_stack.addWidget(self.journal_view)

        self.lower_area = SplitterManager.create_splitter(Qt.Horizontal)
        self.left_column = QWidget()
        self.left_column.setFixedWidth(250)
        self.l_layout = QVBoxLayout(self.left_column)
        self.l_layout.setContentsMargins(0, 0, 0, 0)
        self.l_layout.setSpacing(0)
        
        # Контейнер для 1а / W1а (как у медсестры)
        self.sector_1a_stack = CurrentPageStack()
        from ..rem_card_sectors.sector_w1a import SectorW1a
        from ..rem_card_sectors.sector_w1c import SectorW1c
        self.sector_w1a = SectorW1a(self.remcard_service, role="doctor")
        self.sector_w1c = SectorW1c()
        
        self.sector_1a_stack.addWidget(self.sector_1a)  # index 0: 1а (карта)
        self.sector_1a_stack.addWidget(self.sector_w1a) # index 1: W1а (койки)
        self.sector_1a_stack.addWidget(self.sector_w1c) # index 2: W1c (пустая рамка)
        
        self.l_layout.addWidget(self.sector_1a_stack, 1)
        
        # Контейнер для 1б / W1b
        self.sector_1b_stack = CurrentPageStack()
        self.sector_1b_stack.addWidget(self.sector_1b)  # index 0: 1б (карта)
        self.sector_1b_stack.addWidget(self.sector_w1b) # index 1: W1b (койки)
        
        self.l_layout.addWidget(self.sector_1b_stack, 0)
        
        self.lower_area.addWidget(self.left_column)
        self.lower_area.addWidget(self.selection_stack)

        self.v_splitter_top = SplitterManager.create_splitter(Qt.Vertical)
        self.v_splitter_top.addWidget(self.sector_8)
        self.v_splitter_top.addWidget(self.lower_area)

        self.main_layout.addWidget(self.v_splitter_top)

        # Устанавливаем начальный режим (выбор коек) сразу, чтобы не было "вспышки" секторов карты
        self.selection_stack.setCurrentIndex(1)
        self.sector_1b_stack.setCurrentIndex(1)
        self.current_mode = "beds"
        self._apply_w1_beds_sector_visibility(refresh_w1a=False)
        QTimer.singleShot(0, lambda: self.selection_mode_changed.emit("beds"))
        QTimer.singleShot(0, self._refresh_beds_async)

        # Ленивая инициализация менеджера 1а/5 только при открытии карты.
        self.nurse_orders_manager = None

        # Настройка сигналов
        self.sector_2b.tab_changed.connect(self.set_active_tab)

        # Безопасный запуск логики через таймер
        QTimer.singleShot(100, self._safe_init)

    def _safe_init(self):
        """Инициализация без риска удаления C++ объектов."""
        try:
            self.vitals_stack.setCurrentIndex(0)
            self.sector_7a_stack.setCurrentIndex(0)
            self.sector_7b_stack.setCurrentIndex(0)
            self.apply_splitter_locking()
            self._fix_timer.start(200)
        except: pass

    def apply_splitter_locking(self):
        SplitterManager.apply_locking(self)
        try:
            # right_splitter теперь управляет правой колонкой, а не right_area
            if hasattr(self, 'right_splitter'):
                h = self.right_splitter.handle(2)
                if h: h.setVisible(False)
        except: pass

    def _post_restore_fix(self):
        try:
            # Установка пропорций сплиттеров (только если мы в режиме карты)
            if self.selection_stack.currentIndex() == 0:
                # Фиксируем левую колонку: 250px для секторов 1a/1b
                if hasattr(self, 'lower_area'):
                    self.lower_area.setSizes([250, self.width() - 250])
                    
                self.mid_row.setSizes([self.width() - 250 - 230, 230])
                self.sector_3_4_container.setSizes([186, 204, 65])
                self.content_splitter.setSizes([self.height() - 160, 160])
                bottom_available = max(0, self.width() - 250 - 230)
                bottom_equal = max(1, bottom_available // 3)
                self.bottom_row.setSizes([bottom_equal, bottom_equal, bottom_equal, 230])
                
                # Принудительное выравнивание ширины 2b_g и 2b_v во вкладке баланса
                if self.balance_top_splitter is not None:
                    available_w = self.width() - 250 - 230
                    w2b = int(available_w * 0.4)
                    w2d = available_w - (w2b * 2)
                    self.balance_top_splitter.setSizes([w2b, w2b, w2d])
        except: pass

    def _ensure_ivl_tab_initialized(self):
        if self._ivl_initialized:
            return
        from ..rem_card_sectors.sector_ivl import SectorIvl
        self.sector_ivl = SectorIvl()
        self._ivl_layout.addWidget(self.sector_ivl)
        self._ivl_initialized = True

    def _ensure_proc_tab_initialized(self):
        if self._proc_initialized:
            return
        from ..rem_card_sectors.sector_proc import SectorProc
        self.sector_proc = SectorProc()
        self._proc_layout.addWidget(self.sector_proc)
        self._proc_initialized = True

    def _ensure_anal_tab_initialized(self):
        if self._anal_initialized:
            return
        from ..rem_card_sectors.sector_anal import SectorAnal
        self.sector_anal = SectorAnal()
        self._anal_layout.addWidget(self.sector_anal)
        self._anal_initialized = True

    def _ensure_print_tab_initialized(self):
        if self._print_initialized:
            return
        from ..rem_card_sectors.sector_print import SectorPrint
        self.sector_print = SectorPrint()
        self._print_layout.addWidget(self.sector_print)
        self._print_initialized = True

    def ensure_balance_tab_initialized(self):
        if self._balance_tab_initialized:
            return

        balance_sectors = SectorFactory.create_balance_sectors()
        for name, instance in balance_sectors.items():
            setattr(self, name, instance)

        self.balance_top_splitter = SplitterManager.create_splitter(Qt.Horizontal)
        self.balance_top_splitter.addWidget(self.sector_2b_g)
        self.balance_top_splitter.addWidget(self.sector_2b_v)
        self.balance_top_splitter.setStretchFactor(0, 1)
        self.balance_top_splitter.setStretchFactor(1, 1)

        if self.sector_2d is not None:
            self.balance_top_splitter.addWidget(self.sector_2d)
            self.balance_top_splitter.setStretchFactor(2, 0)

        self.balance_tab_layout.addWidget(self.balance_top_splitter, 4)

        self._balance_grid_wrapper = QWidget()
        grid_wrapper_layout = QVBoxLayout(self._balance_grid_wrapper)
        grid_wrapper_layout.setContentsMargins(0, 0, 0, 3)
        grid_wrapper_layout.setSpacing(0)
        if self.balance_grid is not None:
            grid_wrapper_layout.addWidget(self.balance_grid)
        self.balance_tab_layout.addWidget(self._balance_grid_wrapper, 6)

        self._balance_tab_initialized = True
        self._fix_timer.start(0)

    def ensure_events_sector(self):
        if self.sector_events is not None:
            return self.sector_events
        from ..rem_card_sectors.sector_events import SectorEvents

        self.sector_events = SectorEvents()
        self.sector_events.role = self.role
        self.sector_events.status_changed.connect(self.refresh_current_status)
        for handler in self._events_status_handlers:
            self.sector_events.status_changed.connect(handler)
        self._events_status_handlers.clear()
        self._events_layout.addWidget(self.sector_events)
        if self._events_patient_context:
            self.sector_events.set_patient(*self._events_patient_context)
        if self._events_shift_context:
            self.sector_events.set_shift_context(*self._events_shift_context)
        return self.sector_events

    def register_events_status_handler(self, handler):
        if not callable(handler):
            return
        if self.sector_events is not None:
            self.sector_events.status_changed.connect(handler)
            return
        self._events_status_handlers.append(handler)

    def set_events_context(
        self,
        *,
        admission_id=None,
        status_service=None,
        shift_date=None,
        shift_start=None,
        shift_end=None,
    ):
        if admission_id is not None and status_service is not None:
            self._events_patient_context = (admission_id, status_service)
            if self.sector_events is not None:
                self.sector_events.set_patient(admission_id, status_service)
        if (
            shift_date is not None
            and shift_start is not None
            and shift_end is not None
        ):
            self._events_shift_context = (shift_date, shift_start, shift_end)
            if self.sector_events is not None:
                self.sector_events.set_shift_context(shift_date, shift_start, shift_end)

    def ensure_nurse_orders_manager(self):
        if self.nurse_orders_manager is not None:
            return self.nurse_orders_manager
        if not self.remcard_service:
            return None

        from .components.current_orders_widget import CurrentNurseOrdersWidget
        self.nurse_orders_manager = CurrentNurseOrdersWidget(
            self.remcard_service, self.sector_1a, self.sector_5
        )
        return self.nurse_orders_manager

    def ensure_orders_widget(self):
        if hasattr(self, "orders_widget") and self.orders_widget is not None:
            return self.orders_widget

        from ..doctor_view.orders_widget import OrdersWidget

        self.orders_widget = OrdersWidget(service=self.remcard_service, defer_ui=True)
        placeholder = getattr(self, "_orders_tab_placeholder", None)
        index = self.vitals_stack.indexOf(placeholder) if placeholder is not None else -1
        current_index = self.vitals_stack.currentIndex()
        if index >= 0:
            self.vitals_stack.removeWidget(placeholder)
            placeholder.deleteLater()
            self.vitals_stack.insertWidget(index, self.orders_widget)
            if current_index == index:
                self.vitals_stack.setCurrentIndex(index)
        else:
            self.vitals_stack.addWidget(self.orders_widget)

        self.sector_7na_b.templates_requested.connect(self.orders_widget.open_template_dialog)
        return self.orders_widget

    def _refresh_beds_async(self):
        if hasattr(self, 'beds_selection_widget'):
            self.beds_selection_widget.remcard_service = self.remcard_service
            self.beds_selection_widget.refresh(queue_if_running=False)

    def _ensure_journal_widget(self):
        if self.journal_widget is not None:
            return self.journal_widget

        if not self.remcard_service:
            return None
        from rem_card.ui.patient_bed_management.management_widget import PatientBedManagementWidget

        db_manager = self.remcard_service.orders_dao.db
        self.journal_widget = PatientBedManagementWidget(
            db_manager,
            data_service=getattr(self.remcard_service, "data_service", None),
            parent=self.journal_view,
        )
        self._journal_layout.addWidget(self.journal_widget)
        return self.journal_widget

    def prewarm_journal_widget(self):
        if self.journal_widget is not None:
            return self.journal_widget

        updates_were_enabled = self.updatesEnabled()
        if updates_were_enabled:
            self.setUpdatesEnabled(False)
        try:
            return self._ensure_journal_widget()
        finally:
            if updates_were_enabled:
                self.setUpdatesEnabled(True)

    def _refresh_archive_if_needed(self, force: bool = False):
        if not getattr(self, "archive_widget", None):
            return
        if force or not getattr(self.archive_widget, "all_archived_patients", None):
            self.archive_widget.load_data()
            self._archive_last_change_id = max(self._archive_last_change_id, 0)

    def _w1_display_flags(self) -> tuple[bool, bool]:
        try:
            payload = DisplaySettingsStorage().load()
            return (
                w1a_upcoming_orders_enabled(payload, "doctor"),
                w1b_lower_sector_enabled(payload, "doctor"),
            )
        except Exception:
            return True, True

    def _apply_w1_beds_sector_visibility(self, *, refresh_w1a: bool = True):
        if not hasattr(self, "sector_1a_stack") or not hasattr(self, "sector_1b_stack"):
            return

        w1a_enabled, w1b_enabled = self._w1_display_flags()
        use_w1c = not w1a_enabled and not w1b_enabled

        if hasattr(self, "sector_w1a"):
            self.sector_w1a.apply_display_settings()
        if hasattr(self, "sector_w1b"):
            self.sector_w1b.apply_display_settings()

        if use_w1c and hasattr(self, "sector_w1c"):
            self.sector_1a_stack.setCurrentWidget(self.sector_w1c)
        else:
            self.sector_1a_stack.setCurrentWidget(self.sector_w1a)
            if refresh_w1a and w1a_enabled:
                self.sector_w1a.refresh_data()

        self.sector_1b_stack.setCurrentIndex(1)
        self.left_column.updateGeometry()
        self.left_column.update()

    def apply_display_settings(self):
        if getattr(self, "current_mode", None) in ("beds", "patient_bed_management"):
            self._apply_w1_beds_sector_visibility(refresh_w1a=False)

    def set_patient_selection_mode(self, mode):
        """Переключает режимы: 'card', 'beds', 'archive', 'admin', 'patient_bed_management'."""
        if not hasattr(self, 'selection_stack'): return
        
        if mode == "beds":
            from ...app.logger import logger
            logger.debug(f"Switching to 'beds' mode, remcard_service exists: {self.remcard_service is not None}")
            self.selection_stack.setCurrentIndex(1)
            
            # W1a/W1b сами управляют рамками и внешними отступами.
            # Лишний отступ колонки смещает W1a относительно эталонного 1a.
            if hasattr(self, 'l_layout'):
                self.l_layout.setContentsMargins(0, 0, 0, 0)
            
            self._apply_w1_beds_sector_visibility()
            
            if hasattr(self, 'beds_selection_widget'):
                logger.debug("Scheduling beds_selection_widget.refresh()")
                QTimer.singleShot(0, self._refresh_beds_async)
            else:
                logger.warning("BedsSelectionWidget is not initialized!")
            self.sector_1b.setEnabled(False)
            self.current_mode = "beds"
            self.selection_mode_changed.emit("beds")
        elif mode == "archive":
            if self.archive_widget is None and self.patient_service:
                from ..doctor_view.archive_widget import ArchiveWidget

                self.archive_widget = ArchiveWidget(self.patient_service, remcard_service=self.remcard_service)
                self._archive_layout.addWidget(self.archive_widget)
            self.selection_stack.setCurrentIndex(2)
            self._refresh_archive_if_needed(force=self.archive_widget is not None and self._archive_last_change_id < 0)
            self.sector_1b.setEnabled(False)
            self.current_mode = "archive"
            self.selection_mode_changed.emit("archive")
        elif mode == "admin":
            if self.admin_widget is None:
                from ..admin_view.admin_main_widget import AdminMainWidget

                self.admin_widget = AdminMainWidget(service=self.remcard_service, role="doctor")
                self._admin_layout.addWidget(self.admin_widget)
            self.selection_stack.setCurrentIndex(3)
            self.sector_1b.setEnabled(False)
            self.current_mode = "admin"
            self.selection_mode_changed.emit("admin")
        elif mode in ("patient_bed_management", "journal"):
            if self._ensure_journal_widget() is None:
                return
            self.selection_stack.setCurrentIndex(4)

            if hasattr(self, 'l_layout'):
                self.l_layout.setContentsMargins(0, 0, 0, 0)
            self._apply_w1_beds_sector_visibility()
            self.sector_1b.setEnabled(False)

            if hasattr(self.journal_widget, "refresh_bed_statuses"):
                QTimer.singleShot(0, self.journal_widget.refresh_bed_statuses)

            self.current_mode = "patient_bed_management"
            self.selection_mode_changed.emit("patient_bed_management")
        else: # card
            # Безмерцательное переключение в карту:
            # первый вход выполняем полностью "под капотом", включая отложенный fix,
            # и только после этого включаем перерисовку.
            is_first_card_switch = not self._first_card_mode_switch_done
            self.setUpdatesEnabled(False)
            try:
                self.selection_stack.setCurrentIndex(0)
                self.ensure_nurse_orders_manager()

                # Сбрасываем отступы левой колонки для режима карты
                if hasattr(self, 'l_layout'):
                    self.l_layout.setContentsMargins(0, 0, 0, 0)

                # Показываем активные секторы 1а и 1б
                if hasattr(self, 'sector_1a_stack'):
                    self.sector_1a_stack.setCurrentIndex(0)
                if hasattr(self, 'sector_1b_stack'):
                    self.sector_1b_stack.setCurrentIndex(0)

                self.sector_1b.setEnabled(True)
                self._post_restore_fix()
            except Exception:
                self.setUpdatesEnabled(True)
                self.updateGeometry()
                self.update()
                raise

            if is_first_card_switch:
                def _finish_first_card_switch():
                    try:
                        self._post_restore_fix()
                    finally:
                        self._first_card_mode_switch_done = True
                        self.setUpdatesEnabled(True)
                        self.updateGeometry()
                        self.update()

                QTimer.singleShot(0, _finish_first_card_switch)
            else:
                self.setUpdatesEnabled(True)
                self.updateGeometry()
                self.update()
                # Страховочный проход после цикла событий.
                QTimer.singleShot(0, self._post_restore_fix)
            self.current_mode = "card"
            self.selection_mode_changed.emit("card")

    def refresh_current_status(self):
        """Обновляет отображение текущего статуса во всех связанных виджетах."""
        if not hasattr(self, 'patient_status_service') or not hasattr(self, 'sector_4b'):
            return
            
        # Пытаемся получить текущий admission_id из секторов, если он не задан в менеджере
        adm_id = getattr(self, 'current_admission_id', None)
        if not adm_id: return
        
        status_dto = self._current_status_dto
        if status_dto is None:
            status_dto = self.patient_status_service.get_current_status(adm_id)
        self.sector_4b.update_status(status_dto)

    def set_current_status_dto(self, status_dto):
        self._current_status_dto = status_dto
        if hasattr(self, "sector_4b"):
            self.sector_4b.update_status(status_dto)

    def set_active_tab(self, tab_name):
        if not hasattr(self, 'vitals_stack'): return tab_name
        try:
            tab_name = "Движение" if tab_name == "События" else tab_name
            if hasattr(self, "sector_2b") and hasattr(self.sector_2b, "is_tab_visible"):
                if not self.sector_2b.is_tab_visible(tab_name):
                    tab_name = self.sector_2b.first_visible_tab_name()
                if hasattr(self.sector_2b, "select_tab"):
                    self.sector_2b.select_tab(tab_name, emit=False)

            is_orders = (tab_name == "Назначения")
            self.bottom_row.setVisible(not is_orders)
            self.sector_7na_b.setVisible(is_orders)
            
            # Всегда 230px
            self.sector_3_4_wrapper.setFixedWidth(230)
            
            if is_orders:
                self.ensure_orders_widget()
                self.vitals_stack.setCurrentIndex(1)
                self.sector_3_4_spacer.show()
            elif tab_name == "Витальные функции":
                self.sector_3_4_spacer.show()
                self.vitals_stack.setCurrentIndex(0)
                self.sector_7a_stack.setCurrentIndex(0)
                self.sector_7b_stack.setCurrentIndex(0)
            elif tab_name == "Баланс жидкости":
                self.ensure_balance_tab_initialized()
                self.sector_3_4_spacer.show()
                self.vitals_stack.setCurrentIndex(2)
                self.sector_7a_stack.setCurrentIndex(1)
                self.sector_7b_stack.setCurrentIndex(1)
            elif tab_name in ("Движение", "События"):
                self.ensure_events_sector()
                self.sector_3_4_spacer.show()
                self.vitals_stack.setCurrentIndex(3)
                self.sector_7a_stack.setCurrentIndex(0)
                self.sector_7b_stack.setCurrentIndex(0)
            elif tab_name == "ИВЛ":
                self._ensure_ivl_tab_initialized()
                self.sector_3_4_spacer.show()
                self.vitals_stack.setCurrentIndex(4)
                self.sector_7a_stack.setCurrentIndex(0)
                self.sector_7b_stack.setCurrentIndex(0)
            elif tab_name == "Процедуры":
                self._ensure_proc_tab_initialized()
                self.sector_3_4_spacer.show()
                self.vitals_stack.setCurrentIndex(5)
                self.sector_7a_stack.setCurrentIndex(0)
                self.sector_7b_stack.setCurrentIndex(0)
                if hasattr(self, 'sector_proc') and hasattr(self.sector_proc, 'refresh'):
                    self.sector_proc.refresh()
            elif tab_name == "Анализы":
                self._ensure_anal_tab_initialized()
                self.sector_3_4_spacer.show()
                self.vitals_stack.setCurrentIndex(6)
                self.sector_7a_stack.setCurrentIndex(0)
                self.sector_7b_stack.setCurrentIndex(0)
                if hasattr(self, 'sector_anal') and hasattr(self.sector_anal, 'refresh'):
                    self.sector_anal.refresh()
            elif tab_name == "Печать":
                self._ensure_print_tab_initialized()
                self.sector_3_4_spacer.show()
                self.vitals_stack.setCurrentIndex(7)
                self.sector_7a_stack.setCurrentIndex(0)
                self.sector_7b_stack.setCurrentIndex(0)
                if hasattr(self, 'sector_print') and hasattr(self.sector_print, 'refresh'):
                    self.sector_print.refresh()

            self._fix_timer.start(0)
            return tab_name
        except Exception as exc:
            logger.warning("Не удалось переключить вкладку РЕМ карты врача на %s: %s", tab_name, exc, exc_info=True)
            return tab_name
