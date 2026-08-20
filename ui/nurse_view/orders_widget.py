from PySide6.QtWidgets import QWidget, QVBoxLayout, QListWidget, QListWidgetItem, QCheckBox, QHBoxLayout, QLabel

class OrdersListWidget(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.layout = QVBoxLayout(self)
        self.layout.addWidget(QLabel("Назначения"))
        self.list_widget = QListWidget()
        self.layout.addWidget(self.list_widget)

    def update_orders(self, orders):
        self.list_widget.clear()
        for order in orders:
            item = QListWidgetItem()
            widget = QWidget()
            layout = QHBoxLayout(widget)
            
            checkbox = QCheckBox(f"{order.scheduled_time.strftime('%H:%M')} - {order.description}")
            checkbox.setChecked(order.status == 'done')
            checkbox.setProperty("order_id", order.id)
            
            layout.addWidget(checkbox)
            item.setSizeHint(widget.sizeHint())
            self.list_widget.addItem(item)
            self.list_widget.setItemWidget(item, widget)
            
            # Сохраняем ссылку на чекбокс для подключения события в контроллере
            widget.checkbox = checkbox
