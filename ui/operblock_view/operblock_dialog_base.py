from __future__ import annotations
from rem_card.ui.styles.theme_runtime import set_widget_style


from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QDialog,
    QFrame,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QStyle,
    QStyleOptionViewItem,
    QStyledItemDelegate,
    QVBoxLayout,
)

from rem_card.ui.shared.window_state import SavedFramelessDialogMixin
from rem_card.ui.styles.shared_styles import apply_custom_dialog_style
from rem_card.ui.styles.theme import (
    STYLE_PATIENT_FORM_TAB,
)

from rem_card.ui.operblock_view.operblock_visual_primitives import (
    _apply_operblock_window_icon,
    _create_operblock_title_icon,
)

class OperBlockStyledDialog(SavedFramelessDialogMixin, QDialog):
    embedded_accept_requested = Signal()
    embedded_reject_requested = Signal()

    def __init__(
        self,
        title: str,
        settings_key: str,
        parent=None,
        *,
        minimum_size: tuple[int, int] | None = None,
        initial_size: tuple[int, int] | None = None,
        drag_area_height: int = 58,
    ):
        super().__init__(parent)
        self.setWindowTitle(title)
        _apply_operblock_window_icon(self)
        if minimum_size:
            self.setMinimumSize(*minimum_size)
        if initial_size:
            self.resize(*initial_size)
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setWindowFlags(self.windowFlags() | Qt.FramelessWindowHint)
        self.setMouseTracking(True)
        self._init_saved_frameless_dialog(f"operblock/{settings_key}", drag_area_height=drag_area_height)
        self._init_dialog_chrome(title)

    def _init_dialog_chrome(self, title: str):
        apply_custom_dialog_style(self)

        self.layout_container = QVBoxLayout(self)
        self.layout_container.setContentsMargins(0, 0, 0, 0)

        self.bg_container = QFrame(self)
        self.bg_container.setObjectName("DialogMainFrame")
        self.bg_container.setMouseTracking(True)
        self.layout_container.addWidget(self.bg_container)

        self.main_layout = QVBoxLayout(self.bg_container)
        self.main_layout.setContentsMargins(0, 0, 0, 0)
        self.main_layout.setSpacing(0)

        header_panel = QFrame(self.bg_container)
        header_panel.setObjectName("DialogTitleBar")
        header_panel.setFixedHeight(30)
        header_layout = QHBoxLayout(header_panel)
        header_layout.setContentsMargins(5, 0, 0, 0)
        header_layout.setSpacing(0)

        icon_label = _create_operblock_title_icon(20)
        if icon_label is not None:
            header_layout.addWidget(icon_label)
            header_layout.addSpacing(8)

        title_label = QLabel(str(title or ""))
        title_label.setObjectName("DialogTitleText")
        header_layout.addWidget(title_label)
        header_layout.addStretch(1)

        self.close_button = QPushButton("✕")
        self.close_button.setObjectName("DialogCloseBtn")
        self.close_button.setFixedSize(30, 30)
        self.close_button.setCursor(Qt.PointingHandCursor)
        self.close_button.clicked.connect(self.reject)
        header_layout.addWidget(self.close_button)
        self.main_layout.addWidget(header_panel)

        self.content_widget = QFrame(self.bg_container)
        set_widget_style(self.content_widget, STYLE_PATIENT_FORM_TAB)
        self.content_layout = QVBoxLayout(self.content_widget)
        self.content_layout.setContentsMargins(20, 16, 20, 20)
        self.content_layout.setSpacing(10)
        self.main_layout.addWidget(self.content_widget, 1)

    def _finalize_dialog_chrome(self):
        self._restore_saved_geometry()

    def accept(self) -> None:
        if bool(self.property("settingsEmbedded")):
            self.embedded_accept_requested.emit()
            return
        super().accept()

    def reject(self) -> None:
        if bool(self.property("settingsEmbedded")):
            self.embedded_reject_requested.emit()
            return
        super().reject()

    def _configure_enter_accept_button(self, cancel_button: QPushButton, save_button: QPushButton) -> None:
        cancel_button.setAutoDefault(False)
        cancel_button.setDefault(False)
        save_button.setAutoDefault(True)
        save_button.setDefault(True)
class _OperBlockNoFocusRectDelegate(QStyledItemDelegate):
    """Paint selected cells without the native dotted current-item frame."""

    def paint(self, painter, option, index) -> None:
        clean_option = QStyleOptionViewItem(option)
        clean_option.state &= ~QStyle.State_HasFocus
        super().paint(painter, clean_option, index)
