from PySide6.QtCore import QCoreApplication, Qt
from PySide6.QtGui import QKeySequence, QShortcut
from PySide6.QtWidgets import (
    QApplication,
    QButtonGroup,
    QDialog,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)

from rem_card.ui.shared.loading_overlay import hide_app_loading, show_app_loading
from rem_card.ui.styles.admin_settings_styles import build_admin_settings_style
from rem_card.ui.styles.theme_manager import get_theme_manager


class AdminMainWidget(QWidget):
    """
    Админ-панель с ленивой загрузкой страниц.
    Создаем только меню сразу, а тяжелые словари и печать — по запросу.
    """

    def __init__(self, service=None, role="admin", parent=None):
        super().__init__(parent)
        self.setProperty("settingsContext", True)
        self.service = service
        self.role = role
        self._pending_print_context = None

        self.drugs_widget = None
        self.groups_widget = None
        self.diluents_widget = None
        self.forms_widget = None
        self.templates_widget = None
        self.diet_templates_widget = None
        self.lab_analysis_catalog_widget = None
        self.doctor_list_dialog = None
        self.admin_types_widget = None
        self.print_widget = None
        self.print_dialog = None
        self.theme_dialog = None
        self.display_settings_dialog = None
        self.background_settings_dialog = None
        self.operblock_icon_settings_dialog = None
        self.remcard_icon_settings_dialog = None
        self.operblock_medications_dialog = None
        self.operblock_quick_buttons_settings_widget = None
        self.operblock_route_settings_widget = None
        self.operblock_anesthesia_types_dialog = None
        self.operblock_team_dialog = None
        self.emergency_password_dialog = None
        self.db_rotation_dialog = None
        self.database_info_dialog = None
        self.settings_import_dialog = None
        self.dev_database_switch_dialog = None
        self.btn_back_to_roles = None
        self._settings_import_worker = None
        self._settings_import_loading_key = None

        self.setup_ui()

    def setup_ui(self):
        main_layout = QVBoxLayout(self)
        # Совпадает с внешней геометрией архива и соседнего сектора W1a.
        main_layout.setContentsMargins(0, 5, 5, 4)
        main_layout.setSpacing(0)

        self.surface_frame = QFrame(self)
        self.surface_frame.setObjectName("SettingsCenterFrame")
        main_layout.addWidget(self.surface_frame)

        surface_layout = QVBoxLayout(self.surface_frame)
        surface_layout.setContentsMargins(2, 2, 2, 2)
        surface_layout.setSpacing(0)

        self.stack = QStackedWidget(self.surface_frame)
        surface_layout.addWidget(self.stack)

        self.menu_widget = QWidget()
        self.menu_widget.setObjectName("AdminSettingsMenu")
        menu_layout = QHBoxLayout(self.menu_widget)
        menu_layout.setContentsMargins(0, 0, 0, 0)
        menu_layout.setSpacing(0)

        self.btn_drugs = QPushButton("Справочник препаратов")
        self.btn_groups = QPushButton("Группы препаратов")
        self.btn_forms = QPushButton("Лекарственные формы")
        self.btn_admin_types = QPushButton("Типы введения")
        self.btn_diluents = QPushButton("Растворители")
        self.btn_templates = QPushButton("Шаблоны назначений")
        self.btn_lab_analysis_catalog = QPushButton("Справочник анализов")
        self.btn_diet_templates = QPushButton("Шаблоны питания")
        self.btn_doctor_list = QPushButton("Список врачей")
        self.btn_print = QPushButton("Печать / Отчеты")
        self.btn_style = QPushButton("Цветовая схема")
        self.btn_display_settings = QPushButton("Отображение")
        self.btn_background_settings = QPushButton("Изменение фона")
        self.btn_remcard_icon_settings = QPushButton("Настройка иконок рем карты")
        self.btn_operblock_icon_settings = QPushButton("Настройка иконок оперблока")
        self.btn_operblock_medications = QPushButton("Настройки препаратов")
        self.btn_operblock_quick_buttons = QPushButton("Кнопки быстрых назначений")
        self.btn_operblock_routes = QPushButton("Оперблок - путь введения")
        self.btn_operblock_anesthesia_types = QPushButton("Виды пособия")
        self.btn_operblock_team = QPushButton("Опер. бригада")
        self.btn_emergency_password = QPushButton("Аварийный пароль")
        self.btn_db_rotation = QPushButton("Ручная ротация БД")
        self.btn_database_info = QPushButton("Информация о БД")
        self.btn_import_settings = QPushButton("Загрузить настройки")
        self.btn_switch_database = QPushButton("Смена базы")
        self.btn_backup_settings = QPushButton("Сделать бекап настроек")
        self.btn_backup_main_db = QPushButton("Создать бекап основной бд")

        try:
            from rem_card.app.runtime_paths import is_compiled

            is_dev_version = not is_compiled()
        except Exception:
            is_dev_version = False

        # Left navigation follows the settings layout from shadcn-admin, while
        # the action cards and role rules stay native to the PySide application.
        sidebar = QFrame()
        sidebar.setObjectName("SettingsSidebar")
        sidebar.setFixedWidth(264)
        sidebar_layout = QVBoxLayout(sidebar)
        sidebar_layout.setContentsMargins(20, 22, 20, 18)
        sidebar_layout.setSpacing(8)

        brand_card = QFrame()
        brand_card.setObjectName("SettingsBrandCard")
        brand_row = QHBoxLayout(brand_card)
        brand_row.setContentsMargins(12, 11, 12, 11)
        brand_row.setSpacing(10)
        brand_mark = QLabel("R")
        brand_mark.setObjectName("SettingsBrandMark")
        brand_mark.setAlignment(Qt.AlignCenter)
        brand_mark.setFixedSize(38, 38)
        brand_text = QVBoxLayout()
        brand_text.setSpacing(0)
        brand_title = QLabel("RemCard")
        brand_title.setObjectName("SettingsBrandTitle")
        brand_caption = QLabel("Центр управления")
        brand_caption.setObjectName("SettingsMutedLabel")
        brand_text.addWidget(brand_title)
        brand_text.addWidget(brand_caption)
        brand_row.addWidget(brand_mark)
        brand_row.addLayout(brand_text, 1)
        sidebar_layout.addWidget(brand_card)

        nav_caption = QLabel("НАСТРОЙКИ")
        nav_caption.setObjectName("SettingsNavCaption")
        sidebar_layout.addSpacing(18)
        sidebar_layout.addWidget(nav_caption)

        self.settings_nav_group = QButtonGroup(self)
        self.settings_nav_group.setExclusive(True)
        self.settings_nav_buttons: list[QPushButton] = []
        self.settings_categories: list[dict] = []
        self.settings_action_cards: list[QFrame] = []

        content = QWidget()
        content.setObjectName("SettingsContent")
        content_layout = QVBoxLayout(content)
        content_layout.setContentsMargins(34, 26, 34, 24)
        content_layout.setSpacing(0)

        header_row = QHBoxLayout()
        header_text = QVBoxLayout()
        header_text.setSpacing(3)
        title = QLabel("Настройки RemCard")
        title.setObjectName("SettingsPageTitle")
        subtitle = QLabel("Справочники, интерфейс и обслуживание приложения в одном месте")
        subtitle.setObjectName("SettingsPageSubtitle")
        header_text.addWidget(title)
        header_text.addWidget(subtitle)
        header_row.addLayout(header_text, 1)

        role_badge = QLabel(self._settings_role_title())
        role_badge.setObjectName("SettingsRoleBadge")
        role_badge.setAlignment(Qt.AlignCenter)
        header_row.addWidget(role_badge, 0, Qt.AlignTop)
        content_layout.addLayout(header_row)

        search_row = QHBoxLayout()
        search_row.setSpacing(12)
        self.settings_search = QLineEdit()
        self.settings_search.setObjectName("SettingsSearch")
        self.settings_search.setPlaceholderText("Поиск по настройкам…")
        self.settings_search.setClearButtonEnabled(True)
        self.settings_search.setAccessibleName("Поиск по настройкам")
        self.settings_search.setMinimumHeight(42)
        self.settings_search.textChanged.connect(self._filter_settings)
        self.settings_search_shortcut = QShortcut(QKeySequence.Find, self)
        self.settings_search_shortcut.activated.connect(self.settings_search.setFocus)
        self.settings_result_label = QLabel("Ctrl+F — быстрый поиск")
        self.settings_result_label.setObjectName("SettingsSearchHint")
        search_row.addWidget(self.settings_search, 1)
        search_row.addWidget(self.settings_result_label)
        content_layout.addSpacing(22)
        content_layout.addLayout(search_row)
        content_layout.addSpacing(18)

        self.settings_content_stack = QStackedWidget()
        self.settings_content_stack.setObjectName("SettingsCategoryStack")
        content_layout.addWidget(self.settings_content_stack, 1)

        catalog_actions = [
            (self.btn_drugs, "Препараты", "Наименования препаратов и параметры назначения.", "лекарства медикаменты"),
            (self.btn_groups, "Группы препаратов", "Классификация препаратов для поиска и фильтрации.", "категории лекарства"),
            (self.btn_forms, "Лекарственные формы", "Формы выпуска и варианты применения.", "таблетки растворы"),
            (self.btn_admin_types, "Пути введения", "Способы и типы введения лекарственных средств.", "тип введения маршрут"),
            (self.btn_diluents, "Растворители", "Справочник растворов для разведения препаратов.", "разведение"),
        ]
        if self.role != "nurse":
            catalog_actions.append(
                (self.btn_lab_analysis_catalog, "Лабораторные анализы", "Каталог исследований и отображаемых показателей.", "анализы лаборатория")
            )

        template_actions = [
            (self.btn_templates, "Шаблоны назначений", "Повторно используемые схемы врачебных назначений.", "назначения пресеты"),
            (self.btn_doctor_list, "Список врачей", "Сотрудники, доступные при оформлении документов.", "персонал сотрудники"),
        ]
        if self.role != "nurse":
            template_actions.append(
                (self.btn_diet_templates, "Шаблоны питания", "Типовые назначения питания для пациентов.", "диета питание")
            )

        interface_actions = [
            (self.btn_display_settings, "Отображение", "Вид рабочих экранов для врача, медсестры и оперблока.", "вид интерфейс роль"),
            (self.btn_background_settings, "Фон приложения", "Фон, прозрачность и оформление рабочей области.", "обои изображение прозрачность"),
            (self.btn_remcard_icon_settings, "Иконки RemCard", "Набор иконок основной карты пациента.", "значки рем карта"),
        ]

        maintenance_actions = [
            (self.btn_database_info, "Состояние баз данных", "Пути, доступность и технические сведения о хранилищах.", "бд база статус путь"),
            (self.btn_backup_settings, "Резервная копия настроек", "Создать отдельный снимок пользовательских настроек.", "бекап backup настройки"),
            (self.btn_backup_main_db, "Резервная копия основной БД", "Создать безопасную копию основной базы RemCard.", "бекап backup база"),
        ]

        system_actions = []
        if is_dev_version:
            system_actions.extend(
                [
                    (self.btn_switch_database, "Смена базы", "Выбрать другую dev-базу. Потребуется перезапуск приложения.", "переключить путь база", True),
                    (self.btn_import_settings, "Импорт настроек", "Выборочно загрузить настройки из внешней базы.", "загрузить синхронизация", True),
                ]
            )
        if self.role == "doctor":
            system_actions.extend(
                [
                    (self.btn_emergency_password, "Аварийный пароль", "Изменить пароль доступа к аварийному режиму.", "безопасность доступ", True),
                    (self.btn_db_rotation, "Ручная ротация БД", "Закрыть текущий период и подготовить новую рабочую базу.", "архив новая база", True),
                ]
            )

        self._add_settings_category("catalogs", "Справочники", "Базовые медицинские справочники, используемые во всех модулях.", catalog_actions)
        self._add_settings_category("templates", "Шаблоны и сотрудники", "Повторно используемые назначения и списки сотрудников.", template_actions)
        self._add_settings_category("interface", "Интерфейс", "Настройте внешний вид RemCard под рабочее место.", interface_actions)
        self._add_settings_category(
            "reports",
            "Печать и отчёты",
            "Форматы печатных документов и параметры формирования отчётов.",
            [(self.btn_print, "Печать и отчёты", "Поля, форматы и параметры печатных документов.", "печать pdf отчет")],
        )
        if self.role != "nurse":
            self._add_settings_category(
                "operblock",
                "Операционный блок",
                "Справочники и быстрые действия рабочего места оперблока.",
                [
                    (self.btn_operblock_icon_settings, "Иконки оперблока", "Значки карточек и рабочих действий оперблока.", "значки"),
                    (self.btn_operblock_medications, "Препараты оперблока", "Набор препаратов и готовых дозировок.", "лекарства дозировки"),
                    (self.btn_operblock_quick_buttons, "Быстрые назначения", "Кнопки часто используемых назначений.", "кнопки пресеты"),
                    (self.btn_operblock_routes, "Пути введения", "Пути введения, доступные в оперблоке.", "маршрут тип введения"),
                    (self.btn_operblock_anesthesia_types, "Виды пособия", "Справочник видов анестезиологического пособия.", "анестезия"),
                    (self.btn_operblock_team, "Операционная бригада", "Должности и участники операционной бригады.", "сотрудники персонал"),
                ],
            )
        self._add_settings_category("maintenance", "Обслуживание", "Контроль состояния данных и создание резервных копий.", maintenance_actions)
        if system_actions:
            self._add_settings_category(
                "system",
                "Безопасность и система",
                "Операции с повышенным риском. Перед применением проверьте последствия.",
                system_actions,
                warning=True,
            )

        # Buttons that are intentionally unavailable remain real attributes for
        # compatibility with integrations and tests, but never become windows.
        attached_buttons = {entry["button"] for entry in self.settings_action_cards}
        for button in (
            self.btn_style,
            self.btn_lab_analysis_catalog,
            self.btn_diet_templates,
            self.btn_operblock_icon_settings,
            self.btn_operblock_medications,
            self.btn_operblock_quick_buttons,
            self.btn_operblock_routes,
            self.btn_operblock_anesthesia_types,
            self.btn_operblock_team,
            self.btn_emergency_password,
            self.btn_db_rotation,
            self.btn_import_settings,
            self.btn_switch_database,
        ):
            if button not in attached_buttons:
                button.setParent(self.menu_widget)
                button.hide()

        for category in self.settings_categories:
            sidebar_layout.addWidget(category["nav_button"])
        sidebar_layout.addStretch(1)

        if self.role == "admin":
            self.btn_back_to_roles = QPushButton("← Назад")
            self.btn_back_to_roles.setObjectName("SettingsBackButton")
            self.btn_back_to_roles.setMinimumHeight(40)
            sidebar_layout.addWidget(self.btn_back_to_roles)

        menu_layout.addWidget(sidebar)
        menu_layout.addWidget(content, 1)

        if self.settings_categories:
            self._select_settings_category(0)

        self.setStyleSheet(
            build_admin_settings_style(get_theme_manager().current_tokens())
        )

        self.stack.addWidget(self.menu_widget)
        self.stack.setCurrentWidget(self.menu_widget)

        self.btn_drugs.clicked.connect(self.open_drugs)
        self.btn_groups.clicked.connect(self.open_groups)
        self.btn_forms.clicked.connect(self.open_forms)
        self.btn_admin_types.clicked.connect(self.open_admin_types)
        self.btn_diluents.clicked.connect(self.open_diluents)
        self.btn_templates.clicked.connect(self.open_templates)
        self.btn_lab_analysis_catalog.clicked.connect(self.open_lab_analysis_catalog)
        self.btn_doctor_list.clicked.connect(self.open_doctor_list)
        self.btn_diet_templates.clicked.connect(self.open_diet_templates)
        self.btn_print.clicked.connect(self.open_print)
        self.btn_display_settings.clicked.connect(self.open_display_settings)
        self.btn_background_settings.clicked.connect(self.open_background_settings)
        self.btn_remcard_icon_settings.clicked.connect(self.open_remcard_icon_settings)
        self.btn_operblock_icon_settings.clicked.connect(self.open_operblock_icon_settings)
        self.btn_operblock_medications.clicked.connect(self.open_operblock_medications_settings)
        self.btn_operblock_quick_buttons.clicked.connect(self.open_operblock_quick_buttons_settings)
        self.btn_operblock_routes.clicked.connect(self.open_operblock_route_settings)
        self.btn_operblock_anesthesia_types.clicked.connect(self.open_operblock_anesthesia_types_settings)
        self.btn_operblock_team.clicked.connect(self.open_operblock_team_settings)
        self.btn_emergency_password.clicked.connect(self.open_emergency_password)
        self.btn_db_rotation.clicked.connect(self.open_db_rotation)
        self.btn_database_info.clicked.connect(self.open_database_info)
        self.btn_switch_database.clicked.connect(self.open_dev_database_switch)
        self.btn_import_settings.clicked.connect(self.open_settings_import)
        self.btn_backup_settings.clicked.connect(self.create_settings_backup)
        self.btn_backup_main_db.clicked.connect(self.create_main_db_backup)

    def _settings_role_title(self) -> str:
        return {
            "admin": "Администратор",
            "doctor": "Врач",
            "nurse": "Медсестра",
        }.get(str(self.role or "").lower(), "Настройки")

    def _add_settings_category(
        self,
        key: str,
        title: str,
        description: str,
        actions: list[tuple],
        *,
        warning: bool = False,
    ) -> None:
        if not actions:
            return

        page = QWidget()
        page.setObjectName("SettingsCategoryPage")
        page_layout = QVBoxLayout(page)
        page_layout.setContentsMargins(0, 0, 0, 0)
        page_layout.setSpacing(14)

        heading_row = QHBoxLayout()
        heading_text = QVBoxLayout()
        heading_text.setSpacing(3)
        heading = QLabel(title)
        heading.setObjectName("SettingsSectionTitle")
        section_description = QLabel(description)
        section_description.setObjectName("SettingsSectionDescription")
        section_description.setWordWrap(True)
        heading_text.addWidget(heading)
        heading_text.addWidget(section_description)
        heading_row.addLayout(heading_text, 1)
        count_badge = QLabel(str(len(actions)))
        count_badge.setObjectName("SettingsCountBadge")
        count_badge.setAlignment(Qt.AlignCenter)
        count_badge.setFixedSize(34, 26)
        heading_row.addWidget(count_badge, 0, Qt.AlignTop)
        page_layout.addLayout(heading_row)

        if warning:
            warning_label = QLabel("Перед выполнением этих действий убедитесь, что создана актуальная резервная копия.")
            warning_label.setObjectName("SettingsWarningBanner")
            warning_label.setWordWrap(True)
            page_layout.addWidget(warning_label)

        scroll = QScrollArea()
        scroll.setObjectName("SettingsCardsScroll")
        scroll.setWidgetResizable(True)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        cards_widget = QWidget()
        cards_widget.setObjectName("SettingsCardsContainer")
        cards_layout = QGridLayout(cards_widget)
        cards_layout.setContentsMargins(0, 2, 8, 16)
        cards_layout.setHorizontalSpacing(14)
        cards_layout.setVerticalSpacing(14)
        cards_layout.setColumnStretch(0, 1)
        cards_layout.setColumnStretch(1, 1)

        category_cards = []
        for index, action in enumerate(actions):
            button, action_title, action_description, keywords, *options = action
            danger = bool(options[0]) if options else False
            card = self._create_settings_action_card(
                button,
                action_title,
                action_description,
                keywords,
                category_key=key,
                danger=danger,
            )
            cards_layout.addWidget(card, index // 2, index % 2)
            category_cards.append(card)
        cards_layout.setRowStretch((len(actions) + 1) // 2, 1)
        scroll.setWidget(cards_widget)
        page_layout.addWidget(scroll, 1)

        nav_button = QPushButton(f"{title}   {len(actions)}")
        nav_button.setObjectName("SettingsNavButton")
        nav_button.setCheckable(True)
        nav_button.setCursor(Qt.PointingHandCursor)
        nav_button.setMinimumHeight(42)
        nav_button.setProperty("categoryKey", key)
        category_index = len(self.settings_categories)
        nav_button.clicked.connect(lambda _checked=False, index=category_index: self._select_settings_category(index))
        self.settings_nav_group.addButton(nav_button, category_index)
        self.settings_nav_buttons.append(nav_button)

        self.settings_content_stack.addWidget(page)
        self.settings_categories.append(
            {
                "key": key,
                "title": title,
                "page": page,
                "nav_button": nav_button,
                "cards": category_cards,
                "count_badge": count_badge,
            }
        )

    def _create_settings_action_card(
        self,
        button: QPushButton,
        title: str,
        description: str,
        keywords: str,
        *,
        category_key: str,
        danger: bool,
    ) -> QFrame:
        card = QFrame()
        card.setObjectName("SettingsActionCard")
        card.setProperty("variant", "danger" if danger else "default")
        card.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        card.setMinimumHeight(174)
        layout = QVBoxLayout(card)
        layout.setContentsMargins(18, 16, 18, 16)
        layout.setSpacing(7)

        icon_label = QLabel(self._settings_action_glyph(category_key, danger=danger))
        icon_label.setObjectName("SettingsDangerGlyph" if danger else "SettingsActionGlyph")
        icon_label.setAlignment(Qt.AlignCenter)
        icon_label.setFixedSize(34, 34)
        action_title = QLabel(title)
        action_title.setObjectName("SettingsActionTitle")
        action_title.setWordWrap(True)
        action_description = QLabel(description)
        action_description.setObjectName("SettingsActionDescription")
        action_description.setWordWrap(True)
        action_description.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Expanding)

        layout.addWidget(icon_label, 0, Qt.AlignLeft)
        layout.addWidget(action_title)
        layout.addWidget(action_description, 1)
        button.setObjectName("SettingsDangerButton" if danger else "SettingsActionButton")
        button.setCursor(Qt.PointingHandCursor)
        button.setMinimumHeight(36)
        button.setMaximumWidth(260)
        button.setAccessibleDescription(description)
        layout.addWidget(button, 0, Qt.AlignLeft)

        search_blob = " ".join((title, description, keywords, button.text())).casefold()
        card.setProperty("settingsSearchBlob", search_blob)
        card.setProperty("settingsCategory", category_key)
        self.settings_action_cards.append({"card": card, "button": button, "search_blob": search_blob})
        return card

    @staticmethod
    def _settings_action_glyph(category_key: str, *, danger: bool = False) -> str:
        if danger:
            return "!"
        return {
            "catalogs": "≡",
            "templates": "▤",
            "interface": "◐",
            "reports": "▧",
            "operblock": "+",
            "maintenance": "✓",
        }.get(category_key, "•")

    def _select_settings_category(self, index: int) -> None:
        if not 0 <= index < len(self.settings_categories):
            return
        category = self.settings_categories[index]
        if category["nav_button"].isHidden():
            return
        self.settings_content_stack.setCurrentWidget(category["page"])
        category["nav_button"].setChecked(True)

    def _filter_settings(self, text: str) -> None:
        query = " ".join(str(text or "").casefold().split())
        total_visible = 0
        first_visible_category = None
        current_category = None
        current_page = self.settings_content_stack.currentWidget()

        for index, category in enumerate(self.settings_categories):
            visible_in_category = 0
            if category["page"] is current_page:
                current_category = index
            for card in category["cards"]:
                search_blob = str(card.property("settingsSearchBlob") or "")
                matches = not query or all(part in search_blob for part in query.split())
                card.setVisible(matches)
                visible_in_category += int(matches)
            category["count_badge"].setText(str(visible_in_category))
            category["nav_button"].setVisible(visible_in_category > 0)
            if visible_in_category and first_visible_category is None:
                first_visible_category = index
            total_visible += visible_in_category

        if query:
            self.settings_result_label.setText(f"Найдено: {total_visible}")
            current_has_results = False
            if current_category is not None:
                current_nav = self.settings_categories[current_category]["nav_button"]
                current_has_results = not current_nav.isHidden()
            if not current_has_results and first_visible_category is not None:
                self._select_settings_category(first_visible_category)
            self.settings_search.setProperty("noResults", total_visible == 0)
        else:
            self.settings_result_label.setText("Ctrl+F — быстрый поиск")
            self.settings_search.setProperty("noResults", False)
            if current_category is None and first_visible_category is not None:
                self._select_settings_category(first_visible_category)

        self.settings_search.style().unpolish(self.settings_search)
        self.settings_search.style().polish(self.settings_search)

    def _show_settings_loading(
        self,
        message: str,
        *,
        key: str,
        auto_hide_ms: int = 15000,
    ) -> str | None:
        return show_app_loading(
            self,
            message,
            key=f"admin-settings-{key}:{id(self)}",
            auto_hide_ms=auto_hide_ms,
            process_events=True,
        )

    def _hide_settings_loading(self, loading_key: str | None, *, delay_ms: int = 0) -> None:
        if loading_key:
            hide_app_loading(self, loading_key, delay_ms=delay_ms)

    @staticmethod
    def _prepare_settings_surface(widget):
        from rem_card.ui.styles.settings_surface import apply_settings_surface

        apply_settings_surface(widget)
        return widget

    def _show_page(self, widget):
        if widget is not None:
            self._prepare_settings_surface(widget)
            loading_key = show_app_loading(
                self,
                "Загрузка раздела...",
                key=f"admin-page:{id(self)}",
                auto_hide_ms=8000,
                process_events=True,
            )
            try:
                try:
                    from rem_card.services.prescription_engine import engine

                    engine.reload_if_changed(force_check=True)
                except Exception:
                    pass
                load_data = getattr(widget, "load_data", None)
                if callable(load_data):
                    load_data()
                self.stack.setCurrentWidget(widget)
            finally:
                if loading_key:
                    hide_app_loading(self, loading_key, delay_ms=350)

    def _connect_back(self, widget):
        if hasattr(widget, "btn_back"):
            widget.btn_back.clicked.connect(self.show_menu)
        return widget

    def _ensure_drugs_widget(self):
        if self.drugs_widget is None:
            from .drugs_dict_widget import DrugsDictWidget

            self.drugs_widget = self._connect_back(DrugsDictWidget())
            self.stack.addWidget(self.drugs_widget)
        return self.drugs_widget

    def _ensure_groups_widget(self):
        if self.groups_widget is None:
            from .groups_dict_widget import GroupsDictWidget

            self.groups_widget = self._connect_back(GroupsDictWidget())
            self.stack.addWidget(self.groups_widget)
        return self.groups_widget

    def _ensure_forms_widget(self):
        if self.forms_widget is None:
            from .forms_dict_widget import FormsDictWidget

            self.forms_widget = self._connect_back(FormsDictWidget())
            self.stack.addWidget(self.forms_widget)
        return self.forms_widget

    def _ensure_admin_types_widget(self):
        if self.admin_types_widget is None:
            from .admin_types_dict_widget import AdminTypesDictWidget

            self.admin_types_widget = self._connect_back(AdminTypesDictWidget())
            self.stack.addWidget(self.admin_types_widget)
        return self.admin_types_widget

    def _ensure_diluents_widget(self):
        if self.diluents_widget is None:
            from .diluents_dict_widget import DiluentsDictWidget

            self.diluents_widget = self._connect_back(DiluentsDictWidget())
            self.stack.addWidget(self.diluents_widget)
        return self.diluents_widget

    def _ensure_templates_widget(self):
        if self.templates_widget is None:
            from .templates_dict_widget import TemplatesDictWidget

            self.templates_widget = self._connect_back(TemplatesDictWidget())
            self.stack.addWidget(self.templates_widget)
        return self.templates_widget

    def _ensure_diet_templates_widget(self):
        if self.diet_templates_widget is None:
            from .diet_templates_widget import DietTemplatesWidget

            self.diet_templates_widget = self._connect_back(DietTemplatesWidget(self.service, role=self.role))
            self.stack.addWidget(self.diet_templates_widget)
        elif hasattr(self.diet_templates_widget, "set_service"):
            self.diet_templates_widget.set_service(self.service)
        return self.diet_templates_widget

    def _ensure_lab_analysis_catalog_widget(self):
        if self.lab_analysis_catalog_widget is None:
            from .lab_analysis_catalog_widget import LabAnalysisCatalogWidget

            self.lab_analysis_catalog_widget = self._connect_back(
                LabAnalysisCatalogWidget(self.service, role=self.role)
            )
            self.stack.addWidget(self.lab_analysis_catalog_widget)
        elif hasattr(self.lab_analysis_catalog_widget, "set_service"):
            self.lab_analysis_catalog_widget.set_service(self.service)
        return self.lab_analysis_catalog_widget

    def _ensure_doctor_list_page(self):
        if self.doctor_list_dialog is None:
            from .dictionary_page_chrome import prepare_embedded_settings_page
            from .doctor_list_dialog import DoctorListDialog

            self.doctor_list_dialog = DoctorListDialog(parent=self)
            prepare_embedded_settings_page(
                self.doctor_list_dialog,
                title="Список врачей",
                description="Управляйте врачами и должностями, доступными в протоколах и процедурах.",
                hide_window_actions=("Закрыть",),
            )
            self._connect_back(self.doctor_list_dialog)
            self.stack.addWidget(self.doctor_list_dialog)
        return self.doctor_list_dialog

    def _ensure_operblock_route_settings_widget(self):
        if self.operblock_route_settings_widget is None:
            from .operblock_route_settings_widget import OperBlockRouteSettingsWidget

            self.operblock_route_settings_widget = self._connect_back(OperBlockRouteSettingsWidget())
            self.stack.addWidget(self.operblock_route_settings_widget)
        return self.operblock_route_settings_widget

    def _ensure_operblock_quick_buttons_settings_widget(self):
        if self.operblock_quick_buttons_settings_widget is None:
            from .operblock_quick_buttons_settings_widget import OperBlockQuickButtonsSettingsWidget

            self.operblock_quick_buttons_settings_widget = self._connect_back(OperBlockQuickButtonsSettingsWidget())
            self.stack.addWidget(self.operblock_quick_buttons_settings_widget)
        return self.operblock_quick_buttons_settings_widget

    def _ensure_print_dialog(self):
        if self.print_dialog is None:
            from .print_settings_widget import PrintSettingsDialog
            from .dictionary_page_chrome import prepare_embedded_settings_page

            self.print_dialog = PrintSettingsDialog(parent=self)
            prepare_embedded_settings_page(
                self.print_dialog,
                title="Печать и отчёты",
                description="Выберите разделы, которые должны попадать в печатные документы. Изменения сохраняются автоматически.",
            )
            self.print_widget = self.print_dialog.settings_widget
            self._connect_back(self.print_dialog)
            self.stack.addWidget(self.print_dialog)
            if self._pending_print_context is not None:
                self.print_dialog.set_context(*self._pending_print_context)
        return self.print_dialog

    def _ensure_display_settings_page(self):
        if self.display_settings_dialog is None:
            from .dictionary_page_chrome import prepare_embedded_settings_page
            from .display_settings_dialog import DisplaySettingsDialog

            self.display_settings_dialog = DisplaySettingsDialog(
                initial_role=self.role,
                parent=self,
            )
            prepare_embedded_settings_page(
                self.display_settings_dialog,
                title="Отображение интерфейса",
                description="Управляйте видимостью и порядком рабочих разделов отдельно для каждой роли.",
                hide_window_actions=("Отмена",),
            )
            self._connect_back(self.display_settings_dialog)
            self.stack.addWidget(self.display_settings_dialog)
        return self.display_settings_dialog

    def _ensure_background_settings_page(self):
        if self.background_settings_dialog is None:
            from .background_settings_dialog import BackgroundSettingsDialog
            from .dictionary_page_chrome import prepare_embedded_settings_page

            self.background_settings_dialog = BackgroundSettingsDialog(parent=self)
            prepare_embedded_settings_page(
                self.background_settings_dialog,
                title="Фон приложения",
                description="Настройте фон рабочей области и периоды показа сезонных изображений.",
                hide_window_actions=("Отмена",),
            )
            self._connect_back(self.background_settings_dialog)
            self.stack.addWidget(self.background_settings_dialog)
        return self.background_settings_dialog

    def _ensure_remcard_icon_settings_page(self):
        if self.remcard_icon_settings_dialog is None:
            from .dictionary_page_chrome import prepare_embedded_settings_page
            from .remcard_icon_settings_dialog import RemCardIconSettingsDialog

            self.remcard_icon_settings_dialog = RemCardIconSettingsDialog(parent=self)
            prepare_embedded_settings_page(
                self.remcard_icon_settings_dialog,
                title="Иконки RemCard",
                description="Заменяйте и проверяйте иконки карточки пациента в едином рабочем пространстве.",
                hide_window_actions=("Закрыть",),
            )
            self._connect_back(self.remcard_icon_settings_dialog)
            self.stack.addWidget(self.remcard_icon_settings_dialog)
        return self.remcard_icon_settings_dialog

    def _ensure_operblock_icon_settings_page(self):
        if self.operblock_icon_settings_dialog is None:
            from .dictionary_page_chrome import prepare_embedded_settings_page
            from .operblock_icon_settings_dialog import OperBlockIconSettingsDialog

            self.operblock_icon_settings_dialog = OperBlockIconSettingsDialog(parent=self)
            prepare_embedded_settings_page(
                self.operblock_icon_settings_dialog,
                title="Иконки операционного блока",
                description="Настройте основные значки и изображения препаратов, используемые на рабочем месте оперблока.",
                hide_window_actions=("Закрыть",),
            )
            self._connect_back(self.operblock_icon_settings_dialog)
            self.stack.addWidget(self.operblock_icon_settings_dialog)
        return self.operblock_icon_settings_dialog

    def _attach_operblock_settings_page(
        self,
        dialog,
        *,
        attribute: str,
        title: str,
        description: str,
        hide_window_actions=(),
        on_accept=None,
    ):
        from .dictionary_page_chrome import prepare_embedded_settings_page

        prepare_embedded_settings_page(
            dialog,
            title=title,
            description=description,
            hide_window_actions=hide_window_actions,
        )
        dialog.btn_back.clicked.connect(
            lambda _checked=False, name=attribute: self._discard_operblock_settings_page(name)
        )
        dialog.embedded_reject_requested.connect(
            lambda name=attribute: self._discard_operblock_settings_page(name)
        )
        if callable(on_accept):
            dialog.embedded_accept_requested.connect(on_accept)
        self.stack.addWidget(dialog)
        return dialog

    def _discard_operblock_settings_page(self, attribute: str) -> None:
        page = getattr(self, attribute, None)
        if page is None:
            return
        save_header_state = getattr(page, "_save_table_header_state", None)
        if callable(save_header_state):
            save_header_state()
        if self.stack.currentWidget() is page:
            self.show_menu()
        self.stack.removeWidget(page)
        setattr(self, attribute, None)
        page.deleteLater()

    def _ensure_operblock_medications_page(self):
        if self.operblock_medications_dialog is None:
            from rem_card.services.operblock_medication_presets import (
                load_operblock_medication_presets,
                save_operblock_medication_presets,
            )
            from rem_card.ui.operblock_view.operblock_main_widget import OperBlockMedicationPresetsDialog

            presets = load_operblock_medication_presets(include_disabled=True)
            dialog = OperBlockMedicationPresetsDialog(
                presets,
                parent=self,
                save_handler=save_operblock_medication_presets,
            )
            self.operblock_medications_dialog = self._attach_operblock_settings_page(
                dialog,
                attribute="operblock_medications_dialog",
                title="Препараты операционного блока",
                description="Настройте доступность, отображение, дозы, растворители и избранные препараты рабочего места оперблока.",
                hide_window_actions=("Отменить",),
            )
        return self.operblock_medications_dialog

    def _ensure_operblock_anesthesia_types_page(self):
        if self.operblock_anesthesia_types_dialog is None:
            from rem_card.services.operblock_anesthesia_types import load_operblock_anesthesia_types
            from rem_card.ui.operblock_view.operblock_main_widget import OperBlockAnesthesiaTypesDialog

            dialog = OperBlockAnesthesiaTypesDialog(
                load_operblock_anesthesia_types(),
                parent=self,
            )
            self.operblock_anesthesia_types_dialog = self._attach_operblock_settings_page(
                dialog,
                attribute="operblock_anesthesia_types_dialog",
                title="Виды пособия",
                description="Управляйте перечнем анестезиологических пособий и порядком их отображения.",
                hide_window_actions=("Отмена",),
                on_accept=self._save_operblock_anesthesia_types_page,
            )
        return self.operblock_anesthesia_types_dialog

    def _ensure_operblock_team_page(self):
        if self.operblock_team_dialog is None:
            from rem_card.services.operblock_team import load_operblock_team
            from rem_card.ui.operblock_view.operblock_main_widget import OperBlockTeamDialog

            dialog = OperBlockTeamDialog(load_operblock_team(), parent=self)
            self.operblock_team_dialog = self._attach_operblock_settings_page(
                dialog,
                attribute="operblock_team_dialog",
                title="Операционная бригада",
                description="Настройте сотрудников и должности, доступные при формировании операционной бригады.",
                hide_window_actions=("Отмена",),
                on_accept=self._save_operblock_team_page,
            )
        return self.operblock_team_dialog

    def _ensure_database_info_page(self):
        if self.database_info_dialog is None:
            from .database_info_dialog import DatabaseInfoDialog
            from .dictionary_page_chrome import prepare_embedded_settings_page

            db_manager = self._resolve_db_manager()
            self.database_info_dialog = DatabaseInfoDialog(db_manager, parent=self)
            prepare_embedded_settings_page(
                self.database_info_dialog,
                title="Состояние баз данных",
                description="Рабочие базы, циклы ротации, резервные копии и техническая история хранилища.",
                hide_window_actions=("Закрыть",),
            )
            self._connect_back(self.database_info_dialog)
            self.stack.addWidget(self.database_info_dialog)
        return self.database_info_dialog

    def _ensure_db_rotation_page(self):
        if self.db_rotation_dialog is None:
            from .db_rotation_dialog import DbRotationDialog
            from .dictionary_page_chrome import prepare_embedded_settings_page

            db_manager = self._resolve_db_manager()
            self.db_rotation_dialog = DbRotationDialog(
                db_manager,
                parent=self,
                on_rotated=self._on_db_rotated,
                rotation_owner_context=self._manual_rotation_owner_context,
                on_restart_requested=self._request_application_restart,
            )
            prepare_embedded_settings_page(
                self.db_rotation_dialog,
                title="Ручная ротация базы данных",
                description="Закройте текущий цикл или отмените последнюю ручную ротацию после проверки состояния рабочих мест.",
                hide_window_actions=("Закрыть",),
            )
            self._connect_back(self.db_rotation_dialog)
            self.stack.addWidget(self.db_rotation_dialog)
        return self.db_rotation_dialog

    def _ensure_dev_database_switch_page(self):
        if self.dev_database_switch_dialog is None:
            from .dev_database_switch_dialog import DevDatabaseSwitchDialog
            from .dictionary_page_chrome import prepare_embedded_settings_page

            self.dev_database_switch_dialog = DevDatabaseSwitchDialog(parent=self)
            prepare_embedded_settings_page(
                self.dev_database_switch_dialog,
                title="Смена базы",
                description="Выберите и проверьте базу, которая будет подключена после перезапуска dev-версии.",
                hide_window_actions=("Отмена",),
            )
            self.dev_database_switch_dialog.btn_back.clicked.connect(
                self.dev_database_switch_dialog.cancel_pending_validation
            )
            self.dev_database_switch_dialog.applied.connect(
                self._on_dev_database_switch_applied
            )
            self._connect_back(self.dev_database_switch_dialog)
            self.stack.addWidget(self.dev_database_switch_dialog)
        return self.dev_database_switch_dialog

    def open_drugs(self):
        self._show_page(self._ensure_drugs_widget())

    def open_groups(self):
        self._show_page(self._ensure_groups_widget())

    def open_forms(self):
        self._show_page(self._ensure_forms_widget())

    def open_admin_types(self):
        self._show_page(self._ensure_admin_types_widget())

    def open_diluents(self):
        self._show_page(self._ensure_diluents_widget())

    def open_templates(self):
        self._show_page(self._ensure_templates_widget())

    def open_lab_analysis_catalog(self):
        self._show_page(self._ensure_lab_analysis_catalog_widget())

    def open_doctor_list(self):
        loading_key = self._show_settings_loading("Загрузка списка врачей...", key="doctor-list")
        try:
            page = self._ensure_doctor_list_page()
        finally:
            self._hide_settings_loading(loading_key)
        self._show_page(page)

    def open_diet_templates(self):
        self._show_page(self._ensure_diet_templates_widget())

    def open_print(self):
        loading_key = self._show_settings_loading("Загрузка настроек печати...", key="print")
        try:
            dialog = self._ensure_print_dialog()
            if self._pending_print_context is not None:
                dialog.set_context(*self._pending_print_context)
            dialog.load_settings()
        finally:
            self._hide_settings_loading(loading_key)
        self._show_page(dialog)

    def open_style(self):
        loading_key = self._show_settings_loading("Загрузка цветовой схемы...", key="style")
        try:
            from rem_card.ui.styles.theme_settings_dialog import ThemeSettingsDialog

            role = self.role if self.role in ("doctor", "nurse") else "doctor"
            dialog = ThemeSettingsDialog(role=role, parent=self)
            self._prepare_settings_surface(dialog)
        finally:
            self._hide_settings_loading(loading_key)
        dialog.exec()

    def open_display_settings(self):
        loading_key = self._show_settings_loading("Загрузка настроек отображения...", key="display")
        try:
            page = self._ensure_display_settings_page()
        finally:
            self._hide_settings_loading(loading_key)
        self._show_page(page)

    def open_background_settings(self):
        loading_key = self._show_settings_loading("Загрузка настроек фона...", key="background")
        try:
            page = self._ensure_background_settings_page()
        finally:
            self._hide_settings_loading(loading_key)
        self._show_page(page)

    def open_remcard_icon_settings(self):
        loading_key = self._show_settings_loading("Загрузка настроек иконок рем карты...", key="remcard-icons")
        try:
            page = self._ensure_remcard_icon_settings_page()
        finally:
            self._hide_settings_loading(loading_key)
        self._show_page(page)

    def open_operblock_icon_settings(self):
        loading_key = self._show_settings_loading("Загрузка настроек иконок оперблока...", key="operblock-icons")
        try:
            page = self._ensure_operblock_icon_settings_page()
        finally:
            self._hide_settings_loading(loading_key)
        self._show_page(page)

    def open_operblock_medications_settings(self):
        from rem_card.ui.shared.custom_message_box import CustomMessageBox

        loading_key = self._show_settings_loading("Загрузка настроек препаратов...", key="operblock-medications")
        try:
            try:
                page = self._ensure_operblock_medications_page()
            except Exception as exc:
                CustomMessageBox.warning(self, "Настройки препаратов", f"Не удалось загрузить препараты оперблока: {exc}")
                return
        finally:
            self._hide_settings_loading(loading_key)
        self._show_page(page)

    def open_operblock_route_settings(self):
        self._show_page(self._ensure_operblock_route_settings_widget())

    def open_operblock_quick_buttons_settings(self):
        self._show_page(self._ensure_operblock_quick_buttons_settings_widget())

    def open_operblock_anesthesia_types_settings(self):
        from rem_card.ui.shared.custom_message_box import CustomMessageBox

        loading_key = self._show_settings_loading("Загрузка видов пособия...", key="anesthesia-types")
        try:
            try:
                page = self._ensure_operblock_anesthesia_types_page()
            except Exception as exc:
                CustomMessageBox.warning(self, "Виды пособия", f"Не удалось загрузить виды пособия: {exc}")
                return
        finally:
            self._hide_settings_loading(loading_key)
        self._show_page(page)

    def _save_operblock_anesthesia_types_page(self):
        from rem_card.services.operblock_anesthesia_types import save_operblock_anesthesia_types
        from rem_card.ui.shared.custom_message_box import CustomMessageBox

        dialog = self.operblock_anesthesia_types_dialog
        if dialog is None:
            return
        try:
            save_operblock_anesthesia_types(dialog.items())
        except Exception as exc:
            CustomMessageBox.warning(self, "Виды пособия", f"Не удалось сохранить виды пособия: {exc}")
            return
        self._discard_operblock_settings_page("operblock_anesthesia_types_dialog")

    def open_operblock_team_settings(self):
        from rem_card.ui.shared.custom_message_box import CustomMessageBox

        loading_key = self._show_settings_loading("Загрузка опер. бригады...", key="operblock-team")
        try:
            try:
                page = self._ensure_operblock_team_page()
            except Exception as exc:
                CustomMessageBox.warning(self, "Опер. бригада", f"Не удалось загрузить опер. бригаду: {exc}")
                return
        finally:
            self._hide_settings_loading(loading_key)
        self._show_page(page)

    def _save_operblock_team_page(self):
        from rem_card.services.operblock_team import save_operblock_team
        from rem_card.ui.shared.custom_message_box import CustomMessageBox

        dialog = self.operblock_team_dialog
        if dialog is None:
            return
        try:
            save_operblock_team(dialog.items())
        except Exception as exc:
            CustomMessageBox.warning(self, "Опер. бригада", f"Не удалось сохранить опер. бригаду: {exc}")
            return
        self._discard_operblock_settings_page("operblock_team_dialog")

    def open_emergency_password(self):
        loading_key = self._show_settings_loading("Загрузка аварийного пароля...", key="emergency-password")
        try:
            from .emergency_password_dialog import EmergencyPasswordSettingsDialog

            self.emergency_password_dialog = EmergencyPasswordSettingsDialog(parent=self)
            self._prepare_settings_surface(self.emergency_password_dialog)
        finally:
            self._hide_settings_loading(loading_key)
        self.emergency_password_dialog.exec()

    def open_db_rotation(self):
        loading_key = self._show_settings_loading("Загрузка управления БД...", key="db-rotation")
        try:
            from rem_card.ui.shared.custom_message_box import CustomMessageBox

            try:
                page = self._ensure_db_rotation_page()
            except Exception as exc:
                CustomMessageBox.warning(self, "Ротация БД", f"Не удалось открыть управление БД:\n{exc}")
                return
        finally:
            self._hide_settings_loading(loading_key)
        self._show_page(page)

    def _manual_rotation_owner_context(self):
        window = self.window()
        getter = getattr(window, "manual_rotation_owner_context", None)
        return getter() if callable(getter) else None

    def _request_application_restart(self):
        app = QApplication.instance()
        if app is None:
            return
        app.setProperty("remcard_restart_requested", True)
        top_level = self.window()
        if top_level is not None and top_level is not self:
            if not bool(top_level.close()):
                app.setProperty("remcard_restart_requested", False)
        else:
            app.quit()

    def open_database_info(self):
        from rem_card.ui.shared.custom_message_box import CustomMessageBox

        try:
            page = self._ensure_database_info_page()
        except Exception as exc:
            CustomMessageBox.warning(
                self,
                "Информация о БД",
                f"Не удалось открыть сведения о базах данных:\n{exc}",
            )
            return
        page.reload_info()
        self._show_page(page)

    def create_settings_backup(self):
        from rem_card.services.settings.settings_service import get_settings_service
        from rem_card.ui.shared.custom_message_box import CustomMessageBox

        loading_key = self._show_settings_loading("Создание бекапа настроек...", key="settings-backup", auto_hide_ms=60000)
        try:
            backup_path = get_settings_service().create_manual_settings_backup()
        except Exception as exc:
            CustomMessageBox.warning(self, "Бекап настроек", f"Не удалось создать бекап настроек:\n{exc}")
            return
        finally:
            self._hide_settings_loading(loading_key)
        CustomMessageBox.information(
            self,
            "Бекап настроек",
            f"Бекап настроек создан.\n\nФайл:\n{backup_path}",
        )

    def create_main_db_backup(self):
        from rem_card.app.backup_and_cleanup import create_manual_primary_db_backup
        from rem_card.ui.shared.custom_message_box import CustomMessageBox

        loading_key = self._show_settings_loading("Создание бекапа основной БД...", key="main-db-backup", auto_hide_ms=300000)
        try:
            backup_path = create_manual_primary_db_backup()
        except Exception as exc:
            CustomMessageBox.warning(self, "Бекап основной БД", f"Не удалось создать бекап основной БД:\n{exc}")
            return
        finally:
            self._hide_settings_loading(loading_key)
        CustomMessageBox.information(
            self,
            "Бекап основной БД",
            f"Бекап основной БД создан.\n\nФайл:\n{backup_path}",
        )

    def open_dev_database_switch(self):
        from rem_card.app.runtime_paths import is_compiled
        from rem_card.ui.shared.custom_message_box import CustomMessageBox

        if is_compiled():
            CustomMessageBox.warning(
                self,
                "Смена базы",
                "Смена базы через настройки доступна только в dev-версии.",
            )
            return

        page = self._ensure_dev_database_switch_page()
        page._reload_saved_paths(select_path=page.current_path)
        self._show_page(page)

    def _on_dev_database_switch_applied(self):
        import os

        from rem_card.app.runtime_paths import DEV_RUNTIME_BAZA_PIN_ENV
        from rem_card.ui.shared.custom_message_box import CustomMessageBox

        dialog = self.dev_database_switch_dialog
        if dialog is None:
            return
        self.show_menu()
        if dialog.environment_override and dialog.active_changed:
            CustomMessageBox.warning(
                self,
                "Смена базы",
                "Путь сохранён, но сейчас база задаётся переменной окружения. "
                "Сохранённый выбор начнёт применяться после удаления этой переменной "
                "из конфигурации запуска.",
            )
            return

        if not dialog.active_changed:
            CustomMessageBox.information(
                self,
                "Смена базы",
                "Этот путь уже используется. Он сохранён в списке баз.",
            )
            return

        # The selected path is already persisted for the next launch. Pin all
        # dynamic path lookups in this process to its current database until
        # shutdown, so declining an immediate restart cannot mix two bases.
        os.environ["REMCARD_BAZA_DIR"] = dialog.current_path
        os.environ[DEV_RUNTIME_BAZA_PIN_ENV] = str(os.getpid())

        answer = CustomMessageBox.question(
            self,
            "Смена базы",
            f"Новая база сохранена:\n{dialog.selected_path}\n\n"
            "Для безопасного переключения нужно полностью перезапустить dev-версию. "
            "Перезапустить сейчас?",
        )
        if answer != CustomMessageBox.Yes:
            CustomMessageBox.information(
                self,
                "Смена базы",
                "Новая база будет подключена при следующем запуске dev-версии.",
            )
            return

        app = QApplication.instance()
        if app is not None:
            app.setProperty("remcard_restart_requested", True)
            top_level = self.window()
            if top_level is not None and top_level is not self:
                closed = bool(top_level.close())
                waiting_for_draft = bool(
                    getattr(top_level, "_orders_draft_close_waiting", False)
                )
                if not closed and not waiting_for_draft:
                    app.setProperty("remcard_restart_requested", False)
            else:
                app.quit()

    def open_settings_import(self):
        from rem_card.services.settings.settings_service import get_settings_service
        from rem_card.ui.shared.custom_message_box import CustomMessageBox
        from .settings_import_dialog import SettingsImportPathDialog

        worker = self._settings_import_worker
        if worker is not None:
            return

        answer = CustomMessageBox.question(
            self,
            "Загрузить настройки",
            "Загрузить настройки из сетевой базы в dev-версию?\n\n"
            "Изменения будут применены только для отмеченных строк.",
        )
        if answer != CustomMessageBox.Yes:
            return

        path_dialog = SettingsImportPathDialog(parent=self)
        self._prepare_settings_surface(path_dialog)
        if path_dialog.exec() != QDialog.Accepted:
            return

        settings_service = get_settings_service()
        source_path = str(path_dialog.selected_path)
        self._start_settings_import_stage(
            lambda: settings_service.preview_external_settings_import(source_path),
            message="Загрузка настроек из базы...",
            key="settings-import-preview",
            error_message="Не удалось загрузить настройки",
            on_success=lambda preview: self._on_settings_import_preview_ready(settings_service, preview),
        )

    def _start_settings_import_stage(
        self,
        operation,
        *,
        message: str,
        key: str,
        error_message: str,
        on_success,
    ) -> None:
        from rem_card.ui.shared.async_call import AsyncCallThread

        active_worker = self._settings_import_worker
        if active_worker is not None:
            return

        loading_key = self._show_settings_loading(message, key=key, auto_hide_ms=300000)
        worker = AsyncCallThread(operation)
        worker._settings_import_loading_key = loading_key
        worker._settings_import_error_message = str(error_message)
        worker._settings_import_on_success = on_success
        self._settings_import_worker = worker
        self._settings_import_loading_key = loading_key
        self.btn_import_settings.setEnabled(False)
        self.menu_widget.setEnabled(False)
        worker.succeeded.connect(self._on_settings_import_worker_succeeded, Qt.QueuedConnection)
        worker.failed.connect(self._on_settings_import_worker_failed, Qt.QueuedConnection)
        worker.finished.connect(self._on_settings_import_worker_finished, Qt.QueuedConnection)
        try:
            worker.start()
        except Exception as exc:
            self._handle_settings_import_stage_failed(worker, exc)

    def _finish_settings_import_stage(self, worker, loading_key: str | None) -> bool:
        if self._settings_import_worker is not worker:
            return False
        self._settings_import_worker = None
        self._settings_import_loading_key = None
        self.btn_import_settings.setEnabled(True)
        self.menu_widget.setEnabled(True)
        self._hide_settings_loading(loading_key)
        return True

    def _on_settings_import_worker_succeeded(self, result) -> None:
        worker = self.sender()
        if worker is None:
            return
        loading_key = getattr(worker, "_settings_import_loading_key", None)
        on_success = getattr(worker, "_settings_import_on_success", None)
        if not self._finish_settings_import_stage(worker, loading_key):
            return
        if self._settings_import_ui_is_closing():
            return
        if callable(on_success):
            on_success(result)

    def _on_settings_import_worker_failed(self, exc: Exception) -> None:
        worker = self.sender()
        if worker is not None:
            self._handle_settings_import_stage_failed(worker, exc)

    def _handle_settings_import_stage_failed(self, worker, exc: Exception) -> None:
        from rem_card.ui.shared.custom_message_box import CustomMessageBox

        loading_key = getattr(worker, "_settings_import_loading_key", None)
        error_message = str(getattr(worker, "_settings_import_error_message", "Не удалось выполнить операцию"))
        if not self._finish_settings_import_stage(worker, loading_key):
            return
        if self._settings_import_ui_is_closing():
            return
        CustomMessageBox.warning(self, "Загрузить настройки", f"{error_message}:\n{exc}")

    def _on_settings_import_worker_finished(self) -> None:
        worker = self.sender()
        if worker is not None:
            self._finish_settings_import_stage(
                worker,
                getattr(worker, "_settings_import_loading_key", None),
            )

    def _settings_import_ui_is_closing(self) -> bool:
        if QCoreApplication.closingDown():
            return True
        try:
            window = self.window()
            if bool(getattr(window, "_is_closing", False)):
                return True
            return bool(window is not self and window.isVisible() and not self.isVisible())
        except RuntimeError:
            return True

    def _on_settings_import_preview_ready(self, settings_service, preview) -> None:
        from rem_card.ui.shared.custom_message_box import CustomMessageBox
        from .settings_import_dialog import SettingsImportPreviewDialog

        if not preview.changes:
            CustomMessageBox.information(self, "Загрузить настройки", "Отличий между dev и выбранной БД не найдено.")
            return

        self.settings_import_dialog = SettingsImportPreviewDialog(preview, parent=self)
        self._prepare_settings_surface(self.settings_import_dialog)
        if self.settings_import_dialog.exec() != QDialog.Accepted:
            return
        selected_ids = self.settings_import_dialog.selected_change_ids()
        source_db_path = str(preview.source_db_path)
        selected_ids = tuple(selected_ids)
        self._start_settings_import_stage(
            lambda: settings_service.apply_external_settings_import(source_db_path, selected_ids),
            message="Применение настроек...",
            key="settings-import-apply",
            error_message="Не удалось применить настройки",
            on_success=self._on_settings_import_applied,
        )

    def _on_settings_import_applied(self, report) -> None:
        from rem_card.ui.shared.custom_message_box import CustomMessageBox

        loading_key = self._show_settings_loading("Обновление настроек интерфейса...", key="settings-import-refresh", auto_hide_ms=30000)
        self._refresh_after_settings_import()
        self._hide_settings_loading(loading_key, delay_ms=250)
        counts = report.get("counts") or {}
        CustomMessageBox.information(
            self,
            "Загрузить настройки",
            "Настройки загружены.\n\n"
            f"Добавлено: {int(counts.get('insert') or 0)}\n"
            f"Обновлено: {int(counts.get('update') or 0)}\n"
            f"Удалено из dev: {int(counts.get('delete') or 0)}",
        )

    def _refresh_after_settings_import(self):
        try:
            from rem_card.ui.shared.background_settings import invalidate_background_settings_cache

            invalidate_background_settings_cache()
        except Exception:
            pass
        try:
            from rem_card.ui.shared.operblock_icon_settings import invalidate_operblock_icon_cache

            invalidate_operblock_icon_cache()
        except Exception:
            pass
        try:
            from rem_card.ui.shared.remcard_icon_settings import invalidate_remcard_icon_cache

            invalidate_remcard_icon_cache()
        except Exception:
            pass
        try:
            from rem_card.ui.styles.theme_manager import get_theme_manager

            get_theme_manager().load()
        except Exception:
            pass

    def _resolve_db_manager(self):
        candidates = [
            ("orders_dao", "db"),
            ("patient_dao", "db"),
            ("vitals_dao", "db"),
            ("data_service", "db"),
        ]
        for outer_attr, inner_attr in candidates:
            owner = getattr(self.service, outer_attr, None)
            candidate = getattr(owner, inner_attr, None)
            if candidate is not None:
                return candidate
        candidate = getattr(self.service, "db_manager", None)
        if candidate is not None:
            return candidate
        raise RuntimeError("Менеджер БД недоступен.")

    def _on_db_rotated(self):
        data_service = getattr(self.service, "data_service", None)
        if data_service and hasattr(data_service, "request_immediate_refresh"):
            data_service.request_immediate_refresh(
                force_emit=True,
                source="database_rotation:admin",
            )

    def set_print_context(self, service, admission_id, date):
        self.service = service
        self._pending_print_context = (service, admission_id, date)
        if self.diet_templates_widget is not None:
            self.diet_templates_widget.set_service(service)
        if self.lab_analysis_catalog_widget is not None:
            self.lab_analysis_catalog_widget.set_service(service)
        if self.print_widget is not None:
            self.print_widget.set_context(service, admission_id, date)
        if self.print_dialog is not None:
            self.print_dialog.set_context(service, admission_id, date)

    def show_menu(self):
        self.stack.setCurrentWidget(self.menu_widget)

    def go_back(self) -> bool:
        """Возвращает на предыдущий экран настроек, если он есть."""
        if self.stack.currentWidget() is self.menu_widget:
            return False
        self.show_menu()
        return True
