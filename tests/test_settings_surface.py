from __future__ import annotations

import os
import sys
from pathlib import Path


os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

PROJECT_DIR = Path(__file__).resolve().parents[1]
PACKAGE_PARENT = PROJECT_DIR.parent
if str(PACKAGE_PARENT) not in sys.path:
    sys.path.insert(0, str(PACKAGE_PARENT))

from PySide6.QtWidgets import (  # noqa: E402
    QApplication,
    QComboBox,
    QDialog,
    QFileDialog,
    QLineEdit,
    QPushButton,
    QSizePolicy,
    QSpinBox,
    QVBoxLayout,
)

from rem_card.ui.admin_view.admin_main_widget import AdminMainWidget  # noqa: E402
from rem_card.ui.admin_view.groups_dict_widget import GroupDialog  # noqa: E402
from rem_card.ui.admin_view.templates_dict_widget import TemplatesDictWidget  # noqa: E402
from rem_card.ui.styles.settings_surface import (  # noqa: E402
    apply_settings_surface,
    prepare_settings_file_dialog,
)


def application() -> QApplication:
    return QApplication.instance() or QApplication([])


def test_settings_surface_assigns_one_primary_and_semantic_danger_button():
    app = application()
    dialog = QDialog()
    layout = QVBoxLayout(dialog)
    add_button = QPushButton("Добавить")
    save_button = QPushButton("Сохранить")
    delete_button = QPushButton("Удалить")
    cancel_button = QPushButton("Отмена")
    for button in (add_button, save_button, delete_button, cancel_button):
        button.setObjectName("DialogOkBtn")
        layout.addWidget(button)

    apply_settings_surface(dialog)

    assert save_button.property("settingsSurfaceRole") == "primary"
    assert add_button.property("settingsSurfaceRole") == "secondary"
    assert delete_button.property("settingsSurfaceRole") == "danger"
    assert cancel_button.property("settingsSurfaceRole") == "secondary"
    assert all(
        button.objectName() == "SettingsSurfaceButton"
        for button in (add_button, save_button, delete_button, cancel_button)
    )
    assert save_button.styleSheet()

    dialog.deleteLater()
    app.processEvents()


def test_nested_base_dialog_inherits_settings_context_on_show():
    app = application()
    host = AdminMainWidget(role="admin")
    dialog = GroupDialog(parent=host)

    dialog.show()
    app.processEvents()

    roles = {
        button.text(): button.property("settingsSurfaceRole")
        for button in dialog.buttons.buttons()
    }
    assert dialog.property("settingsContext") is True
    assert roles == {"OK": "primary", "Cancel": "secondary"}

    dialog.close()
    host.deleteLater()
    app.processEvents()


def test_complex_templates_editor_uses_fixed_shared_header():
    app = application()
    widget = TemplatesDictWidget()

    assert widget.dictionary_header.objectName() == "AdminDictionaryHeader"
    assert widget.dictionary_header.sizePolicy().verticalPolicy() == QSizePolicy.Fixed
    assert widget.btn_back.text() == "← Настройки"
    assert widget.btn_save_tpl.objectName() == "AdminDictionaryPrimaryButton"
    assert widget.btn_del_tpl.objectName() == "AdminDictionaryDangerButton"
    assert widget.table.objectName() == "AdminDictionaryTable"

    widget.deleteLater()
    app.processEvents()


def test_settings_file_dialog_uses_non_native_shared_surface():
    app = application()
    dialog = QFileDialog()

    prepare_settings_file_dialog(dialog)

    assert dialog.testOption(QFileDialog.Option.DontUseNativeDialog)
    assert dialog.property("settingsContext") is True
    assert dialog.styleSheet()

    dialog.deleteLater()
    app.processEvents()


def test_settings_surface_preserves_special_icon_buttons_and_field_height():
    app = application()
    dialog = QDialog()
    layout = QVBoxLayout(dialog)
    favorite_button = QPushButton()
    favorite_button.setObjectName("OperBlockFavoritePresetButton")
    favorite_button.setProperty("settingsSurfaceSkip", True)
    favorite_button.setStyleSheet("QPushButton { border: none; }")
    field = QLineEdit("NaCl 0.9% 250 ml")
    combo = QComboBox()
    combo.addItems(["Внутривенно", "Перорально"])
    spin = QSpinBox()
    layout.addWidget(favorite_button)
    layout.addWidget(field)
    layout.addWidget(combo)
    layout.addWidget(spin)

    apply_settings_surface(dialog)

    assert favorite_button.property("settingsSurfaceRole") is None
    assert "border: none" in favorite_button.styleSheet()
    assert field.minimumHeight() >= 38
    assert combo.property("settingsSurfaceControl") is True
    assert spin.property("settingsSurfaceControl") is True
    style = dialog.styleSheet()
    assert "QScrollBar::handle:vertical" in style
    assert "QComboBox[settingsSurfaceControl=\"true\"]::drop-down" in style
    assert "combo_arrow_down.svg" in style
    assert "spin_arrow_up.svg" in style
    assert "spin_arrow_down.svg" in style

    dialog.deleteLater()
    app.processEvents()
