"""Изолированная галерея реальных Qt-компонентов и замер оформления.

Не запускает сервисы, загрузчик приложения или БД пациентов. Все состояния
показываются на синтетических значениях; настройка темы сохраняться не может.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import statistics
import sys
import tempfile
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from _local_rem_card_bootstrap import bootstrap_local_rem_card

bootstrap_local_rem_card()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--platform", default="offscreen")
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    os.environ["QT_QPA_PLATFORM"] = args.platform
    os.environ["REMCARD_FULL_RUNTIME_THEME"] = "1"
    with tempfile.TemporaryDirectory(prefix="remcard-theme-preview-") as folder:
        os.environ["REMCARD_STYLE_SETTINGS_PATH"] = str(Path(folder)/"style.json")
        from PySide6.QtGui import QFont
        from PySide6.QtWidgets import (QApplication, QWidget, QVBoxLayout, QHBoxLayout,
            QLabel, QPushButton, QLineEdit, QComboBox, QTableWidget, QTableWidgetItem, QTabWidget)
        from rem_card.ui.styles.theme_manager import get_theme_manager
        from rem_card.ui.styles.theme_runtime import set_widget_style
        from rem_card.ui.styles.theme import STYLE_SECTOR8_BUTTON
        from rem_card.ui.shared.theme_switch import ThemeSwitch
        from rem_card.ui.patient_bed_management.tabs.diagnosis_tab import DiagnosisTabWidget
        from rem_card.ui.shared.chart_widget import ChartWidget
        app = QApplication.instance() or QApplication([])
        app.setFont(QFont("Segoe UI", 10))
        manager = get_theme_manager()
        manager.load("doctor")
        manager.apply_to_app(app)
        # Любая попытка записи предпочтения из preview — ошибка.
        manager.storage.save = lambda *a: (_ for _ in ()).throw(AssertionError("preview cannot save"))
        window = QWidget()
        window.setWindowTitle("RemCard — проверка тем на демонстрационных данных")
        window.resize(1050, 760)
        layout = QVBoxLayout(window)
        title = QLabel("Ремкарта · демонстрационные данные")
        set_widget_style(title, "font-size: 20px; font-weight: 600; color: #17233f; padding: 8px;")
        layout.addWidget(title)
        tabs = QTabWidget()
        layout.addWidget(tabs, 1)
        page = QWidget()
        page_layout = QVBoxLayout(page)
        fields = QHBoxLayout()
        fields.addWidget(QLabel("Палата 1 · Койка 2"))
        field = QLineEdit("Демонстрационный пациент")
        set_widget_style(field, "color: #253858; background: #ffffff; border: 1px solid #dbe5ef; border-radius: 5px; padding: 6px;")
        fields.addWidget(field)
        combo = QComboBox()
        combo.addItems(["Текущая смена", "Предыдущая смена"])
        fields.addWidget(combo)
        page_layout.addLayout(fields)
        class DemoMKB:
            def search_diagnoses(self, *a, **k): return []
            def find_diagnosis_by_code(self, *a, **k): return None
        diagnosis = DiagnosisTabWidget(DemoMKB(), show_operations=False)
        diagnosis.diagnosis_text_input.setPlainText("Пупочная грыжа без непроходимости или гангрены")
        page_layout.addWidget(diagnosis)
        table = QTableWidget(3, 4)
        table.setHorizontalHeaderLabels(["Назначение", "Время", "Состояние", "Комментарий"])
        values = [("Демонстрационная запись", "08:00", "Выполнено", "Без особенностей"),
                  ("Демонстрационная запись", "12:00", "Запланировано", "Проверить"),
                  ("Демонстрационная запись", "16:00", "Отменено", "Комментарий")]
        for row, items in enumerate(values):
            for column, text in enumerate(items): table.setItem(row, column, QTableWidgetItem(text))
        table.horizontalHeader().setStretchLastSection(True)
        table.setAlternatingRowColors(True)
        table.setMaximumHeight(155)
        table.selectRow(1)
        page_layout.addWidget(table)
        states = QHBoxLayout()
        for text, color in [("Обычное поле", "#eef3f8"), ("Предупреждение", "#fff0da"), ("Ошибка", "#ffdcde"), ("Выполнено", "#ecfdfb")]:
            label = QLabel(text)
            set_widget_style(label, f"color: #172033; background: {color}; border: 1px solid #bdc3c7; border-radius: 4px; padding: 9px;")
            states.addWidget(label)
        page_layout.addLayout(states)
        tabs.addTab(page, "Карта пациента")
        chart = ChartWidget()
        tabs.addTab(chart, "График витальных функций")
        footer = QHBoxLayout()
        switch = ThemeSwitch()
        footer.addWidget(switch)
        footer.addStretch()
        for caption in ("Архив", "Добавить пациента", "Настройки", "Выход"):
            button = QPushButton(caption)
            button.setMinimumHeight(32)
            set_widget_style(button, STYLE_SECTOR8_BUTTON)
            footer.addWidget(button)
        layout.addLayout(footer)
        window.show()
        app.processEvents()
        result = {}
        for mode in ("light", "dark", "light"):
            manager.set_mode(mode, save=False)
            app.processEvents()
            window.grab().save(str(args.output/f"gallery-{mode}.png"))
            tabs.setCurrentIndex(1)
            app.processEvents()
            window.grab().save(str(args.output/f"chart-{mode}.png"))
            tabs.setCurrentIndex(0)
        for mode in ("light", "dark"):
            manager.set_mode(mode, save=False)
            app.processEvents()
            samples=[]
            for _ in range(20):
                start=time.perf_counter()
                group=QWidget()
                grid=QVBoxLayout(group)
                for i in range(60):
                    row=QWidget(group)
                    row_layout=QHBoxLayout(row)
                    for caption in ("Поле", "Кнопка", "Состояние"):
                        label=QLabel(caption)
                        set_widget_style(label, "color: #253858; background: #ffffff; border: 1px solid #dbe5ef; padding: 2px;")
                        row_layout.addWidget(label)
                    grid.addWidget(row)
                group.resize(500,800)
                group.grab()
                samples.append((time.perf_counter()-start)*1000)
                group.deleteLater()
                from PySide6.QtCore import QCoreApplication, QEvent
                QCoreApplication.sendPostedEvents(None, QEvent.DeferredDelete)
            result[mode]={"median_ms":statistics.median(samples),"p95_ms":sorted(samples)[18],"samples_ms":samples}
        switch_samples=[]
        for i in range(10):
            start=time.perf_counter()
            manager.set_mode("light" if i%2 else "dark",save=False)
            app.processEvents()
            switch_samples.append((time.perf_counter()-start)*1000)
        result["switch_median_ms"]=statistics.median(switch_samples)
        result["scope"]="synthetic Qt gallery; no patient DB; not full-card startup"
        (args.output/"timings.json").write_text(json.dumps(result,indent=2),encoding="utf-8")
        print(json.dumps(result,ensure_ascii=False))
        window.deleteLater()
        QCoreApplication.sendPostedEvents(None,QEvent.DeferredDelete)


if __name__ == "__main__":
    main()
