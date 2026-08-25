from __future__ import annotations

from rem_card.ui.styles.theme import FORM_DROPDOWN_ARROW_IMAGE
from rem_card.ui.styles.theme_tokens import token


def build_archive_center_style(tokens: dict[str, str]) -> str:
    """Небольшое theme-aware дополнение к общему стилю центра настроек."""

    def t(key: str, default: str = "") -> str:
        return token(tokens, key, default)

    return f"""
QWidget#ArchiveCenter, QWidget#ArchiveCenterContent {{
    background-color: {t("surface.window")};
}}
QFrame#ArchiveCenterFrame {{
    background-color: {t("surface.window")};
    border: 1.5px solid {t("sector.border")};
    border-radius: 5px;
}}
QWidget#ArchiveCenterContent {{
    border: none;
    border-top-right-radius: 4px;
    border-bottom-right-radius: 4px;
}}
QFrame#ArchiveCenterSidebar {{
    background-color: {t("surface.subtle")};
    border: none;
    border-right: 1px solid {t("border.subtle")};
    border-top-left-radius: 4px;
    border-bottom-left-radius: 4px;
}}
QLabel#ArchiveCenterRoleBadge, QLabel#ArchiveStatisticsScope {{
    background-color: {t("surface.panel")};
    color: {t("text.secondary")};
    border: 1px solid {t("border.subtle")};
    border-radius: {t("radius.md")};
    padding: 6px 10px;
    font-size: 12px;
    font-weight: 650;
}}
QFrame#ArchiveFiltersFrame,
QFrame#ArchiveStatisticsToolbar,
QFrame#ArchiveStatisticsSelector,
QFrame#ArchivePaginationBar,
QFrame#ArchiveActionsBar {{
    background-color: {t("surface.card")};
    border: 1px solid {t("border.subtle")};
    border-radius: {t("radius.md")};
}}
QFrame#ArchiveDataPanel {{
    background-color: transparent;
    border: none;
}}
QLineEdit#ArchiveFilterField,
QComboBox#ArchiveTableFilter,
QDateEdit#ArchiveDateEdit,
QComboBox#AnalyticsMetricSelector,
QComboBox#AnalyticsCohortField,
QComboBox#AnalyticsCohortOperator,
QComboBox#AnalyticsComparisonMode,
QComboBox#AnalyticsSavedViews,
QLineEdit#AnalyticsCohortValue,
QLineEdit#AnalyticsViewName,
QDateEdit#AnalyticsPeriodBFrom,
QDateEdit#AnalyticsPeriodBTo {{
    background-color: {t("field.bg")};
    color: {t("field.text")};
    border: 1px solid {t("field.border")};
    border-radius: {t("radius.md")};
    padding: 6px 9px;
    min-height: 22px;
}}
QLineEdit#ArchiveFilterField:focus,
QComboBox#ArchiveTableFilter:focus,
QDateEdit#ArchiveDateEdit:focus,
QComboBox#AnalyticsMetricSelector:focus,
QComboBox#AnalyticsCohortField:focus,
QComboBox#AnalyticsCohortOperator:focus,
QComboBox#AnalyticsComparisonMode:focus,
QLineEdit#AnalyticsCohortValue:focus,
QLineEdit#AnalyticsViewName:focus {{
    border: 2px solid {t("field.focus_border")};
}}
QComboBox#ArchiveTableFilter::drop-down,
QDateEdit#ArchiveDateEdit::drop-down,
QComboBox#AnalyticsMetricSelector::drop-down,
QComboBox#AnalyticsCohortField::drop-down,
QComboBox#AnalyticsCohortOperator::drop-down,
QComboBox#AnalyticsComparisonMode::drop-down,
QComboBox#AnalyticsSavedViews::drop-down,
QDateEdit#AnalyticsPeriodBFrom::drop-down,
QDateEdit#AnalyticsPeriodBTo::drop-down {{
    subcontrol-origin: padding;
    subcontrol-position: top right;
    width: 28px;
    border: none;
    border-left: 1px solid {t("field.border")};
    background-color: {t("surface.subtle")};
    border-top-right-radius: {t("radius.md")};
    border-bottom-right-radius: {t("radius.md")};
}}
QComboBox#ArchiveTableFilter::down-arrow,
QDateEdit#ArchiveDateEdit::down-arrow,
QComboBox#AnalyticsMetricSelector::down-arrow,
QComboBox#AnalyticsCohortField::down-arrow,
QComboBox#AnalyticsCohortOperator::down-arrow,
QComboBox#AnalyticsComparisonMode::down-arrow,
QComboBox#AnalyticsSavedViews::down-arrow,
QDateEdit#AnalyticsPeriodBFrom::down-arrow,
QDateEdit#AnalyticsPeriodBTo::down-arrow {{
    image: {FORM_DROPDOWN_ARROW_IMAGE};
    width: 12px;
    height: 12px;
}}
QComboBox#AnalyticsMetricSelector QAbstractItemView,
QComboBox#AnalyticsCohortField QAbstractItemView,
QComboBox#AnalyticsCohortOperator QAbstractItemView,
QComboBox#AnalyticsComparisonMode QAbstractItemView,
QComboBox#AnalyticsSavedViews QAbstractItemView {{
    background-color: {t("surface.card")};
    color: {t("text.primary")};
    border: 1px solid {t("field.border")};
    border-radius: {t("radius.md")};
    padding: 4px;
    outline: none;
    selection-background-color: {t("button.accent.bg")};
    selection-color: {t("button.accent.text")};
}}
QCalendarWidget#ArchiveCalendar QWidget {{
    background-color: {t("surface.card")};
    color: {t("text.primary")};
}}
QCalendarWidget#ArchiveCalendar QWidget#qt_calendar_navigationbar {{
    background-color: {t("surface.subtle")};
    border-bottom: 1px solid {t("border.subtle")};
}}
QCalendarWidget#ArchiveCalendar QToolButton {{
    background-color: transparent;
    color: {t("text.primary")};
    border: none;
    border-radius: {t("radius.sm")};
    padding: 6px;
    font-weight: 700;
}}
QCalendarWidget#ArchiveCalendar QToolButton:hover {{
    background-color: {t("surface.hover")};
}}
QCalendarWidget#ArchiveCalendar QAbstractItemView {{
    background-color: {t("surface.card")};
    color: {t("text.primary")};
    selection-background-color: {t("button.accent.bg")};
    selection-color: {t("button.accent.text")};
    border: none;
    outline: none;
}}
QLabel#ArchiveStatisticsStatus {{
    color: {t("text.secondary")};
    font-size: 12px;
}}
QCheckBox#ArchiveRecoveryToggle {{
    color: {t("text.primary")};
    spacing: 8px;
    padding: 7px 4px;
    font-weight: 650;
}}
QPushButton#ArchiveStatisticsRefresh {{
    background-color: {t("button.accent.bg")};
    color: {t("button.accent.text")};
    border: 1px solid {t("button.neutral.border")};
    border-radius: {t("radius.md")};
    padding: 8px 14px;
    font-weight: 700;
}}
QPushButton#ArchiveStatisticsSecondary,
QPushButton#ArchiveStatisticsOption,
QPushButton#ArchiveSecondaryAction,
QPushButton#ArchivePageButton {{
    background-color: {t("button.neutral.bg")};
    color: {t("button.neutral.text")};
    border: 1px solid {t("button.neutral.border")};
    border-radius: {t("radius.md")};
    padding: 8px 12px;
    font-weight: 650;
}}
QPushButton#ArchiveDangerAction {{
    background-color: {t("button.danger.bg")};
    color: {t("button.danger.text")};
    border: 1px solid {t("button.neutral.border")};
    border-radius: {t("radius.md")};
    padding: 8px 12px;
    font-weight: 650;
}}
QPushButton#ArchiveStatisticsRefresh:hover,
QPushButton#ArchiveStatisticsSecondary:hover,
QPushButton#ArchiveStatisticsOption:hover,
QPushButton#ArchiveSecondaryAction:hover,
QPushButton#ArchivePageButton:hover {{
    background-color: {t("button.neutral.hover")};
}}
QPushButton#ArchivePageButton:checked {{
    background-color: {t("button.accent.bg")};
    color: {t("button.accent.text")};
}}
QPushButton#ArchiveDangerAction:hover {{
    background-color: {t("button.danger.hover")};
}}
QPushButton#ArchiveStatisticsRefresh:disabled,
QPushButton#ArchiveStatisticsSecondary:disabled {{
    background-color: {t("surface.subtle")};
    color: {t("text.muted")};
}}
QScrollArea#ArchiveStatisticsOptionsScroll,
QScrollArea#ArchiveAnalysisScroll,
QWidget#ArchiveAnalysisBody,
QWidget#ArchiveStatisticsOptions {{
    background-color: transparent;
    border: none;
}}
QLabel#ArchiveStatisticsGroup {{
    color: {t("text.secondary")};
    font-size: 11px;
    font-weight: 750;
    padding: 8px 2px 3px;
}}
QCheckBox#ArchiveStatisticsCheck {{
    color: {t("text.primary")};
    spacing: 7px;
    padding: 3px 2px;
}}
QLabel#ArchiveStatisticsOptionLabel {{
    color: {t("text.primary")};
    font-size: 12px;
}}
QTextBrowser#ArchiveStatisticsReport {{
    background-color: {t("surface.card")};
    color: {t("text.primary")};
    border: 1px solid {t("border.subtle")};
    border-radius: 12px;
    padding: 8px;
}}
QFrame#AnalyticsWorkspace {{
    background-color: transparent;
    color: {t("text.primary")};
    border: none;
}}
QFrame#AnalyticsSection,
QFrame#AnalyticsKpiCard {{
    background-color: {t("surface.card")};
    color: {t("text.primary")};
    border: 1px solid {t("border.subtle")};
    border-radius: {t("radius.md")};
}}
QLabel#AnalyticsStepTitle {{
    color: {t("text.primary")};
    font-size: 14px;
    font-weight: 750;
    padding: 2px 1px;
}}
QLabel#AnalyticsSectionTitle {{
    color: {t("text.primary")};
    font-size: 12px;
    font-weight: 700;
}}
QLabel#AnalyticsFieldLabel,
QLabel#AnalyticsComparisonPeriodLabel,
QLabel#AnalyticsKpiTitle {{
    color: {t("text.secondary")};
    font-size: 11px;
}}
QLabel#AnalyticsKpiValue {{
    color: {t("text.primary")};
    font-size: 18px;
    font-weight: 750;
}}
QTextBrowser#AnalyticsSelectedResult {{
    background-color: {t("surface.subtle")};
    color: {t("text.primary")};
    border: 1px solid {t("border.subtle")};
    border-radius: {t("radius.md")};
    padding: 14px;
}}
QLabel#AnalyticsWorkspaceText {{
    color: {t("text.secondary")};
    font-size: 12px;
    padding: 3px 2px;
}}
QTableWidget#ArchiveDataTable {{
    background-color: {t("table.bg")};
    alternate-background-color: {t("surface.subtle")};
    color: {t("text.primary")};
    border: 1px solid {t("border.subtle")};
    border-radius: {t("radius.md")};
    gridline-color: {t("table.grid")};
    selection-background-color: {t("table.cell_selected_bg")};
    selection-color: {t("table.cell_selected_text")};
}}
QTableWidget#ArchiveDataTable QHeaderView::section {{
    background-color: {t("table.header_bg")};
    color: {t("table.header_text")};
    border: none;
    border-right: 1px solid {t("table.grid")};
    border-bottom: 1px solid {t("table.grid")};
    padding: 7px 6px;
    font-weight: 700;
}}
QLineEdit#ArchivePageJump {{
    background-color: {t("field.bg")};
    color: {t("field.text")};
    border: 1px solid {t("field.border")};
    border-radius: {t("radius.md")};
    padding: 5px 7px;
}}
QLabel#ArchivePageInfo {{
    color: {t("text.secondary")};
    padding: 0 6px;
}}
"""
