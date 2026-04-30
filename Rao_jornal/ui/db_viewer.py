import sqlite3
from datetime import datetime
from PySide6.QtWidgets import QDialog, QVBoxLayout, QHBoxLayout, QTableWidget, QTableWidgetItem, QPushButton, QLineEdit, QLabel, QHeaderView, QWidget, QFrame
from PySide6.QtCore import Qt, QPoint

class DatabaseViewerDialog(QDialog):
    def __init__(self, db_manager, parent=None):
        super().__init__(parent)
        self.db_manager = db_manager
        self.setWindowTitle("Просмотр базы данных")
        self.resize(1200, 700)
        
        self.setWindowFlags(self.windowFlags() | Qt.FramelessWindowHint)
        self.setAttribute(Qt.WA_TranslucentBackground)
        
        self.bg_color = "#f2f3ee"
        self.border_color = "#c9c9b4"
        self.accent_color = "#8a8a68"

        self._drag_pos = QPoint()
        
        self._init_ui()
        self._load_data()

    def _init_ui(self):
        self.main_layout = QVBoxLayout(self)
        self.main_layout.setContentsMargins(10, 10, 10, 10)
        
        self.container = QWidget()
        self.container.setObjectName("container")
        self.container.setStyleSheet(f"""
            QWidget#container {{
                background-color: {self.bg_color};
                border: 2px solid {self.border_color};
                border-radius: 15px;
            }}
        """)
        self.container_layout = QVBoxLayout(self.container)
        self.container_layout.setContentsMargins(20, 20, 20, 20)
        self.container_layout.setSpacing(15)
        
        self.main_layout.addWidget(self.container)

        # Header
        header_layout = QHBoxLayout()
        title_label = QLabel("АРХИВ ПАЦИЕНТОВ")
        title_label.setStyleSheet(f"color: {self.accent_color}; font-weight: 800; font-size: 14px; letter-spacing: 1px;")
        header_layout.addWidget(title_label)
        header_layout.addStretch()
        
        self.close_btn = QPushButton("×")
        self.close_btn.setFixedSize(30, 30)
        self.close_btn.setCursor(Qt.PointingHandCursor)
        self.close_btn.setStyleSheet("""
            QPushButton { background: transparent; color: #7a7a6a; font-size: 22px; border: none; }
            QPushButton:hover { background: #ef4444; color: white; border-radius: 5px; }
        """)
        self.close_btn.clicked.connect(self.reject)
        header_layout.addWidget(self.close_btn)
        self.container_layout.addLayout(header_layout)

        # Search bar
        search_layout = QHBoxLayout()
        self.search_input = QLineEdit()
        self.search_input.setPlaceholderText("Поиск по ФИО или номеру истории болезни...")
        self.search_input.setStyleSheet(f"""
            QLineEdit {{
                padding: 10px;
                border: 1px solid {self.border_color};
                border-radius: 8px;
                background: white;
            }}
        """)
        self.search_input.textChanged.connect(self._filter_data)
        search_layout.addWidget(self.search_input)
        
        self.refresh_btn = QPushButton("ОБНОВИТЬ")
        self.refresh_btn.setFixedWidth(120)
        self.refresh_btn.setFixedHeight(38)
        self.refresh_btn.setStyleSheet(f"""
            QPushButton {{ background: {self.accent_color}; color: white; font-weight: bold; border-radius: 6px; }}
            QPushButton:hover {{ background: #707054; }}
        """)
        self.refresh_btn.clicked.connect(self._load_data)
        search_layout.addWidget(self.refresh_btn)
        
        self.container_layout.addLayout(search_layout)

        # Table
        self.table = QTableWidget()
        self.table.setColumnCount(7)
        self.table.setHorizontalHeaderLabels(["ID", "ФИО", "№ ИБ", "Койка", "Поступление", "Исход", "Диагноз"])
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.Interactive)
        self.table.horizontalHeader().setSectionResizeMode(1, QHeaderView.Stretch)
        self.table.setSelectionBehavior(QTableWidget.SelectRows)
        self.table.setEditTriggers(QTableWidget.NoEditTriggers)
        self.table.setStyleSheet("""
            QTableWidget {
                background-color: white;
                border: 1px solid #c9c9b4;
                border-radius: 4px;
                gridline-color: #f2f2f2;
            }
            QHeaderView::section {
                background-color: #f0f0e0;
                padding: 8px;
                border: none;
                border-right: 1px solid #c9c9b4;
                border-bottom: 1px solid #c9c9b4;
                font-weight: bold;
                color: #555;
            }
        """)
        self.container_layout.addWidget(self.table)

        # Footer Actions
        footer_layout = QHBoxLayout()
        
        self.report_btn = QPushButton("ОТЧЕТ И СТАТИСТИКА")
        self.report_btn.setStyleSheet(f"""
            QPushButton {{ background: white; color: {self.accent_color}; border: 2px solid {self.accent_color}; font-weight: 800; padding: 10px 20px; border-radius: 8px; }}
            QPushButton:hover {{ background: #f0f0e0; }}
        """)
        self.report_btn.clicked.connect(self._open_report_dialog)
        
        self.backup_btn = QPushButton("РЕЗЕРВНАЯ КОПИЯ")
        self.backup_btn.setStyleSheet(f"""
            QPushButton {{ background: white; color: #5d5d4a; border: 2px solid #c9c9b4; font-weight: 800; padding: 10px 20px; border-radius: 8px; }}
            QPushButton:hover {{ background: #f0f0e0; }}
        """)
        self.backup_btn.clicked.connect(self._create_backup)

        self.restore_btn = QPushButton("ВОССТАНОВИТЬ")
        self.restore_btn.setStyleSheet(f"""
            QPushButton {{ background: white; color: #5d5d4a; border: 2px solid #c9c9b4; font-weight: 800; padding: 10px 20px; border-radius: 8px; }}
            QPushButton:hover {{ background: #f0f0e0; }}
        """)
        self.restore_btn.clicked.connect(self._restore_backup)
        
        footer_layout.addWidget(self.report_btn)
        footer_layout.addStretch()
        footer_layout.addWidget(self.backup_btn)
        footer_layout.addWidget(self.restore_btn)
        
        self.container_layout.addLayout(footer_layout)

    def mousePressEvent(self, event):
        if event.button() == Qt.LeftButton:
            self._drag_pos = event.globalPosition().toPoint()

    def mouseMoveEvent(self, event):
        if event.buttons() & Qt.LeftButton:
            delta = event.globalPosition().toPoint() - self._drag_pos
            self.move(self.x() + delta.x(), self.y() + delta.y())
            self._drag_pos = event.globalPosition().toPoint()

    def _load_data(self):
        self.all_data = self.db_manager.get_all_admissions()
        self._display_data(self.all_data)

    def _display_data(self, data):
        self.table.setRowCount(0)
        from rem_card.Rao_jornal.config.settings import MKB_DB_PATH
        self.mkb_conn = None
        try:
            self.mkb_conn = sqlite3.connect(MKB_DB_PATH)
            self.mkb_cursor = self.mkb_conn.cursor()
        except: pass

        for row_data in reversed(data):
            row = self.table.rowCount()
            self.table.insertRow(row)
            
            # ID
            self.table.setItem(row, 0, QTableWidgetItem(str(row_data.get('id', ''))))
            
            # Full Name
            self.table.setItem(row, 1, QTableWidgetItem(row_data.get('full_name', '')))
            
            # History Number
            self.table.setItem(row, 2, QTableWidgetItem(row_data.get('history_number', '')))
            
            # Bed Number
            self.table.setItem(row, 3, QTableWidgetItem(str(row_data.get('bed_number', ''))))
            
            # Admission Datetime
            adm_dt = row_data.get('admission_datetime', '')
            if adm_dt:
                try: 
                    # If string, try parsing. If datetime, format it.
                    if isinstance(adm_dt, str):
                        dt_obj = datetime.strptime(adm_dt.split('.')[0], "%Y-%m-%d %H:%M:%S")
                        adm_dt = dt_obj.strftime("%d.%m.%Y %H:%M")
                    else:
                        adm_dt = adm_dt.strftime("%d.%m.%Y %H:%M")
                except: pass
            self.table.setItem(row, 4, QTableWidgetItem(adm_dt))
            
            # Outcome
            outcome = row_data.get('outcome', '')
            if not outcome:
                outcome = "В отделении"
            self.table.setItem(row, 5, QTableWidgetItem(outcome))
            
            # Diagnosis
            diag_code = row_data.get('diagnosis_code', '')
            diag_text = row_data.get('diagnosis_text', '')
            
            # Если текста нет, пробуем найти в МКБ по коду
            if not diag_text and diag_code and hasattr(self, 'mkb_cursor'):
                try:
                    cleaned_code = diag_code.strip().upper()
                    candidates = [cleaned_code]
                    if not cleaned_code.endswith(("+", "*")):
                        candidates.extend([f"{cleaned_code}+", f"{cleaned_code}*"])

                    placeholders = ", ".join("?" for _ in candidates)
                    self.mkb_cursor.execute(
                        f"""
                        SELECT name
                        FROM class_mkb
                        WHERE code COLLATE NOCASE IN ({placeholders})
                        ORDER BY CASE UPPER(code)
                            WHEN ? THEN 0
                            WHEN ? THEN 1
                            WHEN ? THEN 2
                            ELSE 3
                        END
                        LIMIT 1
                        """,
                        (*candidates, cleaned_code, f"{cleaned_code}+", f"{cleaned_code}*"),
                    )
                    res = self.mkb_cursor.fetchone()
                    if res: diag_text = res[0]
                except: pass
                
            display_diag = f"[{diag_code}] {diag_text}" if diag_code else diag_text
            self.table.setItem(row, 6, QTableWidgetItem(display_diag))

        if self.mkb_conn:
            self.mkb_conn.close()

    def _filter_data(self):
        search_text = self.search_input.text().lower()
        if not search_text:
            self._display_data(self.all_data)
            return
            
        filtered = [
            d for d in self.all_data 
            if search_text in str(d.get('full_name', '')).lower() or 
               search_text in str(d.get('history_number', '')).lower()
        ]
        self._display_data(filtered)

    def _delete_selected_patient(self):
        # Implementation if needed, but not requested for now.
        from rem_card.Rao_jornal.ui.custom_dialogs import CustomMessageBox
        pass

    def _create_backup(self):
        from rem_card.Rao_jornal.ui.custom_dialogs import CustomMessageBox
        success, msg = self.db_manager.create_backup()
        if success:
            CustomMessageBox.show_info(self, "Резервное копирование", msg)
        else:
            CustomMessageBox.show_info(self, "Ошибка", msg)

    def _restore_backup(self):
        from PySide6.QtWidgets import QFileDialog
        from rem_card.Rao_jornal.config.settings import BACKUP_DIR
        from rem_card.Rao_jornal.ui.custom_dialogs import CustomMessageBox

        file_path, _ = QFileDialog.getOpenFileName(
            self, "Выберите файл резервной копии", BACKUP_DIR, "SQLite Database (*.db)"
        )
        
        if file_path:
            if CustomMessageBox.show_question(self, "Восстановление", "ВНИМАНИЕ!\nТекущая база данных будет полностью заменена данными из бэкапа.\nПродолжить?"):
                success, msg = self.db_manager.restore_backup(file_path)
                if success:
                    CustomMessageBox.show_info(self, "Успех", msg)
                    self._load_data()
                else:
                    CustomMessageBox.show_info(self, "Ошибка", msg)

    def _open_report_dialog(self):
        from rem_card.Rao_jornal.ui.report_dialog import ReportDialog
        dialog = ReportDialog(self.db_manager, self)
        dialog.exec()

    def _open_graphs_dialog(self):
        from rem_card.Rao_jornal.ui.custom_dialogs import CustomMessageBox
        pass
