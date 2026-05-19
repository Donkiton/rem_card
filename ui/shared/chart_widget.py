import os
import pyqtgraph as pg
import warnings
from PySide6.QtWidgets import QWidget, QVBoxLayout
from PySide6.QtCore import Qt, Signal, QEvent, QRect, QTimeLine
from PySide6.QtGui import QPainter, QFont, QColor, QBrush, QPainterPath
from datetime import datetime, timedelta
from .chart_data_processor import ChartDataProcessor
from ..styles.theme import (BG_MAIN, BG_LIGHT, TEXT_PRIMARY, BORDER_COLOR, 
                            COLOR_VITAL_AD_LINE, COLOR_VITAL_AD_BG, COLOR_VITAL_PULSE, 
                            COLOR_VITAL_SPO2, COLOR_VITAL_TEMP, COLOR_VITAL_RESP, COLOR_VITAL_CVP)

pg.setConfigOption("background", "transparent")
pg.setConfigOption("foreground", "k")
pg.setConfigOption("antialias", True)

CHART_ACTIVE_INTERVAL_LOOKBACK_DAYS = max(0, int(os.environ.get("REMCARD_CHART_ACTIVE_LOOKBACK_DAYS", os.environ.get("REMCARD_CHART_LOOKBACK_DAYS", "2"))))
CHART_ACTIVE_INTERVAL_LOOKAHEAD_DAYS = max(0, int(os.environ.get("REMCARD_CHART_ACTIVE_LOOKAHEAD_DAYS", os.environ.get("REMCARD_CHART_LOOKAHEAD_DAYS", "1"))))


class TimeHeader(QWidget):
    hour_selected = Signal(int)

    def __init__(self, chart):
        super().__init__()
        self.chart = chart
        self.setFixedHeight(30)
        self.setObjectName("chart_header")
        self.setAttribute(Qt.WA_StyledBackground, True) # Р’Р°Р¶РЅРѕ: РїСЂРёРјРµРЅСЏРµРј QSS РґР»СЏ QWidget
        self.highlighted_hour = None

    def set_highlight(self, hour_idx):
        self.highlighted_hour = hour_idx
        self.update()

    def mousePressEvent(self, event):
        if event.button() == Qt.LeftButton:
            vb = self.chart.plot_widget.getViewBox()
            view = self.chart.plot_widget
            
            # РЈС‡РёС‚С‹РІР°РµРј СЃРјРµС‰РµРЅРёРµ РєРѕРЅС‚РµР№РЅРµСЂР° РіСЂР°С„РёРєР° (7px) РґР»СЏ РїРѕРїР°РґР°РЅРёСЏ РїРѕ С‡Р°СЃР°Рј
            local_x = event.pos().x() - 7
            
            scene_pos = view.mapToScene(local_x, 0)
            view_pos = vb.mapSceneToView(scene_pos)
            
            hour_idx = int(view_pos.x())
            if 0 <= hour_idx < 24:
                if self.highlighted_hour == hour_idx:
                    self.hour_selected.emit(-1)
                else:
                    self.hour_selected.emit(hour_idx)

    def paintEvent(self, event):
        # РћС‚СЂРёСЃРѕРІРєР° QSS-С„РѕРЅР° Рё СЂР°РјРѕРє
        from PySide6.QtWidgets import QStyleOption, QStyle
        opt = QStyleOption()
        opt.initFrom(self)
        self.style().drawPrimitive(QStyle.PE_Widget, opt, QPainter(self), self)

        painter = QPainter(self)
        
        # 1. РћС‚СЂРёСЃРѕРІРєР° РїРѕРґСЃРІРµС‚РєРё РІС‹РґРµР»РµРЅРЅРѕРіРѕ С‡Р°СЃР° (РЎРњР•Р©Р•РќРђ РЅР° 7px РґР»СЏ СЃРѕРІРїР°РґРµРЅРёСЏ СЃ РіСЂР°С„РёРєРѕРј)
        if self.highlighted_hour is not None:
            vb = self.chart.plot_widget.getViewBox()
            view = self.chart.plot_widget
            p1 = view.mapFromScene(vb.mapViewToScene(pg.Point(self.highlighted_hour, 0)))
            p2 = view.mapFromScene(vb.mapViewToScene(pg.Point(self.highlighted_hour + 1, 0)))
            
            highlight_rect = QRect(p1.x() + 7, 0, p2.x() - p1.x(), self.height())
            painter.fillRect(highlight_rect, QColor(100, 150, 255, 60))

        # 2. РћС‚СЂРёСЃРѕРІРєР° С‚РµРєСЃС‚Р° РІСЂРµРјРµРЅРё (Р‘Р•Р— РЎРњР•Р©Р•РќРРЇ, РєР°Рє Р±С‹Р»Рѕ РґРѕ РїСЂР°РІРѕРє)
        if not self.chart.start_time:
            painter.end()
            return

        painter.setPen(Qt.black)
        vb = self.chart.plot_widget.getViewBox()
        view = self.chart.plot_widget

        for i in range(24):
            # РСЃРїРѕР»СЊР·СѓРµРј С‚Рµ Р¶Рµ РєРѕРѕСЂРґРёРЅР°С‚С‹ РіСЂР°РЅРёС† С‡Р°СЃР°, С‡С‚Рѕ Рё РґР»СЏ РїРѕРґСЃРІРµС‚РєРё (p1, p2)
            p_start = view.mapFromScene(vb.mapViewToScene(pg.Point(i, 0)))
            p_end = view.mapFromScene(vb.mapViewToScene(pg.Point(i + 1, 0)))
            
            # Р’С‹С‡РёСЃР»СЏРµРј С†РµРЅС‚СЂ С‡Р°СЃР° СЃ СѓС‡РµС‚РѕРј СЃРјРµС‰РµРЅРёСЏ РіСЂР°С„РёРєР° 7px
            x_center = (p_start.x() + p_end.x()) / 2 + 7

            text = (self.chart.start_time + timedelta(hours=i)).strftime('%H:%M')
            
            if i == self.highlighted_hour:
                painter.setFont(self.chart.value_font)
            else:
                f = painter.font()
                f.setBold(False)
                painter.setFont(f)

            # РћС‚СЂРёСЃРѕРІС‹РІР°РµРј С‚РµРєСЃС‚ РІ Р±Р»РѕРєРµ С€РёСЂРёРЅРѕР№ 50px, С†РµРЅС‚СЂРёСЂСѓСЏ РµРіРѕ РїРѕ x_center
            painter.drawText(
                int(x_center - 25),
                5,
                50,
                20,
                Qt.AlignCenter,
                text
            )

        painter.end()


class TooltipItem(pg.TextItem):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.setOpacity(1.0)
        
    def paint(self, p, *args):
        # РћС‚СЂРёСЃРѕРІС‹РІР°РµРј РєР°СЃС‚РѕРјРЅС‹Р№ С„РѕРЅ СЃ Р·Р°РєСЂСѓРіР»РµРЅРЅС‹РјРё РєСЂР°СЏРјРё
        p.setRenderHint(QPainter.Antialiasing)
        p.setPen(pg.mkPen('#544d4d', width=1))
        # Р“Р°СЂР°РЅС‚РёСЂРѕРІР°РЅРЅРѕ РЅРµРїСЂРѕР·СЂР°С‡РЅС‹Р№ С„РѕРЅ #ebecef
        p.setBrush(pg.mkBrush(QColor('#ebecef')))
        
        rect = self.boundingRect()
        p.drawRoundedRect(rect, 5, 5)
        
        # Р’С‹Р·С‹РІР°РµРј РѕСЂРёРіРёРЅР°Р»СЊРЅСѓСЋ РѕС‚СЂРёСЃРѕРІРєСѓ С‚РµРєСЃС‚Р°
        super().paint(p, *args)


class ChartWidget(QWidget):
    column_clicked = Signal(int)

    def __init__(self, parent=None):
        super().__init__(parent)
        
        # Р“Р»РѕР±Р°Р»СЊРЅРѕРµ РїРѕРґР°РІР»РµРЅРёРµ РІРѕСЂРЅРёРЅРіРѕРІ numpy РґР»СЏ СЌС‚РѕРіРѕ РІРёРґР¶РµС‚Р° (СЂРµС€Р°РµС‚ РїСЂРѕР±Р»РµРјСѓ All-NaN slice РІ pyqtgraph)
        # РџРѕРґР°РІР»СЏРµРј РІСЃРµ RuntimeWarning, С‚Р°Рє РєР°Рє pyqtgraph С‡Р°СЃС‚Рѕ РіРµРЅРµСЂРёСЂСѓРµС‚ РёС… РїСЂРё СЂР°Р±РѕС‚Рµ СЃ NaN
        warnings.filterwarnings("ignore", category=RuntimeWarning)

        self.layout = QVBoxLayout(self)
        # РЎРґРІРёРіР°РµРј СЂР°РјРєСѓ (СЃР»РѕР№ 2): СЃРІРµСЂС…Сѓ 3px, СЃРЅРёР·Сѓ 3px, СЃРїСЂР°РІР° 0px (СѓРјРµРЅСЊС€РµРЅ РґР»СЏ СЃРґРІРёРіР° РІРїСЂР°РІРѕ)
        self.layout.setContentsMargins(0, 3, 0, 5)
        self.layout.setSpacing(0)

        self.value_font = QFont("Segoe UI", 10, QFont.Bold)

        self.header_spacer = TimeHeader(self)
        self.header_spacer.setFixedHeight(30)
        self.header_spacer.hour_selected.connect(self.on_hour_selected)
        self.layout.addWidget(self.header_spacer)

        self.chart_container = QWidget()
        self.chart_container.setObjectName("chart_body")
        self.chart_layout = QVBoxLayout(self.chart_container)
        self.chart_layout.setContentsMargins(7, 0, 7, 7) 

        self.plot_widget = pg.PlotWidget()
        self.plot_widget.setBackground('transparent') # РџСЂРѕР·СЂР°С‡РЅС‹Р№ С„РѕРЅ РІСЃРµРіРѕ РІРёРґР¶РµС‚Р°
        
        # Р—Р°РєСЂР°С€РёРІР°РµРј Р±РµР»С‹Рј С‚РѕР»СЊРєРѕ СЃР°РјСѓ РѕР±Р»Р°СЃС‚СЊ РіСЂР°С„РёРєР° (ViewBox)
        vb = self.plot_widget.getViewBox()
        vb.setBackgroundColor('w')
        
        # РџРѕРґРєР»СЋС‡Р°РµРј СЃРёРіРЅР°Р» РёР·РјРµРЅРµРЅРёСЏ РґРёР°РїР°Р·РѕРЅР° Рє РѕР±РЅРѕРІР»РµРЅРёСЋ Р·Р°РіРѕР»РѕРІРєР°, 
        # С‡С‚РѕР±С‹ РІСЂРµРјСЏ РІСЃРµРіРґР° "СЃР°РјРѕ" РІС‹СЂР°РІРЅРёРІР°Р»РѕСЃСЊ РїРѕ СЃРµС‚РєРµ РїРѕСЃР»Рµ СЂР°СЃС‡РµС‚РѕРІ РґРІРёР¶РєР°
        vb.sigRangeChanged.connect(lambda *args: self.header_spacer.update())

        self.plot_widget.showGrid(x=False, y=True, alpha=0.5)
        self.plot_widget.setMouseEnabled(x=False, y=False)
        self.plot_widget.setMenuEnabled(False)
        self.plot_widget.hideButtons()
        self.plot_widget.viewport().installEventFilter(self)

        self.plot_widget.hideAxis('bottom')
        self.plot_widget.hideAxis('top')

        ax_left = self.plot_widget.getAxis('left')
        ax_left.setTicks([[(v, str(v)) for v in range(0, 261, 10)]])

        self.plot_widget.setXRange(0, 24, padding=0)
        self.plot_widget.setYRange(0, 260, padding=0)
        self.plot_widget.getViewBox().setLimits(xMin=0, xMax=24, yMin=0, yMax=260)

        self.vitals_data = []
        self.start_time = None
        self.status_service = None
        self.admission_id = None
        
        self.colors = {
            'ad': COLOR_VITAL_AD_LINE,
            'ad_fill': COLOR_VITAL_AD_BG,
            'pulse': COLOR_VITAL_PULSE,
            'spo2': COLOR_VITAL_SPO2,
            'temp': COLOR_VITAL_TEMP,
            'rr': COLOR_VITAL_RESP,
            'cvp': COLOR_VITAL_CVP
        }

        pg.setConfigOptions(antialias=True)
        
        self.curve_items = []
        self.fill_items = []
        self._curve_by_key = {}
        self._scatter_items = {}

        self._init_reusable_plot_items()

        self.current_vitals = []
        self._last_render_key = None
        
        self.slice_line = pg.InfiniteLine(angle=90, movable=False, 
                                          pen=pg.mkPen(color='#888', style=Qt.DashLine, width=2))
        self.slice_line.setZValue(100)
        self.slice_line.setOpacity(0) # РР·РЅР°С‡Р°Р»СЊРЅРѕ РїСЂРѕР·СЂР°С‡РЅРѕ
        self.slice_line.hide()
        self.plot_widget.addItem(self.slice_line)
        
        # РСЃРїРѕР»СЊР·СѓРµРј РЅР°С€ РєР°СЃС‚РѕРјРЅС‹Р№ РєР»Р°СЃСЃ Рё РїСЂРёРІСЏР·С‹РІР°РµРј РµРіРѕ Рє PlotItem (РІС‹С€Рµ РѕСЃРµР№)
        self.tooltip = TooltipItem(html="", anchor=(0, 1))
        self.tooltip.setParentItem(self.plot_widget.getPlotItem())
        self.tooltip.setZValue(1000) # РЈР»СЊС‚РёРјР°С‚РёРІРЅС‹Р№ Z-index РїРѕРІРµСЂС… СЃРµС‚РєРё (РѕСЃРµР№)
        self.tooltip.setOpacity(0)
        self.tooltip.hide()
        
        # Р“СЂСѓРїРїР° Р°РЅРёРјР°С†РёР№ РґР»СЏ РїР»Р°РІРЅС‹С… РїРµСЂРµС…РѕРґРѕРІ
        self.fade_timeline = QTimeLine(100, self)
        self.fade_timeline.setFrameRange(0, 100)
        self.fade_timeline.valueChanged.connect(self._on_fade_step)
        self.fade_timeline.finished.connect(self._on_fade_finished)
        self._fade_action = None
        self._fade_target_state = None

        # РќР°РґРµР¶РЅС‹Р№ РіР»РѕР±Р°Р»СЊРЅС‹Р№ РїРµСЂРµС…РІР°С‚ РєР»РёРєР°
        # РЈРІРµР»РёС‡РёРІР°РµРј СЂР°РґРёСѓСЃ РєР»РёРєР°, С‡С‚РѕР±С‹ РёРіРЅРѕСЂРёСЂРѕРІР°С‚СЊ РјРёРєСЂРѕРґРІРёР¶РµРЅРёСЏ РјС‹С€Рё (СЃ 3 РґРѕ 10 РїРёРєСЃРµР»РµР№)
        self.plot_widget.scene().setClickRadius(10)
        self.plot_widget.scene().sigMouseClicked.connect(self.on_scene_clicked)

        for i in range(25):
            self.plot_widget.addItem(pg.InfiniteLine(pos=i, angle=90,
                                                    pen=pg.mkPen(color=(0, 0, 0, 50))))

        self.chart_layout.addWidget(self.plot_widget)
        self.layout.addWidget(self.chart_container)
        
        # Р’С‹РІРѕРґРёРј С€Р°РїРєСѓ РЅР° РІРµСЂС…РЅРёР№ СЃР»РѕР№ (СЃР»РѕР№ 3)
        self.header_spacer.show()
        self.header_spacer.raise_()

        self.setStyleSheet(f"""
            QWidget#chart_header {{
                background-color: {BG_LIGHT} !important;
                border-top: 1.5px solid {BORDER_COLOR} !important;
                border-right: 1.5px solid {BORDER_COLOR} !important;
                border-bottom: 0.5px solid {BORDER_COLOR} !important;
                border-top-right-radius: 5px !important;
                border-top-left-radius: 0px !important;
                border-left: none !important;
            }}
            QWidget#chart_body {{
                background-color: {BG_MAIN} !important;
                border-right: 1.5px solid {BORDER_COLOR} !important;
                border-bottom: 1.5px solid {BORDER_COLOR} !important;
                border-bottom-right-radius: 5px !important;
                border-left: none !important;
                border-top: none !important;
            }}
        """)

    def _init_reusable_plot_items(self):
        curve_specs = (
            ("sys", "ad", 2),
            ("dia", "ad", 2),
            ("pulse", "pulse", 3),
            ("spo2", "spo2", 2),
            ("temp", "temp", 2),
            ("rr", "rr", 2),
            ("cvp", "cvp", 2),
        )
        for key, color_key, width in curve_specs:
            curve = pg.PlotDataItem(pen=pg.mkPen(self.colors[color_key], width=width))
            curve.setZValue(10)
            self.plot_widget.addItem(curve)
            self._curve_by_key[key] = curve
            self.curve_items.append(curve)

        fill = pg.FillBetweenItem(
            self._curve_by_key["sys"],
            self._curve_by_key["dia"],
            brush=pg.mkBrush(self.colors["ad_fill"]),
        )
        fill.setZValue(-10)
        self.plot_widget.addItem(fill)
        self.fill_items.append(fill)

        self.scatter_vitals = pg.ScatterPlotItem(size=3, pen=None, brush=self.colors["ad"])
        self.scatter_vitals.setZValue(20)
        self.plot_widget.addItem(self.scatter_vitals)
        self._scatter_items["ad"] = self.scatter_vitals

        for key in ("pulse", "spo2", "temp", "rr", "cvp"):
            scatter = pg.ScatterPlotItem(size=3, pen=None, brush=self.colors[key])
            scatter.setZValue(20)
            self.plot_widget.addItem(scatter)
            self._scatter_items[key] = scatter

    def eventFilter(self, source, event):
        if event.type() == QEvent.Wheel:
            return True
        return super().eventFilter(source, event)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        # РџСЂРёРЅСѓРґРёС‚РµР»СЊРЅРѕ РѕР±РЅРѕРІР»СЏРµРј Р·Р°РіРѕР»РѕРІРѕРє РїСЂРё РёР·РјРµРЅРµРЅРёРё СЂР°Р·РјРµСЂРѕРІ, 
        # С‡С‚РѕР±С‹ РІСЂРµРјСЏ РІСЃРµРіРґР° Р±С‹Р»Рѕ РІС‹СЂРѕРІРЅРµРЅРѕ РїРѕ СЃРµС‚РєРµ РіСЂР°С„РёРєР°
        self.header_spacer.update()
        self._hide_slice_instant()

    def showEvent(self, event):
        super().showEvent(event)
        # РџСЂРё РїРµСЂРІРѕРј РїРѕРєР°Р·Рµ РёР»Рё РїРµСЂРµРєР»СЋС‡РµРЅРёРё РІРєР»Р°РґРѕРє РїСЂРёРЅСѓРґРёС‚РµР»СЊРЅРѕ 
        # РїРµСЂРµСЃС‡РёС‚С‹РІР°РµРј РєРѕРѕСЂРґРёРЅР°С‚С‹ С‡РµСЂРµР· РЅРµР±РѕР»СЊС€СѓСЋ РїР°СѓР·Сѓ, С‡С‚РѕР±С‹ 
        # РіСЂР°С„РёС‡РµСЃРєРёР№ РґРІРёР¶РѕРє СѓСЃРїРµР» РёРЅРёС†РёР°Р»РёР·РёСЂРѕРІР°С‚СЊ ViewBox
        from PySide6.QtCore import QTimer
        QTimer.singleShot(50, self.header_spacer.update)
        QTimer.singleShot(200, self.header_spacer.update)

    def on_hour_selected(self, hour_idx):
        # РЎС‚Р°СЂС‹Р№ РјРµС…Р°РЅРёР·Рј РІС‹РґРµР»РµРЅРёСЏ С‡Р°СЃР° РѕС‚РєР»СЋС‡РµРЅ РІ РїРѕР»СЊР·Сѓ СЃРёСЃС‚РµРјС‹ СЃР»Р°Р№СЃРѕРІ
        pass

    def _hide_slice_instant(self):
        if hasattr(self, 'fade_timeline'):
            self.fade_timeline.stop()
        self.slice_line.hide()
        self.tooltip.hide()
        self.slice_line.setOpacity(0)
        self.tooltip.setOpacity(0)

    def _on_fade_step(self, value):
        op = value / 100.0
        if self._fade_action == "out":
            op = 1.0 - op
            
        self.slice_line.setOpacity(op)
        self.tooltip.setOpacity(op)

    def _on_fade_finished(self):
        if self._fade_action == "out":
            self.slice_line.hide()
            self.tooltip.hide()
        else:
            self.slice_line.setOpacity(1.0)
            self.tooltip.setOpacity(1.0)

    def _fade_out(self, duration=100):
        self.fade_timeline.stop()
        self._fade_action = "out"
        self.fade_timeline.start()

    def _apply_slice_state(self, exact_hour, tooltip_pos, anchor, html):
        self.slice_line.setPos(exact_hour)
        self.tooltip.setHtml(html)
        self.tooltip.setAnchor(anchor)
        self.tooltip.setPos(tooltip_pos)

    def _fade_in_to(self, exact_hour, tooltip_pos, anchor, html, is_update=False):
        self.fade_timeline.stop()
        
        self._apply_slice_state(exact_hour, tooltip_pos, anchor, html)
        
        self.slice_line.show()
        self.tooltip.show()
        
        if is_update:
            # РџСЂРё РѕР±РЅРѕРІР»РµРЅРёРё РїСЂРѕСЃС‚Рѕ РїРµСЂРµРєР»СЋС‡Р°РµРј РјРіРЅРѕРІРµРЅРЅРѕ, РЅРѕ СЃ РЅРµР±РѕР»СЊС€РёРј С„РµР№РґРѕРј РґР»СЏ РєСЂР°СЃРѕС‚С‹
            self.slice_line.setOpacity(1.0)
            self.tooltip.setOpacity(1.0)
        else:
            self._fade_action = "in"
            self.fade_timeline.start()

    def on_scene_clicked(self, event):
        if event.button() == Qt.LeftButton:
            pos = event.scenePos()
            vb = self.plot_widget.getViewBox()
            
            # 1. Р–РµСЃС‚РєР°СЏ РїСЂРѕРІРµСЂРєР°: РєР»РёРє РґРѕР»Р¶РµРЅ Р±С‹С‚СЊ СЃС‚СЂРѕРіРѕ РІ РѕР±Р»Р°СЃС‚Рё РіСЂР°С„РёРєРѕРІ (ViewBox)
            # Р”РѕР±Р°РІР»СЏРµРј 1 РїРёРєСЃРµР»СЊ РґРѕРїСѓСЃРєР° РґР»СЏ РіСЂР°РЅРёС†
            vb_rect = vb.sceneBoundingRect()
            vb_rect.adjust(-1, 0, 1, 0)
            
            if not vb_rect.contains(pos):
                self._fade_out()
                return
                
            mouse_point = vb.mapSceneToView(pos)
            x_click = mouse_point.x()
            
            # 2. РџРѕР»РЅРѕСЃС‚СЊСЋ РёРіРЅРѕСЂРёСЂСѓРµРј РІСЃС‘, С‡С‚Рѕ Р»РµРІРµРµ 8:00 (X < 0) РёР»Рё РїСЂР°РІРµРµ РєРѕРЅС†Р° (X > 24)
            if x_click < -0.001 or x_click > 24.001:
                self._fade_out()
                return
            
            if not self.start_time or not self.current_vitals:
                return
                
            # РС‰РµРј Р±Р»РёР¶Р°Р№С€РёР№ РѕСЂРёРіРёРЅР°Р»СЊРЅС‹Р№ РѕР±СЉРµРєС‚ Vital РїРѕ РѕСЃРё X
            closest_dist = float('inf')
            closest_vital = None
            closest_exact_hour = None
            
            s_time = self.start_time.replace(microsecond=0)
            
            for v in self.current_vitals:
                # РРіРЅРѕСЂРёСЂСѓРµРј Р°Р±СЃРѕР»СЋС‚РЅРѕ РїСѓСЃС‚С‹Рµ Р·Р°РїРёСЃРё РїСЂРё РїРѕРёСЃРєРµ
                v_fields = [v.sys, v.dia, v.pulse, v.temp, v.spo2]
                if all(val is None for val in v_fields):
                    continue

                delta_sec = int((v.timestamp.replace(microsecond=0) - s_time).total_seconds())
                exact_hour = delta_sec / 3600.0
                dist = abs(exact_hour - x_click)
                
                # Р”РѕРїСѓСЃРє 0.6 (36 РјРёРЅСѓС‚)
                if dist < 0.6 and dist < closest_dist:
                    closest_dist = dist
                    closest_vital = v
                    closest_exact_hour = exact_hour
            
            if closest_vital is not None:
                time_str = closest_vital.timestamp.strftime('%H:%M')
                
                html = f"<div style='font-family: Segoe UI; font-size: 13px; padding: 5px; background-color: #ebecef;'>"
                html += f"<b>Время: {time_str}</b><br>"
                
                def f_val(val):
                    if val is None: return "-"
                    try: 
                        v = float(val)
                        if v.is_integer(): return str(int(v))
                        return f"{v:.1f}"
                    except: return "-"
                
                if closest_vital.sys is not None or closest_vital.dia is not None:
                    sys_v = f_val(closest_vital.sys)
                    dia_v = f_val(closest_vital.dia)
                    html += f"<span style='color: {self.colors['ad']};'>АД: {sys_v}/{dia_v}</span><br>"
                
                if closest_vital.pulse is not None:
                    pulse_v = f_val(closest_vital.pulse)
                    html += f"<span style='color: {self.colors['pulse']};'>ЧСС: {pulse_v}</span><br>"
                
                if closest_vital.temp is not None:
                    temp_v = f_val(closest_vital.temp)
                    html += f"<span style='color: {self.colors['temp']};'>Temp: {temp_v}</span><br>"
                
                rr_v = getattr(closest_vital, 'rr', None)
                if rr_v is not None:
                    html += f"<span style='color: {self.colors['rr']};'>ЧДД: {f_val(rr_v)}</span><br>"
                
                cvp_v = getattr(closest_vital, 'cvp', None)
                if cvp_v is not None:
                    cvp_str = "Ниже нуля" if cvp_v == -1 else f_val(cvp_v)
                    html += f"<span style='color: {self.colors['cvp']};'>ЦВД: {cvp_str}</span><br>"

                if closest_vital.spo2 is not None:
                    spo2_v = f_val(closest_vital.spo2)
                    html += f"<span style='color: {self.colors['spo2']};'>SpO2: {spo2_v}</span>"
                
                html += "</div>"
                
                # РџРѕР·РёС†РёРѕРЅРёСЂСѓРµРј Tooltip (РјР°РїРїРёРЅРі РІ РєРѕРѕСЂРґРёРЅР°С‚С‹ PlotItem)
                scene_p = self.plot_widget.getViewBox().mapViewToScene(pg.Point(closest_exact_hour, mouse_point.y()))
                plot_item_p = self.plot_widget.getPlotItem().mapFromScene(scene_p)
                
                view_rect = self.plot_widget.getViewBox().viewRect()
                if closest_exact_hour > view_rect.right() - 4.0:
                    anchor = (1.1, 0.5)
                else:
                    anchor = (-0.1, 0.5)
                    
                is_update = self.slice_line.isVisible() and self.tooltip.isVisible()
                self._fade_in_to(closest_exact_hour, plot_item_p, anchor, html, is_update)
            else:
                self._fade_out()

    def highlight_hour(self, hour_idx):
        # РЎС‚Р°СЂС‹Р№ РјРµС…Р°РЅРёР·Рј РІС‹РґРµР»РµРЅРёСЏ С‡Р°СЃР° РѕС‚РєР»СЋС‡РµРЅ
        pass

    def clear_for_context(self, *, admission_id=None, start_time: datetime = None):
        """Immediately remove previous patient curves while a new snapshot is loading."""
        self.admission_id = admission_id
        self.vitals_data = []
        self.current_vitals = []
        self.start_time = start_time
        self._last_render_key = None

        if self.fade_timeline.state() == QTimeLine.Running:
            self.fade_timeline.stop()
        self._fade_action = None
        self._fade_target_state = None

        for item in getattr(self, "_curve_by_key", {}).values():
            item.setData([], [])
        self._clear_fill_paths()
        self.scatter_vitals.setData([])
        for key, item in getattr(self, "_scatter_items", {}).items():
            if key != "ad":
                item.setData([])
        self.slice_line.hide()
        self.slice_line.setOpacity(0)
        self.tooltip.hide()
        self.tooltip.setOpacity(0)
        self.header_spacer.update()
        self.plot_widget.viewport().update()

    def _clear_fill_paths(self):
        for item in getattr(self, "fill_items", []):
            item.setPath(QPainterPath())
            item.update()

    @staticmethod
    def _normalize_key_dt(value):
        if value is None:
            return ""
        if isinstance(value, datetime):
            return value.replace(microsecond=0).isoformat()
        return str(value)

    @classmethod
    def _build_intervals_key(cls, active_intervals):
        if not active_intervals:
            return ()
        return tuple(
            (cls._normalize_key_dt(start), cls._normalize_key_dt(end))
            for start, end in active_intervals
        )

    @classmethod
    def _build_vitals_key(cls, vitals):
        if not vitals:
            return (0, None, None, None)

        first = vitals[0]
        last = vitals[-1]
        first_key = (
            int(getattr(first, "id", 0) or 0),
            cls._normalize_key_dt(getattr(first, "timestamp", None)),
            cls._normalize_key_dt(getattr(first, "updated_at", None)),
        )
        last_key = (
            int(getattr(last, "id", 0) or 0),
            cls._normalize_key_dt(getattr(last, "timestamp", None)),
            cls._normalize_key_dt(getattr(last, "updated_at", None)),
        )

        max_sync = ("", 0)
        for vital in vitals:
            candidate = (
                cls._normalize_key_dt(getattr(vital, "updated_at", None)),
                int(getattr(vital, "id", 0) or 0),
            )
            if candidate > max_sync:
                max_sync = candidate

        return (len(vitals), first_key, last_key, max_sync)

    def update_data(self, vitals, start_time: datetime, active_intervals=None):
        if start_time is None:
            return

        self.vitals_data = vitals
        self.start_time = start_time
        
        # РњС‹ Р‘РћР›Р¬РЁР• РќР• СЃРєСЂС‹РІР°РµРј СЃСЂРµР· РїСЂРё РѕР±РЅРѕРІР»РµРЅРёРё РґР°РЅРЅС‹С….
        # РўР°РєРёРј РѕР±СЂР°Р·РѕРј, РµСЃР»Рё СЃСЂР°Р±Р°С‚С‹РІР°РµС‚ 4-СЃРµРєСѓРЅРґРЅС‹Р№ С‚Р°Р№РјРµСЂ С„РѕРЅРѕРІРѕРіРѕ РѕР±РЅРѕРІР»РµРЅРёСЏ,
        # РѕС‚РєСЂС‹С‚С‹Р№ С‚СѓР»С‚РёРї РѕСЃС‚Р°РЅРµС‚СЃСЏ РЅР° СЌРєСЂР°РЅРµ.
        
        # РџРѕР»СѓС‡Р°РµРј ACTIVE РёРЅС‚РµСЂРІР°Р»С‹ РґР»СЏ С„РёР»СЊС‚СЂР°С†РёРё Рё СЂР°Р·СЂС‹РІРѕРІ
        if active_intervals is None:
            resolved_active_intervals = []
            if self.status_service and self.admission_id:
                end_time = start_time + timedelta(hours=24)
                resolved_active_intervals = self.status_service.get_active_intervals(
                    self.admission_id,
                    start_time - timedelta(days=CHART_ACTIVE_INTERVAL_LOOKBACK_DAYS),
                    end_time + timedelta(days=CHART_ACTIVE_INTERVAL_LOOKAHEAD_DAYS),
                )
        else:
            resolved_active_intervals = active_intervals

        render_key = (
            self._normalize_key_dt(start_time),
            self._build_vitals_key(vitals),
            self._build_intervals_key(resolved_active_intervals),
        )
        if render_key == self._last_render_key:
            return
        self._last_render_key = render_key

        # РћР±СЂР°Р±РѕС‚РєР° РґР°РЅРЅС‹С… С‡РµСЂРµР· РїСЂРѕС†РµСЃСЃРѕСЂ СЃ СѓС‡РµС‚РѕРј Р°РєС‚РёРІРЅС‹С… РёРЅС‚РµСЂРІР°Р»РѕРІ (СЂР°Р·СЂС‹РІС‹ Рё С„РёР»СЊС‚СЂР°С†РёСЏ)
        processed = ChartDataProcessor.process_vitals(vitals, start_time, resolved_active_intervals)
        
        if 'densified_data' in processed:
            d = processed['densified_data']
            self.current_vitals = processed['original_vitals']
        else:
            d = processed
            self.current_vitals = vitals

        for key, curve in self._curve_by_key.items():
            curve.setData(d[f'{key}_x'], d[f'{key}_y'])
        if not len(d.get("sys_x", ())) or not len(d.get("dia_x", ())):
            self._clear_fill_paths()

        scatter_data = {
            "ad": {"x": [], "y": []},
            "pulse": {"x": [], "y": []},
            "spo2": {"x": [], "y": []},
            "temp": {"x": [], "y": []},
            "rr": {"x": [], "y": []},
            "cvp": {"x": [], "y": []},
        }
        for v in self.current_vitals:
            ex_x = (v.timestamp - self.start_time).total_seconds() / 3600.0
            def add_spot(val, key, is_cvp=False):
                try:
                    if val is not None:
                        v_float = float(val)
                        if is_cvp and v_float == -1.0:
                            v_float = 0.0
                        scatter_data[key]["x"].append(ex_x)
                        scatter_data[key]["y"].append(v_float)
                except: pass
            
            add_spot(v.sys, "ad")
            add_spot(v.dia, "ad")
            add_spot(v.pulse, "pulse")
            add_spot(v.spo2, "spo2")
            add_spot(v.temp, "temp")
            add_spot(getattr(v, 'rr', None), "rr")
            add_spot(getattr(v, 'cvp', None), "cvp", is_cvp=True)
            
        for key, item in self._scatter_items.items():
            item.setData(x=scatter_data[key]["x"], y=scatter_data[key]["y"])

        self.header_spacer.update()

