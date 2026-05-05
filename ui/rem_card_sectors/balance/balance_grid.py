from PySide6.QtWidgets import (QTableWidget, QTableWidgetItem, QHeaderView, QAbstractItemView)
from PySide6.QtCore import Qt, Signal, QTimer
from PySide6.QtGui import QColor, QPainter, QPen
from datetime import datetime

class BalanceGridWidget(QTableWidget):
    """Почасовая сетка для отображения и выбора ячеек баланса выведения."""
    cell_selected = Signal(int, int) # row_idx, hour (0-23)

    def __init__(self, parent=None):
        super().__init__(5, 24, parent)
        self.rows_map = ["urine", "drain_output", "ng_output", "stool", "other_output"]
        self.row_labels = ["Диурез", "Дренажи", "ЖКТ (зонд)", "Рвота", "Другое"]
        self.now_line_color = QColor(255, 165, 0, 180) # Оранжевый (с прозрачностью)
        
        self.setup_ui()
        
        # Таймер для обновления маркера "Сейчас"
        self.timer = QTimer(self)
        self.timer.timeout.connect(self.viewport().update)
        self.timer.start(60000) # Раз в минуту

    def setup_ui(self):
        self.setHorizontalHeaderLabels([f"{h:02d}" for h in range(8, 24)] + [f"{h:02d}" for h in range(0, 8)])
        self.setVerticalHeaderLabels(self.row_labels)
        
        header = self.horizontalHeader()
        header.setSectionResizeMode(QHeaderView.Stretch)
        
        v_header = self.verticalHeader()
        v_header.setSectionResizeMode(QHeaderView.Stretch)
        v_header.setStyleSheet("font-weight: bold;")

        self.setSelectionMode(QAbstractItemView.SingleSelection)
        self.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.setFocusPolicy(Qt.NoFocus)
        
        self.setStyleSheet("""
            QTableWidget {
                gridline-color: #bdc3c7;
                background-color: white;
                border: 1px solid #bdc3c7;
                border-radius: 4px;
                font-size: 12px;
            }
            QTableWidget::item:selected {
                background-color: #e3f2fd;
                color: #000;
                border: 2px solid #6c757d;
            }
            QHeaderView::section {
                background-color: #f1f2f6;
                padding: 4px;
                border: 1px solid #bdc3c7;
                font-weight: bold;
            }
        """)

        self.cellClicked.connect(self._on_cell_clicked)

    def paintEvent(self, event):
        # Сначала стандартная отрисовка таблицы
        super().paintEvent(event)
        
        # Рисуем линию "Сейчас" поверх содержимого
        painter = QPainter(self.viewport())
        painter.setRenderHint(QPainter.Antialiasing, True)
        
        now = datetime.now()
        hour = now.hour
        minute = now.minute
        
        # Рассчитываем колонку: сетка начинается с 08:00
        # Колонки 0..15 соответствуют 08..23
        # Колонки 16..23 соответствуют 00..07
        if hour >= 8:
            col = hour - 8
        else:
            col = hour + 16
            
        # Получаем прямоугольник текущей колонки
        # Мы рисуем линию через все строки, поэтому берем ширину из колонки, а высоту из вьюпорта
        if col < self.columnCount():
            rect = self.visualRect(self.model().index(0, col))
            if rect.isValid():
                # Рассчитываем X внутри колонки
                pos_x = rect.left() + int(rect.width() * (minute / 60.0))
                
                # Рисуем оранжевую линию
                painter.setPen(QPen(self.now_line_color, 2))
                painter.drawLine(pos_x, 0, pos_x, self.viewport().height())
        
        painter.end()

    def _on_cell_clicked(self, row, col):
        # Преобразуем индекс колонки (0-23) в реальный час (8-7)
        hour = (col + 8) % 24
        self.cell_selected.emit(row, hour)

    def update_data(self, hourly_data):
        """
        Обновляет значения в сетке с сохранением текущего выделения.
        hourly_data: dict { hour: { 'urine': val, ... } }
        """
        # 1. Запоминаем текущую выбранную ячейку
        curr_row = self.currentRow()
        curr_col = self.currentColumn()
        
        # Блокируем сигналы на время обновления, чтобы не вызывать лишних событий cell_selected
        self.blockSignals(True)
        
        # 2. Умное обновление данных без clearContents()
        for col in range(24):
            hour = (col + 8) % 24
            data = hourly_data.get(hour, {})
            for row, key in enumerate(self.rows_map):
                val = data.get(key, 0)
                new_text = f"{int(val)}" if val > 0 else ""
                
                existing_item = self.item(row, col)
                if existing_item:
                    if existing_item.text() != new_text:
                        existing_item.setText(new_text)
                else:
                    item = QTableWidgetItem(new_text)
                    item.setTextAlignment(Qt.AlignCenter)
                    self.setItem(row, col, item)

        # 3. Восстанавливаем выделение
        if curr_row >= 0 and curr_col >= 0:
            self.setCurrentCell(curr_row, curr_col)
            
        self.blockSignals(False)
                    
    def get_selected_info(self):
        """Возвращает (row_key, hour, current_value) для выделенной ячейки."""
        items = self.selectedItems()
        if items:
            item = items[0]
            row = item.row()
            col = item.column()
        else:
            row = self.currentRow()
            col = self.currentColumn()
            if row < 0 or col < 0:
                return None, None, 0
        hour = (col + 8) % 24
        row_key = self.rows_map[row]
        
        try:
            item = self.item(row, col)
            val = int(item.text()) if item and item.text() else 0
        except:
            val = 0
            
        return row_key, hour, val
