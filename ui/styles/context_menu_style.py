from __future__ import annotations

from PySide6.QtCore import QEvent, QObject, QPoint, Qt
from PySide6.QtGui import QColor, QIcon, QPainter, QPen, QPixmap
from PySide6.QtWidgets import QApplication, QLineEdit, QMenu, QPlainTextEdit, QTextEdit, QWidget

from rem_card.ui.styles.theme_manager import get_theme_manager
from rem_card.ui.styles.theme_tokens import token


_ACTION_TRANSLATIONS = {
    "Undo": "Отменить",
    "Redo": "Повторить",
    "Cut": "Вырезать",
    "Copy": "Копировать",
    "Paste": "Вставить",
    "Delete": "Удалить",
    "Select All": "Выделить всё",
}

_ACTION_ICON_NAMES = {
    "Undo": "edit-undo",
    "Redo": "edit-redo",
    "Cut": "edit-cut",
    "Copy": "edit-copy",
    "Paste": "edit-paste",
    "Delete": "edit-delete",
    "Select All": "edit-select-all",
}


def build_context_menu_style(tokens: dict[str, str] | None = None) -> str:
    tokens = tokens or get_theme_manager().current_tokens()
    t = lambda key, default="": token(tokens, key, default)
    return f"""
        QMenu {{
            background-color: {t("dialog.bg", t("surface.card", "#ffffff"))};
            color: {t("text.primary", "#2c3e50")};
            border: 1px solid {t("dialog.border", t("border.default", "#bdc3c7"))};
            padding: 4px;
        }}
        QMenu::item {{
            background-color: transparent;
            color: {t("text.primary", "#2c3e50")};
            padding: 6px 28px 6px 12px;
            min-width: 140px;
        }}
        QMenu::item:selected {{
            background-color: {t("surface.selected", "#007bff")};
            color: {t("text.inverse", "#ffffff")};
        }}
        QMenu::item:disabled {{
            color: {t("text.disabled", "#adb5bd")};
        }}
        QMenu::separator {{
            height: 1px;
            background-color: {t("border.subtle", "#dee2e6")};
            margin: 4px 6px;
        }}
    """


def apply_context_menu_style(menu: QMenu) -> None:
    menu.setStyleSheet(build_context_menu_style())


def _action_key(text: str) -> str:
    return str(text or "").split("\t", 1)[0].replace("&", "").strip()


def _translated_action_text(text: str) -> str:
    before_shortcut, separator, shortcut = str(text or "").partition("\t")
    translated = _ACTION_TRANSLATIONS.get(before_shortcut.replace("&", "").strip())
    if not translated:
        return text
    return f"{translated}{separator}{shortcut}" if separator else translated


def _fallback_action_icon(action_key: str) -> QIcon:
    icon = QIcon.fromTheme(_ACTION_ICON_NAMES.get(action_key, ""))
    if not icon.isNull():
        return icon

    tokens = get_theme_manager().current_tokens()
    color = QColor(token(tokens, "text.secondary", "#495057"))
    pixmap = QPixmap(16, 16)
    pixmap.fill(Qt.transparent)

    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.Antialiasing)
    pen = QPen(color, 1.6)
    pen.setCapStyle(Qt.RoundCap)
    pen.setJoinStyle(Qt.RoundJoin)
    painter.setPen(pen)
    painter.setBrush(Qt.NoBrush)

    if action_key == "Undo":
        painter.drawArc(4, 4, 9, 8, 35 * 16, 245 * 16)
        painter.drawLine(4, 8, 2, 5)
        painter.drawLine(4, 8, 7, 8)
    elif action_key == "Redo":
        painter.drawArc(3, 4, 9, 8, -100 * 16, 245 * 16)
        painter.drawLine(12, 8, 14, 5)
        painter.drawLine(12, 8, 9, 8)
    elif action_key == "Cut":
        painter.drawLine(4, 4, 12, 12)
        painter.drawLine(12, 4, 4, 12)
        painter.drawEllipse(2, 10, 4, 4)
        painter.drawEllipse(10, 10, 4, 4)
    elif action_key == "Copy":
        painter.drawRect(3, 5, 8, 9)
        painter.drawRect(6, 2, 8, 9)
    elif action_key == "Paste":
        painter.drawRect(4, 5, 8, 9)
        painter.drawRoundedRect(5, 2, 6, 4, 1, 1)
    elif action_key == "Delete":
        painter.drawLine(5, 5, 11, 11)
        painter.drawLine(11, 5, 5, 11)
    elif action_key == "Select All":
        painter.drawRect(3, 3, 10, 10)
        painter.drawLine(5, 6, 11, 6)
        painter.drawLine(5, 9, 11, 9)

    painter.end()
    return QIcon(pixmap)


def _localize_menu_actions(menu: QMenu) -> None:
    for action in menu.actions():
        if action.isSeparator():
            continue
        action_key = _action_key(action.text())
        action.setText(_translated_action_text(action.text()))
        if action.icon().isNull() and action_key in _ACTION_TRANSLATIONS:
            action.setIcon(_fallback_action_icon(action_key))


def build_text_edit_context_menu(editor: QLineEdit | QTextEdit | QPlainTextEdit, global_pos: QPoint | None = None) -> QMenu:
    if isinstance(editor, QLineEdit):
        menu = editor.createStandardContextMenu()
    else:
        try:
            position = editor.viewport().mapFromGlobal(global_pos) if global_pos is not None else QPoint()
            menu = editor.createStandardContextMenu(position)
        except TypeError:
            menu = editor.createStandardContextMenu()

    apply_context_menu_style(menu)
    _localize_menu_actions(menu)

    return menu


def build_line_edit_context_menu(field: QLineEdit) -> QMenu:
    return build_text_edit_context_menu(field)


def install_russian_text_edit_context_menu(editor: QLineEdit | QTextEdit | QPlainTextEdit) -> None:
    if editor.property("_remcard_russian_context_menu"):
        return

    editor.setProperty("_remcard_russian_context_menu", True)
    editor.setContextMenuPolicy(Qt.CustomContextMenu)

    def show_context_menu(position):
        global_pos = editor.mapToGlobal(position)
        menu = build_text_edit_context_menu(editor, global_pos)
        menu.exec(global_pos)

    editor.customContextMenuRequested.connect(show_context_menu)


def install_russian_line_edit_context_menu(field: QLineEdit) -> None:
    install_russian_text_edit_context_menu(field)


def _text_editor_from_event_object(obj) -> QLineEdit | QTextEdit | QPlainTextEdit | None:
    if isinstance(obj, (QLineEdit, QTextEdit, QPlainTextEdit)):
        return obj

    parent = obj.parent() if isinstance(obj, QWidget) else None
    if isinstance(parent, (QTextEdit, QPlainTextEdit)) and parent.viewport() is obj:
        return parent

    return None


class _TextEditContextMenuFilter(QObject):
    def eventFilter(self, obj, event):
        if isinstance(obj, QMenu) and event.type() == QEvent.Show:
            if not obj.property("_remcard_context_menu_styled"):
                apply_context_menu_style(obj)
                obj.setProperty("_remcard_context_menu_styled", True)
            return super().eventFilter(obj, event)

        if event.type() != QEvent.ContextMenu:
            return super().eventFilter(obj, event)

        editor = _text_editor_from_event_object(obj)
        if editor is None:
            return super().eventFilter(obj, event)

        global_pos = event.globalPos()
        menu = build_text_edit_context_menu(editor, global_pos)
        menu.exec(global_pos)
        return True


def install_global_text_edit_context_menus(app: QApplication | None = None) -> None:
    target_app = app or QApplication.instance()
    if target_app is None or getattr(target_app, "_remcard_text_context_menu_filter", None) is not None:
        return

    context_filter = _TextEditContextMenuFilter(target_app)
    target_app.installEventFilter(context_filter)
    setattr(target_app, "_remcard_text_context_menu_filter", context_filter)


def install_russian_context_menus(root: QWidget) -> None:
    if isinstance(root, (QLineEdit, QTextEdit, QPlainTextEdit)):
        install_russian_text_edit_context_menu(root)

    for editor_class in (QLineEdit, QTextEdit, QPlainTextEdit):
        for field in root.findChildren(editor_class):
            install_russian_text_edit_context_menu(field)
