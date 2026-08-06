from __future__ import annotations

import os
import sys
from pathlib import Path


os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

PROJECT_DIR = Path(__file__).resolve().parents[1]
PACKAGE_PARENT = PROJECT_DIR.parent
if str(PACKAGE_PARENT) not in sys.path:
    sys.path.insert(0, str(PACKAGE_PARENT))

from PySide6.QtWidgets import QApplication, QFrame  # noqa: E402

from rem_card.ui.admin_view import groups_dict_widget  # noqa: E402
from rem_card.ui.admin_view.drugs_dict_widget import DrugsDictWidget  # noqa: E402
from rem_card.ui.admin_view.groups_dict_widget import GroupsDictWidget  # noqa: E402


def application() -> QApplication:
    return QApplication.instance() or QApplication([])


def test_dictionary_page_uses_shared_settings_chrome(monkeypatch):
    app = application()
    monkeypatch.setattr(
        groups_dict_widget.engine,
        "groups",
        {
            "antibiotics": {"name_ru": "Антибиотики"},
            "diuretics": {"name_ru": "Диуретики"},
        },
    )
    widget = GroupsDictWidget()

    assert widget.objectName() == "AdminDictionaryPage"
    assert widget.frame.objectName() == "AdminDictionaryShell"
    assert widget.table.objectName() == "AdminDictionaryTable"
    assert widget.findChild(QFrame, "AdminDictionaryHeader") is not None
    assert widget.btn_back.text() == "← Настройки"
    assert widget.btn_add.objectName() == "AdminDictionaryPrimaryButton"
    assert widget.btn_edit.objectName() == "AdminDictionarySecondaryButton"
    assert widget.btn_delete.objectName() == "AdminDictionaryDangerButton"
    assert widget.dictionary_count_label.text() == "2 записи"

    widget.dictionary_search_input.setText("анти")
    app.processEvents()
    assert widget.table.isRowHidden(0) is False
    assert widget.table.isRowHidden(1) is True
    assert widget.dictionary_count_label.text() == "1 запись"

    widget.deleteLater()
    app.processEvents()


def test_drugs_page_keeps_its_domain_filters_inside_shared_toolbar():
    app = application()
    widget = DrugsDictWidget()

    assert widget.group_filter.parent().objectName() == "AdminDictionaryToolbar"
    assert widget.search_input.parent().objectName() == "AdminDictionaryToolbar"
    assert widget.group_filter.objectName() == "AdminDictionaryFilter"
    assert widget.search_input.objectName() == "AdminDictionarySearch"

    widget.deleteLater()
    app.processEvents()
