from __future__ import annotations

import os
import sys
from pathlib import Path


os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

PROJECT_DIR = Path(__file__).resolve().parents[1]
PACKAGE_PARENT = PROJECT_DIR.parent
if str(PACKAGE_PARENT) not in sys.path:
    sys.path.insert(0, str(PACKAGE_PARENT))

from PySide6.QtCore import QSettings, Qt  # noqa: E402
from PySide6.QtWidgets import (  # noqa: E402
    QApplication,
    QComboBox,
    QDialog,
    QFileDialog,
    QHeaderView,
    QLineEdit,
    QPushButton,
    QSizePolicy,
    QSpinBox,
    QTreeView,
    QVBoxLayout,
)
from rem_card.ui.shared.persistent_file_dialog import PersistentSaveFileDialog  # noqa: E402

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


def test_settings_center_uses_the_same_outer_frame_as_archive():
    app = application()
    widget = AdminMainWidget(role="doctor")
    widget.resize(1280, 720)
    widget.show()
    app.processEvents()

    margins = widget.layout().contentsMargins()
    assert widget.surface_frame.objectName() == "SettingsCenterFrame"
    assert (margins.left(), margins.top(), margins.right(), margins.bottom()) == (0, 5, 5, 4)
    assert widget.surface_frame.layout().contentsMargins().left() == 2
    assert "QFrame#SettingsCenterFrame" in widget.styleSheet()
    assert "border-radius: 5px" in widget.styleSheet()

    widget.close()
    widget.deleteLater()
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


def test_persistent_save_dialog_is_russian_full_width_and_restores_state(tmp_path):
    app = application()
    key = "tests/persistent_save_dialog"
    settings = QSettings("MyHospital", "RemCard")
    settings.remove(key)

    dialog = PersistentSaveFileDialog(
        title="Сохранить отчёт",
        directory=str(tmp_path),
        name_filter="PDF (*.pdf)",
        settings_key=key,
        default_suffix="pdf",
    )
    dialog.show()
    app.processEvents()
    tree = dialog.findChild(QTreeView)
    assert dialog.labelText(QFileDialog.DialogLabel.LookIn) == "Папка:"
    assert dialog.labelText(QFileDialog.DialogLabel.FileName) == "Имя файла:"
    assert dialog.labelText(QFileDialog.DialogLabel.FileType) == "Тип файлов:"
    assert dialog.labelText(QFileDialog.DialogLabel.Accept) == "Сохранить"
    assert [tree.model().headerData(i, Qt.Horizontal) for i in range(4)] == [
        "Имя",
        "Размер",
        "Тип",
        "Дата изменения",
    ]
    assert dialog.testOption(QFileDialog.Option.DontUseNativeDialog)
    assert dialog.windowFlags() & Qt.FramelessWindowHint
    assert not dialog.windowIcon().isNull()
    assert dialog.title_bar.objectName() == "DialogTitleBar"
    assert dialog.title_bar.title_label.objectName() == "DialogTitleText"
    assert dialog.title_bar.title_label.text() == "Сохранить отчёт"
    assert dialog.title_bar.btn_minimize.isVisibleTo(dialog)
    assert dialog.title_bar.btn_maximize.isVisibleTo(dialog)
    assert dialog.title_bar.btn_close.isVisibleTo(dialog)
    assert tree.header().sectionResizeMode(3) == QHeaderView.Stretch
    dialog.resize(930, 610)
    tree.header().resizeSection(0, 333)
    dialog._save_dialog_state()
    dialog.close()

    restored = PersistentSaveFileDialog(
        title="Сохранить отчёт",
        directory=str(tmp_path),
        name_filter="PDF (*.pdf)",
        settings_key=key,
        default_suffix="pdf",
    )
    restored.show()
    app.processEvents()
    restored._restore_dialog_state()
    restored_tree = restored.findChild(QTreeView)
    # Offscreen-платформа Qt ограничивает восстановленное окно виртуальным
    # экраном 800x800; главное — геометрия сохранена и не сброшена к default.
    assert settings.value(f"{key}/geometry") is not None
    assert restored.width() >= restored.minimumWidth()
    assert restored.height() >= 500
    assert restored_tree.header().sectionSize(0) == 333
    assert restored_tree.header().sectionResizeMode(3) == QHeaderView.Stretch

    restored.close()
    settings.remove(key)
    settings.sync()


def test_persistent_save_dialog_saves_state_only_once_when_done_closes_it(tmp_path):
    app = application()
    dialog = PersistentSaveFileDialog(
        title="Сохранить статистический отчёт",
        directory=str(tmp_path),
        name_filter="PDF (*.pdf)",
        settings_key="tests/persistent_save_dialog_single_close",
        default_suffix="pdf",
    )
    calls = 0
    original_save = dialog._save_dialog_state

    def counted_save():
        nonlocal calls
        calls += 1
        original_save()

    dialog._save_dialog_state = counted_save
    dialog.show()
    app.processEvents()
    dialog.done(QFileDialog.DialogCode.Rejected)
    app.processEvents()

    assert calls == 1


def test_persistent_save_dialog_custom_title_controls_window(tmp_path):
    app = application()
    dialog = PersistentSaveFileDialog(
        title="Сохранить статистический отчёт",
        directory=str(tmp_path),
        name_filter="PDF (*.pdf)",
        settings_key="tests/persistent_save_dialog_title_controls",
        default_suffix="pdf",
    )
    dialog.show()
    app.processEvents()
    normal_geometry = dialog.geometry()

    dialog.title_bar.btn_maximize.click()
    app.processEvents()
    assert dialog._is_custom_maximized is True
    assert dialog.title_bar.btn_maximize.text() == "❐"

    dialog.title_bar.btn_maximize.click()
    app.processEvents()
    assert dialog._is_custom_maximized is False
    assert dialog.geometry() == normal_geometry

    dialog.title_bar.btn_minimize.click()
    app.processEvents()
    assert dialog.isMinimized()
    dialog.showNormal()
    app.processEvents()

    dialog.title_bar.btn_close.click()
    app.processEvents()
    assert not dialog.isVisible()


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
