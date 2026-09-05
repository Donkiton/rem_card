from __future__ import annotations

import os
import sys
from pathlib import Path
import pytest


os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

PROJECT_DIR = Path(__file__).resolve().parents[1]
PACKAGE_PARENT = PROJECT_DIR.parent
if str(PACKAGE_PARENT) not in sys.path:
    sys.path.insert(0, str(PACKAGE_PARENT))

from PySide6.QtCore import QCoreApplication, QEvent, QSettings  # noqa: E402
from PySide6.QtWidgets import QApplication, QWidget  # noqa: E402

from rem_card.ui.admin_view.drugs_dict_widget import (  # noqa: E402
    DrugDialog,
    MultiCompDrugDialog,
)


def application() -> QApplication:
    return QApplication.instance() or QApplication([])


@pytest.fixture
def drug_dialog_parent():
    app = application()
    parent = QWidget()
    yield parent
    # close()/accept() скрывают диалог, но циклические ссылки в Qt/Python
    # могут оставить его для сборки мусора в другом потоке. Уничтожаем
    # всё дерево окон здесь, пока GUI-поток и QApplication ещё живы.
    parent.close()
    parent.deleteLater()
    QCoreApplication.sendPostedEvents(None, QEvent.DeferredDelete)
    app.processEvents()


def test_drug_editor_is_resizable_and_restores_saved_geometry(tmp_path, monkeypatch, drug_dialog_parent):
    app = application()
    settings = QSettings(str(tmp_path / "drug-editor.ini"), QSettings.IniFormat)
    monkeypatch.setattr(DrugDialog, "_settings", lambda _self: settings)

    first = DrugDialog("nacl", {"latin": "NaCl 0.9% 250 ml"}, parent=drug_dialog_parent)
    first.resize(780, 720)
    assert first.maximumWidth() > first.minimumWidth()
    assert first.maximumHeight() > first.minimumHeight()
    first.accept()

    restored = DrugDialog("nacl", {"latin": "NaCl 0.9% 250 ml"}, parent=drug_dialog_parent)
    assert restored.size().width() == 780
    assert restored.size().height() == 720
    assert restored.form_scroll.widget() is restored.form_widget
    assert restored.form_scroll.viewport().autoFillBackground() is False
    assert restored.form_widget.autoFillBackground() is False
    assert "background: transparent" in restored.form_scroll.styleSheet()
    restored.close()

    settings.clear()
    app.processEvents()


def test_multicomponent_drug_editor_is_resizable_and_scrollable(tmp_path, monkeypatch, drug_dialog_parent):
    app = application()
    settings = QSettings(str(tmp_path / "multicomp-editor.ini"), QSettings.IniFormat)
    monkeypatch.setattr(MultiCompDrugDialog, "_settings", lambda _self: settings)
    dialog = MultiCompDrugDialog(parent=drug_dialog_parent)

    assert dialog.maximumWidth() > dialog.minimumWidth()
    assert dialog.maximumHeight() > dialog.minimumHeight()
    assert dialog.form_scroll.widget() is dialog.form_widget
    assert dialog.form_scroll.viewport().autoFillBackground() is False
    assert "background: transparent" in dialog.form_scroll.styleSheet()

    dialog.close()
    settings.clear()
    app.processEvents()
