from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QButtonGroup,
    QFrame,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QSizePolicy,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)

from rem_card.ui.doctor_view.archive_widget import ARCHIVE_MODE_OPERBLOCK, ARCHIVE_MODE_RAO, ArchiveWidget
from rem_card.ui.styles.admin_settings_styles import build_admin_settings_style
from rem_card.ui.styles.archive_center_styles import build_archive_center_style
from rem_card.ui.styles.theme_manager import get_theme_manager

from .statistics_page import ArchiveStatisticsPage
from .graphs_page import ArchiveGraphsPage


class ArchiveMainWidget(QWidget):
    """Единая оболочка архива с независимыми страницами РАО и оперблока."""

    patient_selected = Signal(object)
    operblock_case_selected = Signal(object)
    edit_requested = Signal(object)
    delete_requested = Signal(object)
    back_requested = Signal()

    def __init__(
        self,
        patient_service,
        remcard_service=None,
        parent=None,
        *,
        role: str = "doctor",
        allow_edit: bool = False,
        operblock_service=None,
        initial_destination: int = 0,
        allow_rao_edit: bool | None = None,
        allow_operblock_edit: bool | None = None,
    ):
        super().__init__(parent)
        self.setObjectName("ArchiveCenter")
        self.patient_service = patient_service
        self.remcard_service = remcard_service
        self.operblock_service = operblock_service
        self.role = str(role or "doctor")
        self.allow_edit = bool(allow_edit)
        self.allow_rao_edit = self.allow_edit if allow_rao_edit is None else bool(allow_rao_edit)
        self.allow_operblock_edit = self.allow_edit if allow_operblock_edit is None else bool(allow_operblock_edit)
        self._initial_destination = int(initial_destination)
        self._active_index = 0
        self._init_ui()

    @property
    def all_archived_patients(self):
        return list(getattr(self._active_archive_page(), "all_archived_patients", []) or [])

    @property
    def filtered_patients(self):
        return list(getattr(self._active_archive_page(), "filtered_patients", []) or [])

    def _init_ui(self):
        page_layout = QVBoxLayout(self)
        # Врачебная и сестринская оболочки уже дают архиву левый зазор соседним
        # сектором. В оперблоке центр архива занимает весь stack, поэтому ему
        # нужен собственный левый отступ, симметричный правому.
        left_outer_margin = 5 if self.role == "operblock" else 0
        page_layout.setContentsMargins(left_outer_margin, 5, 5, 4)
        page_layout.setSpacing(0)

        self.surface_frame = QFrame(self)
        self.surface_frame.setObjectName("ArchiveCenterFrame")
        page_layout.addWidget(self.surface_frame)

        outer = QHBoxLayout(self.surface_frame)
        # Дочерние фоны не перекрывают скруглённый контур родительского frame.
        outer.setContentsMargins(2, 2, 2, 2)
        outer.setSpacing(0)

        sidebar = QFrame()
        sidebar.setObjectName("ArchiveCenterSidebar")
        sidebar.setFixedWidth(264)
        sidebar_layout = QVBoxLayout(sidebar)
        sidebar_layout.setContentsMargins(20, 22, 20, 18)
        sidebar_layout.setSpacing(8)
        sidebar_layout.addWidget(self._brand_card())
        sidebar_layout.addSpacing(18)

        self.navigation_group = QButtonGroup(self)
        self.navigation_group.setExclusive(True)
        self.navigation_buttons = []
        for index, label in enumerate(
            (
                "Архив реанимации",
                "Архив оперблока",
                "Статистика РАО",
                "Графики реанимации",
                "Статистика оперблока",
            )
        ):
            button = QPushButton(label)
            button.setObjectName("SettingsNavButton")
            button.setCheckable(True)
            button.setCursor(Qt.PointingHandCursor)
            button.setMinimumHeight(42)
            button.clicked.connect(lambda _checked=False, page=index: self.select_destination(page))
            self.navigation_group.addButton(button, index)
            self.navigation_buttons.append(button)
            sidebar_layout.addWidget(button)
        sidebar_layout.addStretch(1)
        outer.addWidget(sidebar)

        content = QWidget()
        content.setObjectName("ArchiveCenterContent")
        content_layout = QVBoxLayout(content)
        content_layout.setContentsMargins(34, 26, 34, 24)
        content_layout.setSpacing(0)
        header = QHBoxLayout()
        text_block = QVBoxLayout()
        text_block.setSpacing(3)
        self.page_title = QLabel("Архив пациентов реанимации")
        self.page_title.setObjectName("SettingsPageTitle")
        text_block.addWidget(self.page_title)
        header.addLayout(text_block, 1)
        badge = QLabel(self._role_badge_text())
        badge.setObjectName("ArchiveCenterRoleBadge")
        badge.setAlignment(Qt.AlignCenter)
        header.addWidget(badge, 0, Qt.AlignTop)
        content_layout.addLayout(header)
        content_layout.addSpacing(20)

        self.content_stack = QStackedWidget()
        self.content_stack.setObjectName("ArchiveCenterStack")
        # Скрытые страницы не должны навязывать всему RemCard свою широкую
        # minimumSizeHint: именно это обрезало правые углы архива.
        self.content_stack.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Expanding)
        self.rao_archive = ArchiveWidget(
            self.patient_service,
            remcard_service=self.remcard_service,
            allow_edit=self.allow_rao_edit,
            operblock_service=self.operblock_service,
            fixed_source_mode=ARCHIVE_MODE_RAO,
            embedded=True,
        )
        self.operblock_archive = ArchiveWidget(
            self.patient_service,
            remcard_service=self.remcard_service,
            allow_edit=self.allow_operblock_edit,
            operblock_service=self.operblock_service,
            fixed_source_mode=ARCHIVE_MODE_OPERBLOCK,
            embedded=True,
        )
        self.rao_statistics = ArchiveStatisticsPage(
            source_mode=ARCHIVE_MODE_RAO,
            remcard_service=self.remcard_service,
            operblock_service=self.operblock_service,
            archive_page=self.rao_archive,
        )
        self.rao_graphs = ArchiveGraphsPage(
            remcard_service=self.remcard_service,
            archive_page=self.rao_archive,
        )
        self.operblock_statistics = ArchiveStatisticsPage(
            source_mode=ARCHIVE_MODE_OPERBLOCK,
            remcard_service=self.remcard_service,
            operblock_service=self.operblock_service,
            archive_page=self.operblock_archive,
        )
        for page in (
            self.rao_archive,
            self.operblock_archive,
            self.rao_statistics,
            self.rao_graphs,
            self.operblock_statistics,
        ):
            self.content_stack.addWidget(page)
        content_layout.addWidget(self.content_stack, 1)
        outer.addWidget(content, 1)

        for archive in (self.rao_archive, self.operblock_archive):
            archive.patient_selected.connect(self.patient_selected)
            archive.operblock_case_selected.connect(self.operblock_case_selected)
            archive.edit_requested.connect(self.edit_requested)
            archive.delete_requested.connect(self.delete_requested)

        tokens = get_theme_manager().current_tokens()
        self.setStyleSheet(build_admin_settings_style(tokens) + build_archive_center_style(tokens))
        # Начальная страница выбирается синхронно. Загрузка архива запускается
        # менеджером компоновки после добавления виджета в рабочий stack.
        initial_index = max(0, min(self._initial_destination, self.content_stack.count() - 1))
        self._apply_destination(initial_index, load=False)

    def _role_badge_text(self) -> str:
        return {
            "doctor": "Врач",
            "nurse": "Медсестра",
            "operblock": "Оперблок",
        }.get(self.role, "Врач" if self.allow_edit else "Медсестра")

    def _brand_card(self) -> QFrame:
        card = QFrame()
        card.setObjectName("SettingsBrandCard")
        row = QHBoxLayout(card)
        row.setContentsMargins(12, 11, 12, 11)
        # Полностью повторяем бренд-блок центра управления: между синей
        # меткой R и текстом RemCard там используется зазор 10 px.
        row.setSpacing(10)
        mark = QLabel("R")
        mark.setObjectName("SettingsBrandMark")
        mark.setAlignment(Qt.AlignCenter)
        mark.setFixedSize(38, 38)
        labels = QVBoxLayout()
        labels.setSpacing(0)
        title = QLabel("RemCard")
        title.setObjectName("SettingsBrandTitle")
        subtitle = QLabel("Архив пациентов")
        subtitle.setObjectName("SettingsMutedLabel")
        labels.addWidget(title)
        labels.addWidget(subtitle)
        row.addWidget(mark)
        row.addLayout(labels, 1)
        return card

    def select_destination(self, index: int):
        self._apply_destination(index, load=True)

    def _apply_destination(self, index: int, *, load: bool):
        index = max(0, min(int(index), self.content_stack.count() - 1))
        self._active_index = index
        self.content_stack.setCurrentIndex(index)
        self.navigation_buttons[index].setChecked(True)
        self.page_title.setText(
            (
                "Архив пациентов реанимации",
                "Архив пациентов оперблока",
                "Статистика реанимации",
                "Графики реанимации",
                "Статистика оперблока",
            )[index]
        )
        if not load:
            return
        if index in (0, 1):
            self._active_archive_page().load_data(reset_page=False)
        else:
            self.content_stack.currentWidget().ensure_loaded()

    def _active_archive_page(self):
        return self.operblock_archive if self._active_index in (1, 4) else self.rao_archive

    def load_data(self, reset_page: bool = False):
        """Совместимый вход для существующих менеджеров компоновки."""
        if self._active_index in (0, 1):
            self._active_archive_page().load_data(reset_page=reset_page)

    def shutdown(self):
        self.rao_statistics.shutdown()
        self.rao_graphs.shutdown()
        self.operblock_statistics.shutdown()
