from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QButtonGroup, QCheckBox, QFrame, QHBoxLayout, QLabel, QPushButton, QScrollArea, QSizePolicy, QStackedWidget, QVBoxLayout, QWidget

from rem_card.ui.shared.archive_date_edit import ArchiveDateEdit


class ArchiveAnalysisPage(QWidget):
    """Full-height host for structured analytics, separate from legacy tables."""

    def __init__(self, statistics_page, parent=None):
        super().__init__(parent)
        self.statistics_page = statistics_page
        self.setObjectName("ArchiveAnalysisPage")

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)
        self.scroll = QScrollArea(self)
        self.scroll.setObjectName("ArchiveAnalysisScroll")
        self.scroll.setWidgetResizable(True)
        self.scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        body = QWidget(self.scroll)
        body.setObjectName("ArchiveAnalysisBody")
        body.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)
        layout = QVBoxLayout(body)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(10)

        toolbar = QFrame(self)
        toolbar.setObjectName("AnalyticsSection")
        toolbar_layout = QVBoxLayout(toolbar)
        toolbar_layout.setContentsMargins(12, 10, 12, 10)
        toolbar_layout.setSpacing(7)
        title = QLabel("1. Кого и за какой период анализируем", toolbar)
        title.setObjectName("AnalyticsStepTitle")
        toolbar_layout.addWidget(title)
        row = QHBoxLayout()
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(8)
        row.addWidget(QLabel("Основной период", toolbar))
        row.addWidget(QLabel("С", toolbar))
        self.date_from = ArchiveDateEdit(statistics_page.date_from.date(), toolbar)
        self.date_from.setFixedWidth(132)
        row.addWidget(self.date_from)
        row.addWidget(QLabel("По", toolbar))
        self.date_to = ArchiveDateEdit(statistics_page.date_to.date(), toolbar)
        self.date_to.setFixedWidth(132)
        row.addWidget(self.date_to)
        self.include_recovery = QCheckBox("Учитывать койки пробуждения", toolbar)
        self.include_recovery.setObjectName("ArchiveRecoveryToggle")
        self.include_recovery.setChecked(statistics_page.chk_include_recovery.isChecked())
        self.include_recovery.setVisible(not statistics_page.is_operblock)
        row.addWidget(self.include_recovery)
        self.status = QLabel("", toolbar)
        self.status.setObjectName("ArchiveStatisticsStatus")
        row.addWidget(self.status)
        row.addStretch(1)
        toolbar_layout.addLayout(row)
        layout.addWidget(toolbar)

        workspace = statistics_page.analytics_workspace
        workspace.setParent(body)
        workspace.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)
        layout.addWidget(workspace, 1)
        self.scroll.setWidget(body)
        outer.addWidget(self.scroll)

        self.date_from.dateChanged.connect(statistics_page.date_from.setDate)
        self.date_to.dateChanged.connect(statistics_page.date_to.setDate)
        statistics_page.date_from.dateChanged.connect(self.date_from.setDate)
        statistics_page.date_to.dateChanged.connect(self.date_to.setDate)
        self.include_recovery.toggled.connect(statistics_page.chk_include_recovery.setChecked)
        statistics_page.chk_include_recovery.toggled.connect(self.include_recovery.setChecked)
        statistics_page.status_changed.connect(self.status.setText)

    @property
    def analytics_workspace(self):
        return self.statistics_page.analytics_workspace

    def ensure_loaded(self):
        if self.analytics_workspace.snapshot is None:
            self.statistics_page.refresh_analysis()


class UnifiedArchiveAnalysisPage(QWidget):
    """One analytics destination with an internal RAO/operblock scope switch."""

    def __init__(self, rao_statistics, operblock_statistics, parent=None):
        super().__init__(parent)
        self.setObjectName("UnifiedArchiveAnalysisPage")
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(10)

        switcher = QFrame(self)
        switcher.setObjectName("ArchiveStatisticsToolbar")
        row = QHBoxLayout(switcher)
        row.setContentsMargins(12, 8, 12, 8)
        row.setSpacing(7)
        label = QLabel("Область анализа", switcher)
        label.setObjectName("AnalyticsSectionTitle")
        row.addWidget(label)
        self.scope_group = QButtonGroup(self)
        self.scope_group.setExclusive(True)
        self.btn_rao = QPushButton("Реанимация", switcher)
        self.btn_operblock = QPushButton("Оперблок", switcher)
        for index, button in enumerate((self.btn_rao, self.btn_operblock)):
            button.setObjectName("ArchivePageButton")
            button.setCheckable(True)
            self.scope_group.addButton(button, index)
            button.clicked.connect(lambda _checked=False, page=index: self.select_scope(page))
            row.addWidget(button)
        row.addStretch(1)
        layout.addWidget(switcher)

        self.stack = QStackedWidget(self)
        self.rao_page = ArchiveAnalysisPage(rao_statistics, self.stack)
        self.operblock_page = ArchiveAnalysisPage(operblock_statistics, self.stack)
        self.stack.addWidget(self.rao_page)
        self.stack.addWidget(self.operblock_page)
        layout.addWidget(self.stack, 1)
        self.btn_rao.setChecked(True)
        self.stack.setCurrentIndex(0)

    def select_scope(self, index: int):
        index = max(0, min(int(index), self.stack.count() - 1))
        self.stack.setCurrentIndex(index)
        self.scope_group.button(index).setChecked(True)
        self.stack.currentWidget().ensure_loaded()

    def ensure_loaded(self):
        self.stack.currentWidget().ensure_loaded()
