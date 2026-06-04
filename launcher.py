from _local_rem_card_bootstrap import bootstrap_local_rem_card


def _select_dev_role():
    from PySide6.QtCore import Qt
    from PySide6.QtWidgets import QApplication, QDialog, QHBoxLayout, QLabel, QPushButton, QVBoxLayout

    QApplication.instance() or QApplication([])

    dialog = QDialog()
    dialog.setWindowTitle("Выбор роли РЕМКАРТЫ")
    dialog.setModal(True)
    dialog.setMinimumWidth(420)

    selected_role = {"value": None}

    layout = QVBoxLayout(dialog)
    layout.setContentsMargins(18, 18, 18, 18)
    layout.setSpacing(14)

    title = QLabel("Выберите роль для запуска из исходников")
    title.setAlignment(Qt.AlignmentFlag.AlignCenter)
    title.setStyleSheet("font-size: 18px; font-weight: 700;")
    layout.addWidget(title)

    buttons = QHBoxLayout()
    buttons.setSpacing(10)
    layout.addLayout(buttons)

    for label, role in (
        ("Врач", "doctor"),
        ("Медсестра", "nurse"),
        ("Оперблок", "operblock"),
    ):
        button = QPushButton(label)
        button.setMinimumHeight(46)
        button.clicked.connect(
            lambda _checked=False, value=role: (
                selected_role.__setitem__("value", value),
                dialog.accept(),
            )
        )
        buttons.addWidget(button)

    if dialog.exec() != QDialog.DialogCode.Accepted:
        return None
    return selected_role["value"]


def run_rem_card():
    bootstrap_local_rem_card()

    role = _select_dev_role()
    if not role:
        return

    from rem_card.app.main import main

    main(forced_role=role)

if __name__ == "__main__":
    run_rem_card()
