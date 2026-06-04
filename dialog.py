# -*- coding: utf-8 -*-
"""OSM Quick 3D dialog: pick an area + layers, then download and style natively.

The dialog only collects inputs and emits ``runRequested(dict)``; the plugin
class does the work. Last choices persist between runs via QgsSettings.
"""
from __future__ import annotations

from qgis.PyQt.QtCore import pyqtSignal
from qgis.PyQt.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QDoubleSpinBox,
    QFormLayout,
    QGridLayout,
    QGroupBox,
    QLabel,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)
from qgis.core import QgsMapLayerProxyModel, QgsSettings
from qgis.gui import QgsMapLayerComboBox

_S = "osm_quick_3d"


def _truthy(value, default):
    if value is None:
        return default
    return str(value).strip().lower() in ("1", "true", "yes", "on")


class PluginDialog(QDialog):
    runRequested = pyqtSignal(dict)

    def __init__(self, iface, parent=None):
        super().__init__(parent)
        self.iface = iface
        self.setWindowTitle("OSM Quick 3D")
        self.resize(560, 560)
        self._build_ui()
        self._restore()

    # ── UI ───────────────────────────────────────────────────────────────
    def _build_ui(self):
        root = QVBoxLayout(self)
        self.tabs = QTabWidget()
        self.tabs.addTab(self._tab_main(), "Alan & katmanlar")
        self.tabs.addTab(self._tab_about(), "Hakkında")
        root.addWidget(self.tabs, 1)

        self.status = QLabel("Hazır")
        self.status.setStyleSheet("color:#555;padding:4px;")
        root.addWidget(self.status)

        bb = QDialogButtonBox()
        self.run_btn = bb.addButton("İndir & 3D yükle", QDialogButtonBox.ButtonRole.AcceptRole)
        bb.addButton(QDialogButtonBox.StandardButton.Close)
        bb.accepted.connect(self._emit_run)
        bb.rejected.connect(self.close)
        root.addWidget(bb)

    def _tab_main(self):
        w = QWidget()
        form = QFormLayout(w)

        self.area_source = QComboBox()
        self.area_source.addItem("Görünür harita alanı (canvas)", "canvas")
        self.area_source.addItem("Seçili objelerin kapsamı", "selection")
        form.addRow("Alan kaynağı:", self.area_source)

        self.max_km2 = QDoubleSpinBox()
        self.max_km2.setRange(0.1, 200.0)
        self.max_km2.setSingleStep(0.5)
        self.max_km2.setSuffix(" km²")
        self.max_km2.setValue(6.0)
        form.addRow("En fazla alan:", self.max_km2)

        note = QLabel(
            "Büyük alanlarda Overpass yavaşlayabilir ya da eleman sınırına takılabilir. "
            "Alan, merkez etrafında bu değere kırpılır."
        )
        note.setWordWrap(True)
        note.setStyleSheet("color:#777;font-size:11px;")
        form.addRow("", note)

        layers = QGroupBox("Katmanlar")
        grid = QGridLayout(layers)
        self.cb_buildings = QCheckBox("Binalar")
        self.cb_extrude = QCheckBox("3D ekstrüzyon (massing)")
        self.cb_roads = QCheckBox("Yollar + bisiklet")
        self.cb_water = QCheckBox("Su")
        self.cb_greens = QCheckBox("Yeşil alanlar")
        self.cb_trees = QCheckBox("Ağaçlar")
        self.cb_furniture = QCheckBox("Sokak mobilyası")
        grid.addWidget(self.cb_buildings, 0, 0)
        grid.addWidget(self.cb_extrude, 0, 1)
        grid.addWidget(self.cb_roads, 1, 0)
        grid.addWidget(self.cb_water, 1, 1)
        grid.addWidget(self.cb_greens, 2, 0)
        grid.addWidget(self.cb_trees, 2, 1)
        grid.addWidget(self.cb_furniture, 3, 0)
        self.cb_buildings.toggled.connect(self.cb_extrude.setEnabled)
        form.addRow(layers)

        self.height_scale = QDoubleSpinBox()
        self.height_scale.setRange(0.5, 5.0)
        self.height_scale.setSingleStep(0.25)
        self.height_scale.setDecimals(2)
        self.height_scale.setSuffix("×")
        self.height_scale.setValue(1.0)
        self.height_scale.setToolTip(
            "3D bina yüksekliklerini abartır (1.0 = gerçek OSM yüksekliği). "
            "Düz şehirlerde 1.5–2.0 massing'i okunur kılar."
        )
        self.cb_extrude.toggled.connect(self.height_scale.setEnabled)
        form.addRow("Yükseklik abartma:", self.height_scale)

        self.basemap = QgsMapLayerComboBox()
        self.basemap.setFilters(QgsMapLayerProxyModel.RasterLayer | QgsMapLayerProxyModel.VectorLayer)
        self.basemap.setAllowEmptyLayer(True)
        self.basemap.setCurrentIndex(0)
        form.addRow("Altlık (basemap):", self.basemap)

        self.cb_save_gpkg = QCheckBox("Sonucu GeoPackage'a kaydet (kalıcı katmanlar)")
        self.cb_save_gpkg.setToolTip(
            "İşaretliyse indirilen katmanlar tek bir .gpkg dosyasına yazılır ve oradan "
            "yüklenir; böylece proje kapanınca kaybolmaz. Çalıştırınca dosya konumu sorulur."
        )
        form.addRow("", self.cb_save_gpkg)

        self.cb_open3d = QCheckBox("Bitince 3D Harita Görünümü'nü aç")
        form.addRow("", self.cb_open3d)
        return w

    def _tab_about(self):
        w = QWidget()
        lay = QVBoxLayout(w)
        lay.addWidget(QLabel(
            "<h3>OSM Quick 3D</h3>"
            "<p>Bir alan seç, OpenStreetMap verisini indir ve QGIS içinde native "
            "olarak aç: işlevsel renkli 2D katmanlar + düz-tepeli 3D bina ekstrüzyonu "
            "(massing) + altlık. Tarayıcı yok; büyük alanlar için.</p>"
            "<p>© OpenStreetMap katkıcıları. "
            "<a href='https://github.com/YusufEminoglu/osm_quick_3d/issues'>GitHub Issues</a></p>"
        ))
        lay.addStretch(1)
        return w

    # ── state ──────────────────────────────────────────────────────────────
    def _emit_run(self):
        p = {
            "area_source": self.area_source.currentData(),
            "max_km2": self.max_km2.value(),
            "want_buildings": self.cb_buildings.isChecked(),
            "extrude_3d": self.cb_extrude.isChecked() and self.cb_buildings.isChecked(),
            "height_scale": self.height_scale.value(),
            "want_roads": self.cb_roads.isChecked(),
            "want_water": self.cb_water.isChecked(),
            "want_greens": self.cb_greens.isChecked(),
            "want_trees": self.cb_trees.isChecked(),
            "want_furniture": self.cb_furniture.isChecked(),
            "basemap": self.basemap.currentLayer(),
            "open_3d": self.cb_open3d.isChecked(),
            "save_gpkg": self.cb_save_gpkg.isChecked(),
        }
        self._save(p)
        self.runRequested.emit(p)

    def _restore(self):
        s = QgsSettings()
        idx = self.area_source.findData(s.value(f"{_S}/area_source", "canvas"))
        if idx >= 0:
            self.area_source.setCurrentIndex(idx)
        try:
            self.max_km2.setValue(float(s.value(f"{_S}/max_km2", 6.0)))
        except (TypeError, ValueError):
            pass
        self.cb_buildings.setChecked(_truthy(s.value(f"{_S}/buildings"), True))
        self.cb_extrude.setChecked(_truthy(s.value(f"{_S}/extrude"), True))
        self.cb_roads.setChecked(_truthy(s.value(f"{_S}/roads"), True))
        self.cb_water.setChecked(_truthy(s.value(f"{_S}/water"), True))
        self.cb_greens.setChecked(_truthy(s.value(f"{_S}/greens"), True))
        self.cb_trees.setChecked(_truthy(s.value(f"{_S}/trees"), False))
        self.cb_furniture.setChecked(_truthy(s.value(f"{_S}/furniture"), False))
        self.cb_open3d.setChecked(_truthy(s.value(f"{_S}/open3d"), True))
        self.cb_save_gpkg.setChecked(_truthy(s.value(f"{_S}/save_gpkg"), False))
        try:
            self.height_scale.setValue(float(s.value(f"{_S}/height_scale", 1.0)))
        except (TypeError, ValueError):
            pass
        self.cb_extrude.setEnabled(self.cb_buildings.isChecked())
        self.height_scale.setEnabled(self.cb_extrude.isChecked())

    def _save(self, p):
        s = QgsSettings()
        s.setValue(f"{_S}/area_source", p["area_source"])
        s.setValue(f"{_S}/max_km2", p["max_km2"])
        s.setValue(f"{_S}/buildings", p["want_buildings"])
        s.setValue(f"{_S}/extrude", self.cb_extrude.isChecked())
        s.setValue(f"{_S}/roads", p["want_roads"])
        s.setValue(f"{_S}/water", p["want_water"])
        s.setValue(f"{_S}/greens", p["want_greens"])
        s.setValue(f"{_S}/trees", p["want_trees"])
        s.setValue(f"{_S}/furniture", p["want_furniture"])
        s.setValue(f"{_S}/open3d", p["open_3d"])
        s.setValue(f"{_S}/height_scale", p["height_scale"])
        s.setValue(f"{_S}/save_gpkg", p["save_gpkg"])

    def set_status(self, text, *, error=False):
        self.status.setStyleSheet(f"color:{'#b71c1c' if error else '#1b5e20'};padding:4px;")
        self.status.setText(text)
