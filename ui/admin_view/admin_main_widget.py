from PySide6.QtCore import Qt
from PySide6.QtWidgets import QDialog, QHBoxLayout, QLabel, QPushButton, QStackedWidget, QVBoxLayout, QWidget


class AdminMainWidget(QWidget):
    """
    Админ-панель с ленивой загрузкой страниц.
    Создаем только меню сразу, а тяжелые словари и печать — по запросу.
    """

    def __init__(self, service=None, role="admin", parent=None):
        super().__init__(parent)
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
        self.operblock_medications_dialog = None
        self.operblock_route_settings_widget = None
        self.operblock_anesthesia_types_dialog = None
        self.operblock_team_dialog = None
        self.emergency_password_dialog = None
        self.db_rotation_dialog = None

        self.setup_ui()

    def setup_ui(self):
        main_layout = QVBoxLayout(self)
        main_layout.setContentsMargins(0, 0, 0, 0)

        self.stack = QStackedWidget()
        main_layout.addWidget(self.stack)

        self.menu_widget = QWidget()
        menu_layout = QVBoxLayout(self.menu_widget)
        menu_layout.setContentsMargins(28, 24, 28, 20)
        menu_layout.setSpacing(18)

        title = QLabel("Панель Администратора")
        title.setProperty("heading", "true")
        title.setAlignment(Qt.AlignLeft | Qt.AlignVCenter)
        menu_layout.addWidget(title)

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
        self.btn_operblock_icon_settings = QPushButton("Настройка иконок оперблока")
        self.btn_operblock_medications = QPushButton("Настройки препаратов")
        self.btn_operblock_routes = QPushButton("Оперблок - путь введения")
        self.btn_operblock_anesthesia_types = QPushButton("Виды пособия")
        self.btn_operblock_team = QPushButton("Опер. бригада")
        self.btn_emergency_password = QPushButton("Аварийный пароль")
        self.btn_db_rotation = QPushButton("Ручная ротация БД")

        def prepare_button(btn: QPushButton):
            btn.setObjectName("DialogOkBtn")
            btn.setMinimumSize(250, 44)
            btn.setMaximumWidth(300)
            return btn

        drug_buttons = [
            self.btn_drugs,
            self.btn_groups,
            self.btn_forms,
            self.btn_admin_types,
            self.btn_diluents,
        ]
        template_buttons = [
            self.btn_templates,
            self.btn_doctor_list,
        ]
        if self.role != "nurse":
            template_buttons.insert(1, self.btn_lab_analysis_catalog)
            template_buttons.append(self.btn_diet_templates)

        self.btn_style.setVisible(False)
        program_buttons = [self.btn_print, self.btn_display_settings, self.btn_background_settings]
        if self.role == "doctor":
            program_buttons.append(self.btn_emergency_password)
            program_buttons.append(self.btn_db_rotation)
        operblock_buttons = [
            self.btn_operblock_icon_settings,
            self.btn_operblock_medications,
            self.btn_operblock_routes,
            self.btn_operblock_anesthesia_types,
            self.btn_operblock_team,
        ]

        columns_layout = QHBoxLayout()
        columns_layout.setSpacing(22)
        columns_layout.setAlignment(Qt.AlignLeft | Qt.AlignTop)

        def add_column(column_title: str, buttons: list[QPushButton]):
            column = QWidget()
            column_layout = QVBoxLayout(column)
            column_layout.setContentsMargins(0, 0, 0, 0)
            column_layout.setSpacing(10)
            lbl = QLabel(column_title)
            column_layout.addWidget(lbl)
            for btn in buttons:
                column_layout.addWidget(prepare_button(btn))
            column_layout.addStretch()
            columns_layout.addWidget(column)

        add_column("Препараты", drug_buttons)
        add_column("Шаблоны", template_buttons)
        add_column("Настройка программы", program_buttons)
        if self.role != "nurse":
            add_column("Оперблок", operblock_buttons)
        columns_layout.addStretch()

        menu_layout.addLayout(columns_layout, 1)
        menu_layout.addStretch()

        nav_layout = QHBoxLayout()
        self.btn_back_to_roles = QPushButton("← Назад")
        self.btn_back_to_roles.setObjectName("DialogOkBtn")
        self.btn_back_to_roles.setFixedSize(250, 40)
        nav_layout.addWidget(self.btn_back_to_roles)
        nav_layout.addStretch()
        menu_layout.addLayout(nav_layout)

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
        self.btn_operblock_icon_settings.clicked.connect(self.open_operblock_icon_settings)
        self.btn_operblock_medications.clicked.connect(self.open_operblock_medications_settings)
        self.btn_operblock_routes.clicked.connect(self.open_operblock_route_settings)
        self.btn_operblock_anesthesia_types.clicked.connect(self.open_operblock_anesthesia_types_settings)
        self.btn_operblock_team.clicked.connect(self.open_operblock_team_settings)
        self.btn_emergency_password.clicked.connect(self.open_emergency_password)
        self.btn_db_rotation.clicked.connect(self.open_db_rotation)

    def _show_page(self, widget):
        if widget is not None:
            try:
                from rem_card.services.prescription_engine import engine

                engine.reload_if_changed(force_check=True)
            except Exception:
                pass
            load_data = getattr(widget, "load_data", None)
            if callable(load_data):
                load_data()
            self.stack.setCurrentWidget(widget)

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

    def _ensure_operblock_route_settings_widget(self):
        if self.operblock_route_settings_widget is None:
            from .operblock_route_settings_widget import OperBlockRouteSettingsWidget

            self.operblock_route_settings_widget = self._connect_back(OperBlockRouteSettingsWidget())
            self.stack.addWidget(self.operblock_route_settings_widget)
        return self.operblock_route_settings_widget

    def _ensure_print_dialog(self):
        if self.print_dialog is None:
            from .print_settings_widget import PrintSettingsDialog

            self.print_dialog = PrintSettingsDialog(parent=self)
            if self._pending_print_context is not None:
                self.print_dialog.set_context(*self._pending_print_context)
        return self.print_dialog

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
        if self.doctor_list_dialog is None:
            from .doctor_list_dialog import DoctorListDialog

            self.doctor_list_dialog = DoctorListDialog(parent=self)
        self.doctor_list_dialog.show()
        self.doctor_list_dialog.raise_()
        self.doctor_list_dialog.activateWindow()

    def open_diet_templates(self):
        self._show_page(self._ensure_diet_templates_widget())

    def open_print(self):
        dialog = self._ensure_print_dialog()
        if self._pending_print_context is not None:
            dialog.set_context(*self._pending_print_context)
        dialog.load_settings()
        dialog.show()
        dialog.raise_()
        dialog.activateWindow()

    def open_style(self):
        from rem_card.ui.styles.theme_settings_dialog import ThemeSettingsDialog

        role = self.role if self.role in ("doctor", "nurse") else "doctor"
        dialog = ThemeSettingsDialog(role=role, parent=self)
        dialog.exec()

    def open_display_settings(self):
        from .display_settings_dialog import DisplaySettingsDialog

        dialog = DisplaySettingsDialog(initial_role=self.role, parent=self)
        dialog.exec()

    def open_background_settings(self):
        from .background_settings_dialog import BackgroundSettingsDialog

        dialog = BackgroundSettingsDialog(parent=self)
        dialog.exec()

    def open_operblock_icon_settings(self):
        from .operblock_icon_settings_dialog import OperBlockIconSettingsDialog

        self.operblock_icon_settings_dialog = OperBlockIconSettingsDialog(parent=self)
        self.operblock_icon_settings_dialog.exec()

    def open_operblock_medications_settings(self):
        from rem_card.services.operblock_medication_presets import (
            load_operblock_medication_presets,
            save_operblock_medication_presets,
        )
        from rem_card.ui.operblock_view.operblock_main_widget import OperBlockMedicationPresetsDialog
        from rem_card.ui.shared.custom_message_box import CustomMessageBox

        try:
            presets = load_operblock_medication_presets(include_disabled=True)
        except Exception as exc:
            CustomMessageBox.warning(self, "Настройки препаратов", f"Не удалось загрузить препараты оперблока: {exc}")
            return
        self.operblock_medications_dialog = OperBlockMedicationPresetsDialog(
            presets,
            parent=self,
            save_handler=save_operblock_medication_presets,
        )
        self.operblock_medications_dialog.exec()

    def open_operblock_route_settings(self):
        self._show_page(self._ensure_operblock_route_settings_widget())

    def open_operblock_anesthesia_types_settings(self):
        from rem_card.services.operblock_anesthesia_types import (
            load_operblock_anesthesia_types,
            save_operblock_anesthesia_types,
        )
        from rem_card.ui.operblock_view.operblock_main_widget import OperBlockAnesthesiaTypesDialog
        from rem_card.ui.shared.custom_message_box import CustomMessageBox

        try:
            items = load_operblock_anesthesia_types()
        except Exception as exc:
            CustomMessageBox.warning(self, "Виды пособия", f"Не удалось загрузить виды пособия: {exc}")
            return
        self.operblock_anesthesia_types_dialog = OperBlockAnesthesiaTypesDialog(items, parent=self)
        if self.operblock_anesthesia_types_dialog.exec() != QDialog.Accepted:
            return
        try:
            save_operblock_anesthesia_types(self.operblock_anesthesia_types_dialog.items())
        except Exception as exc:
            CustomMessageBox.warning(self, "Виды пособия", f"Не удалось сохранить виды пособия: {exc}")

    def open_operblock_team_settings(self):
        from rem_card.services.operblock_team import load_operblock_team, save_operblock_team
        from rem_card.ui.operblock_view.operblock_main_widget import OperBlockTeamDialog
        from rem_card.ui.shared.custom_message_box import CustomMessageBox

        try:
            items = load_operblock_team()
        except Exception as exc:
            CustomMessageBox.warning(self, "Опер. бригада", f"Не удалось загрузить опер. бригаду: {exc}")
            return
        self.operblock_team_dialog = OperBlockTeamDialog(items, parent=self)
        if self.operblock_team_dialog.exec() != QDialog.Accepted:
            return
        try:
            save_operblock_team(self.operblock_team_dialog.items())
        except Exception as exc:
            CustomMessageBox.warning(self, "Опер. бригада", f"Не удалось сохранить опер. бригаду: {exc}")

    def open_emergency_password(self):
        from .emergency_password_dialog import EmergencyPasswordSettingsDialog

        self.emergency_password_dialog = EmergencyPasswordSettingsDialog(parent=self)
        self.emergency_password_dialog.exec()

    def open_db_rotation(self):
        from .db_rotation_dialog import DbRotationDialog
        from rem_card.ui.shared.custom_message_box import CustomMessageBox

        try:
            db_manager = self._resolve_db_manager()
        except Exception as exc:
            CustomMessageBox.warning(self, "Ротация БД", f"Не удалось открыть управление БД:\n{exc}")
            return
        self.db_rotation_dialog = DbRotationDialog(
            db_manager,
            parent=self,
            on_rotated=self._on_db_rotated,
        )
        self.db_rotation_dialog.exec()

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
            data_service.request_immediate_refresh(force_emit=True)

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
