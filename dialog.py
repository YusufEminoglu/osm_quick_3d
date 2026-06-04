# -*- coding: utf-8 -*-
"""OSM Quick 3D dialog: pick a study area + layers, then download and style natively.

The dialog only collects inputs and emits ``runRequested(dict)``; the plugin
class does the work. Last choices persist between runs via QgsSettings.
"""
from __future__ import annotations

from qgis.PyQt.QtCore import pyqtSignal
from qgis.PyQt.QtGui import QIcon
from qgis.PyQt.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QDoubleSpinBox,
    QFormLayout,
    QFrame,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QVBoxLayout,
    QWidget,
)
from qgis.core import QgsMapLayerProxyModel, QgsSettings
from qgis.gui import QgsMapLayerComboBox

import os

from .osm_download import (
    SHAPE_CIRCLE,
    SHAPE_HEXAGON,
    SHAPE_RECTANGLE,
    SHAPE_ROUNDED,
)

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
        icon_path = os.path.join(os.path.dirname(__file__), "icons", "icon.png")
        if os.path.exists(icon_path):
            self.setWindowIcon(QIcon(icon_path))
        self.resize(540, 640)
        self._build_ui()
        self._restore()

    # ── UI ───────────────────────────────────────────────────────────────
    def _build_ui(self):
        root = QVBoxLayout(self)
        root.setSpacing(10)

        root.addWidget(self._header())
        root.addWidget(self._group_area())
        root.addWidget(self._group_layers())
        root.addWidget(self._group_3d())
        root.addWidget(self._group_output())
        root.addStretch(1)

        self.status = QLabel("Ready")
        self.status.setWordWrap(True)
        self.status.setStyleSheet("color:#555;padding:6px;background:#f4f5f4;border-radius:4px;")
        root.addWidget(self.status)

        bb = QDialogButtonBox()
        self.run_btn = bb.addButton("Download & build 3D", QDialogButtonBox.ButtonRole.AcceptRole)
        self.run_btn.setStyleSheet("font-weight:600;padding:6px 14px;")
        bb.addButton(QDialogButtonBox.StandardButton.Close)
        bb.accepted.connect(self._emit_run)
        bb.rejected.connect(self.close)
        root.addWidget(bb)

    def _header(self):
        box = QFrame()
        box.setStyleSheet("background:#37494c;border-radius:6px;")
        lay = QHBoxLayout(box)
        lay.setContentsMargins(12, 10, 12, 10)
        icon_path = os.path.join(os.path.dirname(__file__), "icons", "icon.png")
        if os.path.exists(icon_path):
            pix = QIcon(icon_path).pixmap(40, 40)
            ic = QLabel()
            ic.setPixmap(pix)
            lay.addWidget(ic)
        text = QLabel(
            "<b style='color:#eef2f1;font-size:14px;'>OSM Quick 3D</b><br>"
            "<span style='color:#aebcbb;font-size:11px;'>"
            "OpenStreetMap → native QGIS 2D + 3D massing, no browser</span>"
        )
        lay.addWidget(text, 1)
        return box

    def _group_area(self):
        box = QGroupBox("Study area")
        form = QFormLayout(box)

        self.area_source = QComboBox()
        self.area_source.addItem("Visible map extent (canvas)", "canvas")
        self.area_source.addItem("Extent of selected features", "selection")
        form.addRow("Source:", self.area_source)

        self.shape = QComboBox()
        self.shape.addItem("Rectangle (map extent)", SHAPE_RECTANGLE)
        self.shape.addItem("Rounded rectangle", SHAPE_ROUNDED)
        self.shape.addItem("Circle", SHAPE_CIRCLE)
        self.shape.addItem("Hexagon", SHAPE_HEXAGON)
        self.shape.setToolTip(
            "The study-area shape that OSM features are clipped/exported to. "
            "Circle and hexagon are inscribed in the shorter side of the extent."
        )
        form.addRow("Shape:", self.shape)

        self.max_km2 = QDoubleSpinBox()
        self.max_km2.setRange(0.1, 200.0)
        self.max_km2.setSingleStep(0.5)
        self.max_km2.setSuffix(" km²")
        self.max_km2.setValue(6.0)
        form.addRow("Max area:", self.max_km2)

        note = QLabel(
            "Large areas can be slow on Overpass or hit element limits. "
            "The area is clamped to this value about its centre."
        )
        note.setWordWrap(True)
        note.setStyleSheet("color:#777;font-size:11px;")
        form.addRow("", note)
        return box

    def _group_layers(self):
        box = QGroupBox("Layers")
        grid = QGridLayout(box)
        self.cb_buildings = QCheckBox("Buildings")
        self.cb_roads = QCheckBox("Roads + cycleways")
        self.cb_water = QCheckBox("Water")
        self.cb_greens = QCheckBox("Green areas")
        self.cb_trees = QCheckBox("Trees")
        self.cb_furniture = QCheckBox("Street furniture")
        grid.addWidget(self.cb_buildings, 0, 0)
        grid.addWidget(self.cb_roads, 0, 1)
        grid.addWidget(self.cb_water, 1, 0)
        grid.addWidget(self.cb_greens, 1, 1)
        grid.addWidget(self.cb_trees, 2, 0)
        grid.addWidget(self.cb_furniture, 2, 1)
        return box

    def _group_3d(self):
        box = QGroupBox("3D & base")
        form = QFormLayout(box)

        self.cb_extrude = QCheckBox("3D extrusion (massing)")
        self.cb_extrude.setToolTip("Extrude buildings into a flat-roof massing model.")
        form.addRow("", self.cb_extrude)

        self.height_scale = QDoubleSpinBox()
        self.height_scale.setRange(0.5, 5.0)
        self.height_scale.setSingleStep(0.25)
        self.height_scale.setDecimals(2)
        self.height_scale.setSuffix("×")
        self.height_scale.setValue(1.0)
        self.height_scale.setToolTip(
            "Exaggerate 3D building heights (1.0 = true OSM height). "
            "1.5–2.0 helps low-rise districts read."
        )
        form.addRow("Height exaggeration:", self.height_scale)

        self.cb_base = QCheckBox("Add ground base (recessed −5 m slab, +5 m buffer)")
        self.cb_base.setToolTip(
            "A ground slab the city stands on: the study area buffered outward by "
            "5 m, extruded as a plinth from −5 m up to ground level."
        )
        form.addRow("", self.cb_base)

        self.cb_open3d = QCheckBox("Open a 3D Map View when done")
        form.addRow("", self.cb_open3d)

        # 3D-dependent controls follow the extrusion toggle.
        self.cb_buildings.toggled.connect(self.cb_extrude.setEnabled)
        self.cb_extrude.toggled.connect(self.height_scale.setEnabled)
        return box

    def _group_output(self):
        box = QGroupBox("Output & data")
        form = QFormLayout(box)

        self.basemap = QgsMapLayerComboBox()
        self.basemap.setFilters(QgsMapLayerProxyModel.RasterLayer | QgsMapLayerProxyModel.VectorLayer)
        self.basemap.setAllowEmptyLayer(True)
        self.basemap.setCurrentIndex(0)
        form.addRow("Basemap underlay:", self.basemap)

        self.cb_save_gpkg = QCheckBox("Save result to a GeoPackage (persistent layers)")
        self.cb_save_gpkg.setToolTip(
            "Write the downloaded layers into one .gpkg and load them from it, so they "
            "survive a project reload. You are asked for the file location on run."
        )
        form.addRow("", self.cb_save_gpkg)

        self.cb_use_cache = QCheckBox("Use cache when possible (spare Overpass)")
        self.cb_use_cache.setToolTip(
            "Re-runs on the same area reuse a one-week disk cache, so they open instantly "
            "without hitting the rate-limited Overpass API again."
        )
        form.addRow("", self.cb_use_cache)
        return box

    # ── state ──────────────────────────────────────────────────────────────
    def _emit_run(self):
        p = {
            "area_source": self.area_source.currentData(),
            "shape": self.shape.currentData(),
            "max_km2": self.max_km2.value(),
            "want_buildings": self.cb_buildings.isChecked(),
            "extrude_3d": self.cb_extrude.isChecked() and self.cb_buildings.isChecked(),
            "height_scale": self.height_scale.value(),
            "want_base": self.cb_base.isChecked(),
            "want_roads": self.cb_roads.isChecked(),
            "want_water": self.cb_water.isChecked(),
            "want_greens": self.cb_greens.isChecked(),
            "want_trees": self.cb_trees.isChecked(),
            "want_furniture": self.cb_furniture.isChecked(),
            "basemap": self.basemap.currentLayer(),
            "open_3d": self.cb_open3d.isChecked(),
            "save_gpkg": self.cb_save_gpkg.isChecked(),
            "use_cache": self.cb_use_cache.isChecked(),
        }
        self._save(p)
        self.runRequested.emit(p)

    def _restore(self):
        s = QgsSettings()
        idx = self.area_source.findData(s.value(f"{_S}/area_source", "canvas"))
        if idx >= 0:
            self.area_source.setCurrentIndex(idx)
        sidx = self.shape.findData(s.value(f"{_S}/shape", SHAPE_RECTANGLE))
        if sidx >= 0:
            self.shape.setCurrentIndex(sidx)
        try:
            self.max_km2.setValue(float(s.value(f"{_S}/max_km2", 6.0)))
        except (TypeError, ValueError):
            pass
        self.cb_buildings.setChecked(_truthy(s.value(f"{_S}/buildings"), True))
        self.cb_extrude.setChecked(_truthy(s.value(f"{_S}/extrude"), True))
        self.cb_base.setChecked(_truthy(s.value(f"{_S}/base"), True))
        self.cb_roads.setChecked(_truthy(s.value(f"{_S}/roads"), True))
        self.cb_water.setChecked(_truthy(s.value(f"{_S}/water"), True))
        self.cb_greens.setChecked(_truthy(s.value(f"{_S}/greens"), True))
        self.cb_trees.setChecked(_truthy(s.value(f"{_S}/trees"), False))
        self.cb_furniture.setChecked(_truthy(s.value(f"{_S}/furniture"), False))
        self.cb_open3d.setChecked(_truthy(s.value(f"{_S}/open3d"), True))
        self.cb_save_gpkg.setChecked(_truthy(s.value(f"{_S}/save_gpkg"), False))
        self.cb_use_cache.setChecked(_truthy(s.value(f"{_S}/use_cache"), True))
        try:
            self.height_scale.setValue(float(s.value(f"{_S}/height_scale", 1.0)))
        except (TypeError, ValueError):
            pass
        self.cb_extrude.setEnabled(self.cb_buildings.isChecked())
        self.height_scale.setEnabled(self.cb_extrude.isChecked())

    def _save(self, p):
        s = QgsSettings()
        s.setValue(f"{_S}/area_source", p["area_source"])
        s.setValue(f"{_S}/shape", p["shape"])
        s.setValue(f"{_S}/max_km2", p["max_km2"])
        s.setValue(f"{_S}/buildings", p["want_buildings"])
        s.setValue(f"{_S}/extrude", self.cb_extrude.isChecked())
        s.setValue(f"{_S}/base", p["want_base"])
        s.setValue(f"{_S}/roads", p["want_roads"])
        s.setValue(f"{_S}/water", p["want_water"])
        s.setValue(f"{_S}/greens", p["want_greens"])
        s.setValue(f"{_S}/trees", p["want_trees"])
        s.setValue(f"{_S}/furniture", p["want_furniture"])
        s.setValue(f"{_S}/open3d", p["open_3d"])
        s.setValue(f"{_S}/height_scale", p["height_scale"])
        s.setValue(f"{_S}/save_gpkg", p["save_gpkg"])
        s.setValue(f"{_S}/use_cache", p["use_cache"])

    def set_status(self, text, *, error=False):
        self.status.setStyleSheet(
            f"color:{'#b71c1c' if error else '#1b5e20'};padding:6px;"
            "background:#f4f5f4;border-radius:4px;"
        )
        self.status.setText(text)
