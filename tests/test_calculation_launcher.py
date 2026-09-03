from __future__ import annotations

import os
import subprocess
import sys
import textwrap
from pathlib import Path


PROJECT_DIR = Path(__file__).resolve().parents[1]


def _run_qt_script(source: str) -> subprocess.CompletedProcess[str]:
    env = dict(os.environ)
    env["QT_QPA_PLATFORM"] = "offscreen"
    return subprocess.run(
        [sys.executable, "-c", textwrap.dedent(source)],
        cwd=PROJECT_DIR,
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )


def test_launcher_disables_burns_and_returns_available_selection():
    completed = _run_qt_script(
        """
        import sys
        from pathlib import Path
        sys.path.insert(0, str(Path.cwd().parent))
        from PySide6.QtWidgets import QApplication, QDialog
        from rem_card.ui.shared.components.calculation_launcher import (
            CALCULATION_BURNS,
            CalculationLauncherDialog,
        )

        app = QApplication.instance() or QApplication([])
        reason = "Диагноз не относится к острым ожогам T20–T25, T27, T29–T32"
        disabled = CalculationLauncherDialog(burn_enabled=False, burn_disabled_reason=reason)
        disabled.show()
        app.processEvents()
        assert not disabled.burns_button.isEnabled()
        assert reason in disabled.burns_button.text()
        assert disabled.burns_button.toolTip() == reason
        assert disabled.infusion_button.hasFocus()
        disabled.close()

        enabled = CalculationLauncherDialog(burn_enabled=True)
        enabled.burns_button.click()
        assert enabled.result() == QDialog.Accepted
        assert enabled.selected_calculation == CALCULATION_BURNS
        print("completed")
        """
    )

    assert completed.returncode == 0, completed.stderr
    assert "completed" in completed.stdout


def test_repeated_launcher_and_infusion_dialogs_are_destroyed():
    completed = _run_qt_script(
        """
        import sys
        from pathlib import Path
        sys.path.insert(0, str(Path.cwd().parent))
        from PySide6.QtCore import QCoreApplication, QEvent, QTimer
        from PySide6.QtWidgets import QApplication, QDialog, QWidget
        from rem_card.ui.shared.components.calculation_launcher import (
            exec_calculation_dialog,
            run_calculation_launcher,
        )
        from rem_card.ui.shared.components.infusion_calculator import InfusionCalculatorDialog

        app = QApplication.instance() or QApplication([])
        host = QWidget()
        host.resize(900, 700)
        host.show()
        app.processEvents()
        for index in range(30):
            QTimer.singleShot(0, lambda: app.activeModalWidget().infusion_button.click())
            selected, center = run_calculation_launcher(
                host,
                burn_enabled=False,
                burn_disabled_reason="Только из карты пациента",
            )
            if selected != "infusion":
                raise RuntimeError(f"wrong selection at iteration {index}")
            calculator = InfusionCalculatorDialog(host)
            QTimer.singleShot(0, calculator.accept)
            exec_calculation_dialog(calculator, center)
            QCoreApplication.sendPostedEvents(None, QEvent.DeferredDelete)
            app.processEvents()
            if host.findChildren(QDialog):
                raise RuntimeError(f"leaked dialogs at iteration {index}")
        print("completed")
        """
    )

    assert completed.returncode == 0, completed.stderr
    assert "completed" in completed.stdout
