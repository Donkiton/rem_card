from types import SimpleNamespace
import time

import pytest
from PySide6.QtCore import Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QDialog, QLabel

from rem_card.app.updater_main import UpdateWindow, UpdateWorker
from rem_card.ui.shared.update_dialogs import UpdateLaunchDialog, UpdatePromptDialog


@pytest.fixture
def app():
    return QApplication.instance() or QApplication([])


def test_prompt_matches_standalone_updater_without_native_title_bar(app):
    prompt = UpdatePromptDialog("5.1.11")
    updater = UpdateWindow(SimpleNamespace())
    for window in (prompt, updater):
        window.show()
    app.processEvents()
    assert prompt.styleSheet() == updater.styleSheet()
    assert prompt.width() == updater.width() == 460
    assert prompt.windowFlags() & Qt.FramelessWindowHint
    assert prompt.title_bar.height() == updater.title_bar.height()
    assert "5.1.11" in prompt.findChild(QLabel, "UpdateStatus").text()
    # All controls and wrapping labels must fit inside the card at its actual size.
    for widget in prompt.findChildren(QLabel) + [prompt.update_button, prompt.skip_button]:
        assert prompt.rect().contains(widget.mapTo(prompt, widget.rect().topLeft()))
        assert prompt.rect().contains(widget.mapTo(prompt, widget.rect().bottomRight()))
    updater._finished = True


def test_updater_can_construct_its_background_worker(app, monkeypatch):
    from PySide6.QtCore import QThread
    updater = UpdateWindow(SimpleNamespace())
    starts = []
    # Exercise the real worker constructor and signal wiring without running
    # an installation against any local or network files.
    monkeypatch.setattr(QThread, 'start', lambda thread: starts.append(thread))
    updater.start()
    assert isinstance(updater._worker, UpdateWorker)
    assert starts == [updater._thread]
    updater._progress_timer.stop()
    updater._finished = True


def test_updater_reports_invalid_package_from_real_background_thread(app, tmp_path):
    updater = UpdateWindow(SimpleNamespace(
        source=str(tmp_path / 'missing-release'), target=str(tmp_path / 'install'),
        baza_dir=str(tmp_path / 'baza'), target_version='5.1.11',
        starting_lock='', local_starting_lock='',
    ))
    updater.show()
    updater.start()
    try:
        deadline = time.monotonic() + 3
        while (not updater._finished or updater._thread.isRunning()) and time.monotonic() < deadline:
            QTest.qWait(10)
        assert updater._finished and not updater._thread.isRunning()
        assert updater.title_label.text() == 'Обновление не выполнено'
        assert 'ready.ok' in updater.status_label.text()
        assert updater.close_button.isVisible()
    finally:
        updater._thread.quit()
        assert updater._thread.wait(3000)
        updater._progress_timer.stop()


@pytest.mark.parametrize("action,expected", [
    ("update", QDialog.Accepted), ("skip", QDialog.Rejected),
    ("escape", QDialog.Rejected), ("close", QDialog.Rejected),
    ("enter", QDialog.Accepted),
])
def test_prompt_buttons_and_keyboard(app, action, expected):
    prompt = UpdatePromptDialog("5.1.11")
    prompt.show()
    app.processEvents()
    if action in ("update", "skip"):
        QTest.mouseClick(getattr(prompt, action + "_button"), Qt.LeftButton)
    elif action == "close":
        QTest.mouseClick(prompt.window_close_button, Qt.LeftButton)
    else:
        QTest.keyClick(prompt, Qt.Key_Escape if action == "escape" else Qt.Key_Return)
    assert not prompt.isVisible()
    assert prompt.result() == expected


def test_launch_feedback_stays_visible_until_launcher_finishes(app):
    dialog = UpdateLaunchDialog("5.1.11")
    dialog.show()
    app.processEvents()
    QTest.keyClick(dialog, Qt.Key_Escape)
    dialog.close()
    assert dialog.isVisible()
    assert dialog.progress.minimum() == dialog.progress.maximum() == 0
    assert dialog.window_close_button.isHidden()

