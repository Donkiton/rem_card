"""Нативные подсказки Qt для уже отфильтрованных результатов МКБ."""

from PySide6.QtCore import QRect, QSize, Qt, Signal
from PySide6.QtGui import QColor, QFontMetrics, QStandardItem, QStandardItemModel
from PySide6.QtWidgets import QCompleter, QListView, QStyle, QStyledItemDelegate

from rem_card.services.mkb import MKBMatch
from rem_card.ui.styles.diagnosis_styles import (
    LIGHT_DIAGNOSIS_PALETTE,
    DiagnosisPalette,
    diagnosis_popup_style,
)


MATCH_ROLE = int(Qt.UserRole) + 1
MANUAL_ROLE = int(Qt.UserRole) + 2


class DiagnosisSuggestionDelegate(QStyledItemDelegate):
    def __init__(self, view):
        super().__init__(view)
        self.view = view
        self.colors = LIGHT_DIAGNOSIS_PALETTE

    def _text_rect(self, rect, match):
        return rect.adjusted(78 if match else 12, 9, -12, -9)

    def sizeHint(self, option, index):
        match = index.data(MATCH_ROLE)
        width = max(180, self.view.viewport().width())
        text_width = width - (90 if match else 24)
        metrics = QFontMetrics(option.font)
        bounds = metrics.boundingRect(
            QRect(0, 0, text_width, 10000), Qt.TextWordWrap,
            match.name if match else index.data(Qt.DisplayRole),
        )
        return QSize(width, max(38, bounds.height() + 18))

    def paint(self, painter, option, index):
        painter.save()
        colors = self.colors
        active = bool(option.state & (QStyle.State_Selected | QStyle.State_MouseOver))
        painter.fillRect(option.rect, QColor(colors.hover if active else colors.surface))
        painter.setFont(option.font)
        match = index.data(MATCH_ROLE)
        if match:
            code_font = painter.font()
            code_font.setBold(True)
            painter.setFont(code_font)
            painter.setPen(QColor(colors.accent))
            painter.drawText(option.rect.adjusted(12, 9, -12, -9), Qt.AlignLeft | Qt.AlignTop, match.code)
            painter.setFont(option.font)
        painter.setPen(QColor(colors.text if match else colors.accent if index.data(MANUAL_ROLE) else colors.muted))
        painter.drawText(
            self._text_rect(option.rect, match), Qt.AlignLeft | Qt.AlignTop | Qt.TextWordWrap,
            match.name if match else index.data(Qt.DisplayRole),
        )
        painter.restore()


class DiagnosisCompleter(QCompleter):
    diagnosis_selected = Signal(object)
    manual_requested = Signal(str)

    def __init__(self, line_edit):
        super().__init__(line_edit)
        self.line_edit = line_edit
        self.query = ""
        self.results_model = QStandardItemModel(self)
        self.setModel(self.results_model)
        self.setCompletionMode(QCompleter.UnfilteredPopupCompletion)
        self.setMaxVisibleItems(7)
        view = QListView()
        view.setWordWrap(True)
        view.setTextElideMode(Qt.ElideNone)
        view.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        view.setMouseTracking(True)
        self.delegate = DiagnosisSuggestionDelegate(view)
        view.setAccessibleName("Подсказки диагнозов МКБ-10")
        self.setPopup(view)
        view.setItemDelegate(self.delegate)
        # QLineEdit.setCompleter связывает highlighted(QString) с заменой
        # текста. Нам нужен отдельный запрос: стрелки только подсвечивают
        # строки, а диагноз меняется исключительно после подтверждения.
        self.setWidget(line_edit)
        self.activated["QModelIndex"].connect(self.accept_index)
        self.apply_palette(LIGHT_DIAGNOSIS_PALETTE)

    def pathFromIndex(self, index):
        match = index.data(MATCH_ROLE)
        return match.code if match else "" if index.data(MANUAL_ROLE) else self.line_edit.text()

    def apply_palette(self, palette: DiagnosisPalette):
        self.delegate.colors = palette
        self.popup().setStyleSheet(diagnosis_popup_style(palette))
        self.popup().viewport().update()

    def set_matches(self, query: str, matches: list[MKBMatch], *, code_like: bool, failed: bool = False):
        self.query = query
        self.results_model.clear()
        for match in matches:
            item = QStandardItem(f"{match.code} — {match.name}")
            item.setData(match, MATCH_ROLE)
            self.results_model.appendRow(item)
        if not matches:
            item = QStandardItem("Справочник недоступен" if failed else "Совпадений нет")
            item.setFlags(Qt.NoItemFlags)
            self.results_model.appendRow(item)
        own = QStandardItem("Написать свой диагноз без кода" if code_like else "Использовать введённый текст без кода")
        own.setData(True, MANUAL_ROLE)
        self.results_model.appendRow(own)
        self.setCompletionPrefix(query)

    def accept_index(self, index):
        match = index.data(MATCH_ROLE)
        if match:
            self.diagnosis_selected.emit(match)
        elif index.data(MANUAL_ROLE):
            self.manual_requested.emit(self.query)
        self.popup().hide()

    def show_matches(self):
        rect = self.line_edit.rect()
        self.popup().setMinimumWidth(rect.width())
        self.popup().setMaximumWidth(rect.width())
        self.popup().doItemsLayout()
        self.complete(rect)
