# -*- coding: utf-8 -*-
"""Primary docked control panel for OSM Quick 3D."""
from __future__ import annotations

import os

from qgis.PyQt.QtCore import QTimer, Qt, pyqtSignal
from qgis.PyQt.QtGui import QColor, QIcon
from qgis.PyQt.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDockWidget,
    QDoubleSpinBox,
    QFormLayout,
    QFrame,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QSlider,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)
from qgis.core import QgsLayerTreeGroup, QgsLayerTreeLayer, QgsMapLayerProxyModel, QgsProject, QgsSettings
from qgis.gui import QgsColorButton, QgsMapLayerComboBox

from . import native3d, styling
from .osm_download import (
    BASE_DEPTH_M,
    SHAPE_CIRCLE,
    SHAPE_HEXAGON,
    SHAPE_RECTANGLE,
    SHAPE_ROUNDED,
    clear_cache,
)

_S = "osm_quick_3d"


def _truthy(value, default):
    if value is None:
        return default
    return str(value).strip().lower() in ("1", "true", "yes", "on")


def _qt_value(owner, enum_name, value_name, fallback_name=None):
    try:
        enum_owner = getattr(owner, enum_name)
        return getattr(enum_owner, value_name)
    except AttributeError:
        return getattr(owner, fallback_name or value_name)


def _scroll_policy(name):
    return _qt_value(Qt, "ScrollBarPolicy", name)


def _size_policy(name):
    return _qt_value(QSizePolicy, "Policy", name)


class PluginDockWidget(QDockWidget):
    """Docked plugin surface with build, live styling, and embedded 3D view tabs."""

    runRequested = pyqtSignal(dict)

    _QSS = """
    QDockWidget {
        background-color: #eef1f2;
    }
    QWidget#DockRoot {
        background-color: #eef1f2;
    }
    QScrollArea {
        background-color: transparent;
        border: none;
    }
    QFrame#Header {
        background: #26383b;
        border-radius: 0;
    }
    QLabel#HeaderTitle {
        color: #f4f8f7;
        font-size: 15px;
        font-weight: 700;
    }
    QLabel#HeaderSubtitle {
        color: #b7c7c5;
        font-size: 11px;
    }
    QLabel#StatusLabel {
        color: #355052;
        background: #ffffff;
        border: 1px solid #dce4e5;
        border-radius: 7px;
        padding: 5px 8px;
        font-size: 11px;
    }
    QGroupBox {
        font-weight: 700;
        font-size: 12px;
        color: #21383b;
        border: 1px solid #dce4e5;
        border-left: 4px solid #3f8079;
        background-color: #ffffff;
        margin-top: 11px;
        padding: 10px 7px 7px 7px;
        border-radius: 6px;
    }
    QGroupBox::title {
        subcontrol-origin: margin;
        subcontrol-position: top left;
        left: 10px;
        top: 1px;
        padding: 1px 7px;
        color: #1f625d;
        background: #ffffff;
    }
    QLabel {
        color: #263b3e;
        font-size: 11px;
        font-weight: 500;
        background: transparent;
    }
    QCheckBox {
        spacing: 8px;
        padding: 3px 0;
        color: #23383b;
        background: transparent;
        font-size: 11px;
    }
    QCheckBox:checked {
        color: #0f4f4a;
        font-weight: 700;
    }
    QCheckBox:disabled {
        color: #6d7f82;
    }
    QComboBox, QDoubleSpinBox {
        border: 1px solid #c6d0d2;
        border-radius: 6px;
        padding: 4px 8px;
        background: #ffffff;
        min-height: 20px;
        font-size: 11px;
        color: #2d3d40;
        selection-background-color: #cfe4e1;
        selection-color: #21302f;
    }
    QComboBox:disabled, QDoubleSpinBox:disabled {
        background: #edf1f2;
        color: #52676a;
        border-color: #d5dddf;
    }
    QComboBox:hover, QDoubleSpinBox:hover {
        border-color: #9bb5b7;
    }
    QComboBox:focus, QDoubleSpinBox:focus {
        border-color: #3f8079;
        background: #ffffff;
    }
    QPushButton {
        border-radius: 6px;
        padding: 5px 10px;
        background-color: #ffffff;
        border: 1px solid #c6d0d2;
        color: #405154;
        font-size: 11px;
        font-weight: 700;
    }
    QPushButton:hover {
        background-color: #edf3f3;
        color: #243436;
        border-color: #9bb5b7;
    }
    QPushButton:pressed {
        background-color: #d6e1e2;
    }
    QPushButton#PrimaryRun {
        background-color: #2f756f;
        border: 1px solid #2f756f;
        color: #ffffff;
        padding: 7px 12px;
    }
    QPushButton#PrimaryRun:hover {
        background-color: #28645f;
        border-color: #28645f;
    }
    QTabWidget::pane {
        border: none;
        background: transparent;
    }
    QTabBar::tab {
        background: #dfe7e8;
        color: #607174;
        border: 1px solid #c6d0d2;
        border-bottom-color: transparent;
        border-top-left-radius: 6px;
        border-top-right-radius: 6px;
        min-width: 50px;
        padding: 6px 7px;
        font-size: 11px;
        font-weight: 700;
        margin-right: 1px;
    }
    QTabBar::tab:selected {
        background: #ffffff;
        color: #2f756f;
        border-color: #c6d0d2;
        border-bottom: 2px solid #2f756f;
    }
    QTabBar::tab:hover {
        background: #edf3f3;
    }
    QSlider::groove:horizontal {
        height: 6px;
        background: #dfe7e8;
        border-radius: 3px;
    }
    QSlider::handle:horizontal {
        background: #3f8079;
        width: 14px;
        height: 14px;
        margin-top: -4px;
        margin-bottom: -4px;
        border-radius: 7px;
    }
    QSlider::handle:horizontal:hover {
        background: #2d615b;
    }
    """

    def __init__(self, iface, parent=None):
        super().__init__("OSM Quick 3D", parent)
        self.iface = iface
        self.setObjectName("OSMQuick3DController")
        try:
            dock_areas = Qt.DockWidgetArea.LeftDockWidgetArea | Qt.DockWidgetArea.RightDockWidgetArea
        except AttributeError:
            dock_areas = getattr(Qt, "LeftDockWidgetArea") | getattr(Qt, "RightDockWidgetArea")
        self.setAllowedAreas(dock_areas)
        try:
            features = (
                QDockWidget.DockWidgetFeature.DockWidgetClosable
                | QDockWidget.DockWidgetFeature.DockWidgetMovable
                | QDockWidget.DockWidgetFeature.DockWidgetFloatable
            )
        except AttributeError:
            features = (
                QDockWidget.DockWidgetClosable
                | QDockWidget.DockWidgetMovable
                | QDockWidget.DockWidgetFloatable
            )
        self.setFeatures(features)
        self.setMinimumWidth(300)
        self.resize(430, 720)

        self._embedded_canvas = None
        self._embedded_container = None
        self._embedded_original_parent = None
        self._embedded_original_layout = None
        self._embedded_original_dock = None
        self._embedded_canvas_name = native3d.EMBEDDED_3D_CANVAS_NAME

        self._build_ui()
        self._restore()
        self.refresh_groups()

        self._embed_timer = QTimer(self)
        self._embed_timer.setInterval(1500)
        self._embed_timer.timeout.connect(lambda: self._refresh_3d_view(auto=True))
        self._embed_timer.start()

    def _build_ui(self):
        self.setStyleSheet(self._QSS)

        root_widget = QWidget()
        root_widget.setObjectName("DockRoot")
        root = QVBoxLayout(root_widget)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        root.addWidget(self._header())

        self.tab_widget = QTabWidget()
        self.tab_widget.setUsesScrollButtons(True)
        self.tab_widget.setDocumentMode(True)
        try:
            self.tab_widget.setElideMode(Qt.TextElideMode.ElideRight)
        except AttributeError:
            self.tab_widget.setElideMode(getattr(Qt, "ElideRight"))
        self.download_tab = self._download_tab()
        self.live_tab = self._live_tab()
        self.view_tab = self._view_tab()
        self.tab_widget.addTab(self.download_tab, "Build")
        self.tab_widget.addTab(self.live_tab, "Style")
        self.tab_widget.addTab(self.view_tab, "3D")
        self.tab_widget.setTabToolTip(0, "Download & Build")
        self.tab_widget.setTabToolTip(1, "Live Styling")
        self.tab_widget.setTabToolTip(2, "Embedded 3D Map View")
        root.addWidget(self.tab_widget, 1)

        self.setWidget(root_widget)

    def _header(self):
        header = QFrame()
        header.setObjectName("Header")
        lay = QHBoxLayout(header)
        lay.setContentsMargins(10, 7, 10, 7)
        lay.setSpacing(10)

        icon_path = os.path.join(os.path.dirname(__file__), "icons", "icon.png")
        if os.path.exists(icon_path):
            icon = QLabel()
            icon.setPixmap(QIcon(icon_path).pixmap(34, 34))
            lay.addWidget(icon)

        text_col = QVBoxLayout()
        text_col.setContentsMargins(0, 0, 0, 0)
        text_col.setSpacing(1)
        title = QLabel("OSM Quick 3D")
        title.setObjectName("HeaderTitle")
        subtitle = QLabel("Native QGIS 2D + 3D")
        subtitle.setObjectName("HeaderSubtitle")
        text_col.addWidget(title)
        text_col.addWidget(subtitle)
        lay.addLayout(text_col, 1)
        return header

    def _scroll_tab(self):
        tab = QWidget()
        outer = QVBoxLayout(tab)
        outer.setContentsMargins(6, 6, 6, 6)
        outer.setSpacing(5)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setVerticalScrollBarPolicy(_scroll_policy("ScrollBarAsNeeded"))
        scroll.setHorizontalScrollBarPolicy(_scroll_policy("ScrollBarAlwaysOff"))
        content = QWidget()
        content.setStyleSheet("background: transparent;")
        content.setSizePolicy(_size_policy("Preferred"), _size_policy("MinimumExpanding"))
        layout = QVBoxLayout(content)
        layout.setContentsMargins(0, 0, 4, 0)
        layout.setSpacing(5)
        scroll.setWidget(content)
        outer.addWidget(scroll, 1)
        return tab, layout

    def _configure_form(self, form):
        try:
            form.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.AllNonFixedFieldsGrow)
        except AttributeError:
            form.setFieldGrowthPolicy(getattr(QFormLayout, "AllNonFixedFieldsGrow"))
        try:
            form.setRowWrapPolicy(QFormLayout.RowWrapPolicy.WrapLongRows)
        except AttributeError:
            form.setRowWrapPolicy(getattr(QFormLayout, "WrapLongRows"))
        form.setContentsMargins(4, 2, 4, 4)
        form.setHorizontalSpacing(8)
        form.setVerticalSpacing(4)
        return form

    def _download_tab(self):
        tab, layout = self._scroll_tab()
        layout.addWidget(self._download_area_group())
        layout.addWidget(self._download_layers_group())
        layout.addWidget(self._download_3d_group())
        layout.addWidget(self._download_output_group())

        self.status = QLabel("Ready")
        self.status.setObjectName("StatusLabel")
        self.status.setWordWrap(True)
        layout.addWidget(self.status)

        self.run_btn = QPushButton("Download & build 3D")
        self.run_btn.setObjectName("PrimaryRun")
        self.run_btn.clicked.connect(self._emit_run)
        layout.addWidget(self.run_btn)
        return tab

    def _download_area_group(self):
        box = QGroupBox("Area")
        form = self._configure_form(QFormLayout(box))

        self.area_source = QComboBox()
        self.area_source.addItem("Visible map extent (canvas)", "canvas")
        self.area_source.addItem("Extent of selected features", "selection")
        form.addRow("Source:", self.area_source)

        self.shape = QComboBox()
        self.shape.addItem("Rectangle", SHAPE_RECTANGLE)
        self.shape.addItem("Rounded rectangle", SHAPE_ROUNDED)
        self.shape.addItem("Circle", SHAPE_CIRCLE)
        self.shape.addItem("Hexagon", SHAPE_HEXAGON)
        form.addRow("Shape:", self.shape)

        self.max_km2 = QDoubleSpinBox()
        self.max_km2.setRange(0.1, 200.0)
        self.max_km2.setSingleStep(0.5)
        self.max_km2.setSuffix(" km2")
        self.max_km2.setValue(6.0)
        form.addRow("Max area:", self.max_km2)
        return box

    def _download_layers_group(self):
        box = QGroupBox("Layers")
        grid = QGridLayout(box)
        grid.setContentsMargins(6, 4, 6, 4)
        grid.setHorizontalSpacing(12)
        grid.setVerticalSpacing(2)
        self.cb_buildings = QCheckBox("Buildings")
        self.cb_roads = QCheckBox("Roads + cycleways")
        self.cb_water = QCheckBox("Water")
        self.cb_greens = QCheckBox("Green areas")
        self.cb_trees = QCheckBox("Trees")
        self.cb_furniture = QCheckBox("Street furniture")
        self.cb_labels = QCheckBox("Show name labels")
        grid.addWidget(self.cb_buildings, 0, 0)
        grid.addWidget(self.cb_roads, 0, 1)
        grid.addWidget(self.cb_water, 1, 0)
        grid.addWidget(self.cb_greens, 1, 1)
        grid.addWidget(self.cb_trees, 2, 0)
        grid.addWidget(self.cb_furniture, 2, 1)
        grid.addWidget(self.cb_labels, 3, 0, 1, 2)
        return box

    def _download_3d_group(self):
        box = QGroupBox("3D Color")
        form = self._configure_form(QFormLayout(box))

        self.cb_extrude = QCheckBox("3D extrusion")
        form.addRow("", self.cb_extrude)

        self.height_scale = QDoubleSpinBox()
        self.height_scale.setRange(0.5, 5.0)
        self.height_scale.setSingleStep(0.25)
        self.height_scale.setDecimals(2)
        self.height_scale.setSuffix("x")
        self.height_scale.setValue(1.0)
        form.addRow("Height exaggeration:", self.height_scale)

        self.theme_combo = QComboBox()
        for key, val in styling.THEMES.items():
            self.theme_combo.addItem(val["label"], key)
        self.theme_combo.currentIndexChanged.connect(self._update_download_color_preview)
        form.addRow("Map theme:", self.theme_combo)

        self.building_color = QComboBox()
        for value, label in styling.BUILDING_COLOR_MODES:
            self.building_color.addItem(label, value)
        self.building_color.currentIndexChanged.connect(self._update_download_classification_enabled)
        self.building_color.currentIndexChanged.connect(self._update_download_color_preview)
        form.addRow("Building colors:", self.building_color)

        self.classification = QComboBox()
        self.classification.addItem("Continuous", "continuous")
        self.classification.addItem("Discrete intervals", "discrete")
        self.classification.addItem("Quantile", "quantile")
        form.addRow("Height classes:", self.classification)

        self.download_color_preview = QFrame()
        self.download_color_preview.setFixedHeight(14)
        form.addRow("", self.download_color_preview)

        self.cb_base = QCheckBox("Add recessed ground base")
        form.addRow("", self.cb_base)

        self.cb_open3d = QCheckBox("Open and embed 3D Map View")
        form.addRow("", self.cb_open3d)

        self.map_resolution = QComboBox()
        self.map_resolution.addItem("Low (256 px)", 256)
        self.map_resolution.addItem("Medium (512 px)", 512)
        self.map_resolution.addItem("High (1024 px)", 1024)
        self.map_resolution.addItem("Ultra (2048 px)", 2048)
        self.map_resolution.addItem("Insane (4096 px)", 4096)
        form.addRow("Map resolution:", self.map_resolution)

        self.cb_buildings.toggled.connect(self.cb_extrude.setEnabled)
        self.cb_extrude.toggled.connect(self.height_scale.setEnabled)
        return box

    def _download_output_group(self):
        box = QGroupBox("Output")
        form = self._configure_form(QFormLayout(box))

        self.basemap = QgsMapLayerComboBox()
        self.basemap.setFilters(QgsMapLayerProxyModel.RasterLayer | QgsMapLayerProxyModel.VectorLayer)
        self.basemap.setAllowEmptyLayer(True)
        self.basemap.setCurrentIndex(0)
        form.addRow("Basemap underlay:", self.basemap)

        self.cb_save_gpkg = QCheckBox("Save result to GeoPackage")
        form.addRow("", self.cb_save_gpkg)

        self.cb_use_cache = QCheckBox("Use OSM cache")
        form.addRow("", self.cb_use_cache)

        row = QHBoxLayout()
        self.clear_cache_btn = QPushButton("Clear cache")
        self.clear_cache_btn.clicked.connect(self._on_clear_cache)
        row.addWidget(self.clear_cache_btn)
        row.addStretch(1)
        form.addRow("", row)
        return box

    def _live_tab(self):
        tab, layout = self._scroll_tab()

        scene_box = QGroupBox("Scene")
        scene_form = self._configure_form(QFormLayout(scene_box))

        target_row = QHBoxLayout()
        self.group_combo = QComboBox()
        self.group_combo.currentIndexChanged.connect(self._on_group_selected)
        self.refresh_btn = QPushButton("Refresh")
        self.refresh_btn.clicked.connect(self.refresh_groups)
        target_row.addWidget(self.group_combo, 1)
        target_row.addWidget(self.refresh_btn)
        scene_form.addRow("Group:", target_row)

        self.live_theme_combo = QComboBox()
        for key, val in styling.THEMES.items():
            self.live_theme_combo.addItem(val["label"], key)
        self.live_theme_combo.currentIndexChanged.connect(self._on_live_theme_changed)
        scene_form.addRow("Theme:", self.live_theme_combo)

        self.scene_bg_color = QgsColorButton()
        self.scene_bg_color.setColor(QColor("#ffffff"))
        self.scene_bg_color.colorChanged.connect(self._apply_scene_background)
        scene_form.addRow("Background:", self.scene_bg_color)
        layout.addWidget(scene_box)

        building_box = QGroupBox("Building Visuals")
        form = self._configure_form(QFormLayout(building_box))

        self.live_height_scale = QDoubleSpinBox()
        self.live_height_scale.setRange(0.5, 5.0)
        self.live_height_scale.setSingleStep(0.25)
        self.live_height_scale.setDecimals(2)
        self.live_height_scale.setValue(1.0)
        self.live_height_scale.setSuffix("x")
        self.live_height_scale.valueChanged.connect(self._apply_changes)
        form.addRow("Height exaggeration:", self.live_height_scale)

        self.live_building_color = QComboBox()
        for value, label in styling.BUILDING_COLOR_MODES:
            self.live_building_color.addItem(label, value)
        self.live_building_color.currentIndexChanged.connect(self._update_live_color_preview)
        self.live_building_color.currentIndexChanged.connect(self._update_live_classification_enabled)
        self.live_building_color.currentIndexChanged.connect(self._apply_changes)
        form.addRow("Building colors:", self.live_building_color)

        self.live_classification = QComboBox()
        self.live_classification.addItem("Continuous", "continuous")
        self.live_classification.addItem("Discrete intervals", "discrete")
        self.live_classification.addItem("Quantile", "quantile")
        self.live_classification.currentIndexChanged.connect(self._apply_changes)
        form.addRow("Height classes:", self.live_classification)

        self.live_color_preview = QFrame()
        self.live_color_preview.setFixedHeight(12)
        form.addRow("", self.live_color_preview)

        self.building_opacity = QSlider(Qt.Orientation.Horizontal)
        self.building_opacity.setRange(0, 100)
        self.building_opacity.setValue(100)
        self.building_opacity.valueChanged.connect(self._apply_advanced_changes)
        form.addRow("Opacity:", self.building_opacity)

        self.live_cb_labels = QCheckBox("Show name labels")
        self.live_cb_labels.toggled.connect(self._apply_changes)
        form.addRow("", self.live_cb_labels)

        self.live_cb_base = QCheckBox("Extrude ground base")
        self.live_cb_base.toggled.connect(self._apply_changes)
        form.addRow("", self.live_cb_base)

        self.live_map_resolution = QComboBox()
        self.live_map_resolution.addItem("Low (256 px)", 256)
        self.live_map_resolution.addItem("Medium (512 px)", 512)
        self.live_map_resolution.addItem("High (1024 px)", 1024)
        self.live_map_resolution.addItem("Ultra (2048 px)", 2048)
        self.live_map_resolution.addItem("Insane (4096 px)", 4096)
        self.live_map_resolution.currentIndexChanged.connect(self._apply_resolution)
        form.addRow("3D resolution:", self.live_map_resolution)
        layout.addWidget(building_box)

        surface_box = QGroupBox("Surface Palette")
        surface_form = self._configure_form(QFormLayout(surface_box))

        self.major_roads_color = QgsColorButton()
        self.major_roads_color.setColor(QColor("#e1846f"))
        self.major_roads_color.colorChanged.connect(self._apply_advanced_changes)
        surface_form.addRow("Major roads:", self.major_roads_color)

        self.minor_roads_color = QgsColorButton()
        self.minor_roads_color.setColor(QColor("#eae5da"))
        self.minor_roads_color.colorChanged.connect(self._apply_advanced_changes)
        surface_form.addRow("Minor roads:", self.minor_roads_color)

        self.greens_color = QgsColorButton()
        self.greens_color.setColor(QColor("#a9c08a"))
        self.greens_color.colorChanged.connect(self._apply_advanced_changes)
        surface_form.addRow("Green areas:", self.greens_color)

        self.water_color = QgsColorButton()
        self.water_color.setColor(QColor("#a5c9eb"))
        self.water_color.colorChanged.connect(self._apply_advanced_changes)
        surface_form.addRow("Water:", self.water_color)
        layout.addWidget(surface_box)

        detail_box = QGroupBox("Detail Layers")
        detail_form = self._configure_form(QFormLayout(detail_box))

        self.trees_color = QgsColorButton()
        self.trees_color.setColor(QColor("#6f9e5c"))
        self.trees_color.colorChanged.connect(self._apply_advanced_changes)
        detail_form.addRow("Tree canopy:", self.trees_color)

        self.trees_size = QDoubleSpinBox()
        self.trees_size.setRange(0.5, 10.0)
        self.trees_size.setSingleStep(0.25)
        self.trees_size.setValue(1.8)
        self.trees_size.valueChanged.connect(self._apply_advanced_changes)
        detail_form.addRow("Tree size:", self.trees_size)
        layout.addWidget(detail_box)
        return tab

    def _view_tab(self):
        tab = QWidget()
        layout = QVBoxLayout(tab)
        layout.setContentsMargins(5, 5, 5, 5)
        layout.setSpacing(5)

        controls = QHBoxLayout()
        controls.setContentsMargins(0, 0, 0, 0)
        controls.setSpacing(5)
        self.open_3d_btn = QPushButton("Open")
        self.open_3d_btn.setToolTip("Create the embedded QGIS 3D scene in this panel.")
        self.open_3d_btn.clicked.connect(self._on_open_3d_clicked)
        self.restore_3d_btn = QPushButton("Close")
        self.restore_3d_btn.setToolTip("Close the embedded 3D scene.")
        self.restore_3d_btn.clicked.connect(self.cleanup_embedded_3d)
        controls.addWidget(self.open_3d_btn)
        controls.addWidget(self.restore_3d_btn)
        layout.addLayout(controls)

        self.view_host = QFrame()
        self.view_host.setFrameShape(QFrame.Shape.StyledPanel)
        self.view_host.setStyleSheet(
            "QFrame{background:#101518;border:1px solid #26383b;border-radius:6px;}"
        )
        self.view_layout = QVBoxLayout(self.view_host)
        self.view_layout.setContentsMargins(0, 0, 0, 0)
        self.view_layout.setSpacing(0)

        self.view_placeholder = QLabel("Open the embedded 3D scene here.")
        try:
            self.view_placeholder.setAlignment(Qt.AlignmentFlag.AlignCenter)
        except AttributeError:
            self.view_placeholder.setAlignment(getattr(Qt, "AlignCenter"))
        self.view_placeholder.setWordWrap(True)
        self.view_placeholder.setStyleSheet("color:#b7c7c5;font-size:12px;padding:16px;")
        self.view_layout.addWidget(self.view_placeholder)
        layout.addWidget(self.view_host, 1)
        return tab

    def _on_clear_cache(self):
        removed, freed = clear_cache()
        if removed:
            self.set_status(f"Cleared {removed} cached responses ({freed / 1024:.0f} KB).")
        else:
            self.set_status("Cache already empty.")

    def _emit_run(self):
        p = {
            "area_source": self.area_source.currentData(),
            "shape": self.shape.currentData(),
            "max_km2": self.max_km2.value(),
            "want_buildings": self.cb_buildings.isChecked(),
            "extrude_3d": self.cb_extrude.isChecked() and self.cb_buildings.isChecked(),
            "height_scale": self.height_scale.value(),
            "theme": self.theme_combo.currentData(),
            "building_color": self.building_color.currentData(),
            "classification": self.classification.currentData(),
            "want_base": self.cb_base.isChecked(),
            "want_roads": self.cb_roads.isChecked(),
            "want_water": self.cb_water.isChecked(),
            "want_greens": self.cb_greens.isChecked(),
            "want_trees": self.cb_trees.isChecked(),
            "want_furniture": self.cb_furniture.isChecked(),
            "want_labels": self.cb_labels.isChecked(),
            "basemap": self.basemap.currentLayer(),
            "open_3d": self.cb_open3d.isChecked(),
            "map_resolution": self.map_resolution.currentData(),
            "save_gpkg": self.cb_save_gpkg.isChecked(),
            "use_cache": self.cb_use_cache.isChecked(),
        }
        self._save(p)
        self.set_status("Running...")
        self.runRequested.emit(p)

    def _restore(self):
        s = QgsSettings()
        idx = self.area_source.findData(s.value(f"{_S}/area_source", "canvas"))
        if idx >= 0:
            self.area_source.setCurrentIndex(idx)
        sidx = self.shape.findData(s.value(f"{_S}/shape", SHAPE_RECTANGLE))
        if sidx >= 0:
            self.shape.setCurrentIndex(sidx)
        cidx = self.building_color.findData(s.value(f"{_S}/building_color", styling.BUILDING_COLOR_FUNCTION))
        if cidx >= 0:
            self.building_color.setCurrentIndex(cidx)
        tidx = self.theme_combo.findData(s.value(f"{_S}/theme", "default"))
        if tidx >= 0:
            self.theme_combo.setCurrentIndex(tidx)
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
        self.cb_labels.setChecked(_truthy(s.value(f"{_S}/labels"), False))
        self.cb_open3d.setChecked(_truthy(s.value(f"{_S}/open3d"), True))
        try:
            res_val = int(s.value(f"{_S}/map_resolution", 1024))
            ridx = self.map_resolution.findData(res_val)
            if ridx >= 0:
                self.map_resolution.setCurrentIndex(ridx)
            lridx = self.live_map_resolution.findData(res_val)
            if lridx >= 0:
                self.live_map_resolution.setCurrentIndex(lridx)
        except (TypeError, ValueError):
            pass
        try:
            class_val = s.value(f"{_S}/classification", "continuous")
            clidx = self.classification.findData(class_val)
            if clidx >= 0:
                self.classification.setCurrentIndex(clidx)
            lclidx = self.live_classification.findData(class_val)
            if lclidx >= 0:
                self.live_classification.setCurrentIndex(lclidx)
        except (TypeError, ValueError):
            pass
        self.cb_save_gpkg.setChecked(_truthy(s.value(f"{_S}/save_gpkg"), False))
        self.cb_use_cache.setChecked(_truthy(s.value(f"{_S}/use_cache"), True))
        try:
            self.height_scale.setValue(float(s.value(f"{_S}/height_scale", 1.0)))
            self.live_height_scale.setValue(float(s.value(f"{_S}/height_scale", 1.0)))
        except (TypeError, ValueError):
            pass
        self.cb_extrude.setEnabled(self.cb_buildings.isChecked())
        self.height_scale.setEnabled(self.cb_extrude.isChecked())
        self._update_download_classification_enabled()
        self._update_live_classification_enabled()
        self._update_download_color_preview()
        self._update_live_color_preview()

    def _save(self, p):
        s = QgsSettings()
        s.setValue(f"{_S}/area_source", p["area_source"])
        s.setValue(f"{_S}/shape", p["shape"])
        s.setValue(f"{_S}/building_color", p["building_color"])
        s.setValue(f"{_S}/theme", p["theme"])
        s.setValue(f"{_S}/max_km2", p["max_km2"])
        s.setValue(f"{_S}/buildings", p["want_buildings"])
        s.setValue(f"{_S}/extrude", self.cb_extrude.isChecked())
        s.setValue(f"{_S}/base", p["want_base"])
        s.setValue(f"{_S}/roads", p["want_roads"])
        s.setValue(f"{_S}/water", p["want_water"])
        s.setValue(f"{_S}/greens", p["want_greens"])
        s.setValue(f"{_S}/trees", p["want_trees"])
        s.setValue(f"{_S}/furniture", p["want_furniture"])
        s.setValue(f"{_S}/labels", p["want_labels"])
        s.setValue(f"{_S}/open3d", p["open_3d"])
        s.setValue(f"{_S}/map_resolution", p["map_resolution"])
        s.setValue(f"{_S}/classification", p["classification"])
        s.setValue(f"{_S}/height_scale", p["height_scale"])
        s.setValue(f"{_S}/save_gpkg", p["save_gpkg"])
        s.setValue(f"{_S}/use_cache", p["use_cache"])

    def set_status(self, text, *, error=False):
        color = "#a32525" if error else "#245d39"
        border = "#efc4c4" if error else "#c9dfd0"
        self.status.setStyleSheet(
            f"color:{color};background:#ffffff;border:1px solid {border};"
            "border-radius:7px;padding:5px 8px;font-size:11px;"
        )
        self.status.setText(text)

    def refresh_groups(self):
        current_name = self.group_combo.currentText()
        self.group_combo.blockSignals(True)
        self.group_combo.clear()

        root = QgsProject.instance().layerTreeRoot()
        for child in root.children():
            if isinstance(child, QgsLayerTreeGroup) and child.name().startswith("OSM Quick 3D"):
                self.group_combo.addItem(child.name(), child)

        idx = self.group_combo.findText(current_name)
        self.group_combo.setCurrentIndex(idx if idx >= 0 else 0)
        self.group_combo.blockSignals(False)
        self._on_group_selected()

    def _set_live_controls_enabled(self, enabled):
        controls = (
            self.live_theme_combo,
            self.scene_bg_color,
            self.live_height_scale,
            self.live_building_color,
            self.live_classification,
            self.live_cb_labels,
            self.live_cb_base,
            self.live_map_resolution,
            self.building_opacity,
            self.major_roads_color,
            self.minor_roads_color,
            self.greens_color,
            self.water_color,
            self.trees_color,
            self.trees_size,
        )
        for widget in controls:
            widget.setEnabled(enabled)

    def _on_group_selected(self):
        group_node = self.group_combo.currentData()
        if not group_node:
            self._set_live_controls_enabled(False)
            return

        self._set_live_controls_enabled(True)
        layers = self._get_layers(group_node)

        for widget in (
            self.live_theme_combo,
            self.scene_bg_color,
            self.live_height_scale,
            self.live_building_color,
            self.live_classification,
            self.live_cb_labels,
            self.live_cb_base,
            self.building_opacity,
            self.major_roads_color,
            self.minor_roads_color,
            self.greens_color,
            self.water_color,
            self.trees_color,
            self.trees_size,
        ):
            widget.blockSignals(True)

        detected_theme = "default"
        detected_color_mode = styling.BUILDING_COLOR_FUNCTION
        detected_classification = "continuous"
        for layer in layers.values():
            val = layer.customProperty("osm_quick_3d/theme")
            if val:
                detected_theme = str(val)
            mode = layer.customProperty("osm_quick_3d/building_color")
            if mode:
                detected_color_mode = str(mode)
            class_val = layer.customProperty("osm_quick_3d/classification")
            if class_val:
                detected_classification = str(class_val)

        tidx = self.live_theme_combo.findData(detected_theme)
        if tidx >= 0:
            self.live_theme_combo.setCurrentIndex(tidx)
        try:
            self.scene_bg_color.setColor(self.iface.mapCanvas().canvasColor())
        except Exception:
            self.scene_bg_color.setColor(QColor(styling.THEMES.get(detected_theme, styling.THEMES["default"])["bg"]))
        midx = self.live_building_color.findData(detected_color_mode)
        if midx >= 0:
            self.live_building_color.setCurrentIndex(midx)
        cidx = self.live_classification.findData(detected_classification)
        if cidx >= 0:
            self.live_classification.setCurrentIndex(cidx)

        buildings = layers.get("buildings")
        if buildings:
            scale = buildings.customProperty("osm_quick_3d/height_scale")
            try:
                self.live_height_scale.setValue(float(scale))
            except (TypeError, ValueError):
                pass
            self.live_cb_labels.setChecked(buildings.labelsEnabled())
            self.building_opacity.setValue(int(buildings.opacity() * 100.0))

        roads = layers.get("roads")
        if roads:
            c_maj = styling.get_layer_color(roads, "major")
            if c_maj:
                self.major_roads_color.setColor(QColor(c_maj))
            c_min = styling.get_layer_color(roads, "residential")
            if c_min:
                self.minor_roads_color.setColor(QColor(c_min))

        greens = layers.get("greens")
        if greens:
            c_grn = styling.get_layer_color(greens, "park")
            if c_grn:
                self.greens_color.setColor(QColor(c_grn))

        water = layers.get("waterareas") or layers.get("waterlines")
        if water:
            c_wat = styling.get_layer_color(water)
            if c_wat:
                self.water_color.setColor(QColor(c_wat))

        trees = layers.get("trees")
        if trees:
            c_trs = styling.get_layer_color(trees)
            if c_trs:
                self.trees_color.setColor(QColor(c_trs))
            self.trees_size.setValue(styling.get_trees_size(trees))

        base = layers.get("base")
        base_3d = layers.get("base_3d")
        if base or base_3d:
            self.live_cb_base.setChecked(base_3d is not None and base_3d.renderer3D() is not None)

        for widget in (
            self.live_theme_combo,
            self.scene_bg_color,
            self.live_height_scale,
            self.live_building_color,
            self.live_classification,
            self.live_cb_labels,
            self.live_cb_base,
            self.building_opacity,
            self.major_roads_color,
            self.minor_roads_color,
            self.greens_color,
            self.water_color,
            self.trees_color,
            self.trees_size,
        ):
            widget.blockSignals(False)

        self._update_live_classification_enabled()
        self._update_live_color_preview()

    def _get_layers(self, group_node) -> dict:
        layers = {}
        for child in group_node.children():
            if isinstance(child, QgsLayerTreeLayer):
                layer = child.layer()
                if not layer:
                    continue
                name = layer.name()
                if name == "OSM Buildings":
                    layers["buildings"] = layer
                elif name == "OSM Roads":
                    layers["roads"] = layer
                elif name == "OSM Base":
                    layers["base"] = layer
                elif name == "OSM Base 3D":
                    layers["base_3d"] = layer
                elif name == "OSM Greens":
                    layers["greens"] = layer
                elif name == "OSM Trees":
                    layers["trees"] = layer
                elif name == "OSM Water areas":
                    layers["waterareas"] = layer
                elif name == "OSM Waterlines":
                    layers["waterlines"] = layer
                elif name == "OSM Bike lanes":
                    layers["bikelanes"] = layer
        return layers

    def _paint_preview(self, frame, stops):
        if not stops:
            return
        if len(stops) == 1:
            stops = stops * 2
        n = len(stops) - 1
        parts = ", ".join(f"stop:{i / n:.4f} {hexv}" for i, hexv in enumerate(stops))
        frame.setStyleSheet(
            "QFrame{border:1px solid #d2d9da;border-radius:6px;"
            f"background:qlineargradient(x1:0,y1:0,x2:1,y2:0,{parts});}}"
        )

    def _update_download_color_preview(self):
        theme_key = self.theme_combo.currentData() or "default"
        stops = styling.building_color_swatches(self.building_color.currentData(), theme=theme_key)
        self._paint_preview(self.download_color_preview, stops)

    def _update_live_color_preview(self):
        theme_key = self.live_theme_combo.currentData() or "default"
        stops = styling.building_color_swatches(self.live_building_color.currentData(), theme=theme_key)
        self._paint_preview(self.live_color_preview, stops)

    def _update_download_classification_enabled(self):
        mode = self.building_color.currentData()
        self.classification.setEnabled(mode != styling.BUILDING_COLOR_FUNCTION)

    def _update_live_classification_enabled(self):
        mode = self.live_building_color.currentData()
        self.live_classification.setEnabled(mode != styling.BUILDING_COLOR_FUNCTION)

    def _on_live_theme_changed(self):
        theme_key = self.live_theme_combo.currentData()
        if not theme_key:
            return

        t_data = styling.THEMES.get(theme_key, styling.THEMES["default"])
        for widget, key in (
            (self.major_roads_color, "roads_major"),
            (self.minor_roads_color, "roads_minor"),
            (self.greens_color, "greens"),
            (self.water_color, "water"),
            (self.trees_color, "trees"),
            (self.scene_bg_color, "bg"),
        ):
            widget.blockSignals(True)
            widget.setColor(QColor(t_data[key]))
            widget.blockSignals(False)

        self.iface.mapCanvas().setCanvasColor(QColor(t_data["bg"]))
        group_node = self.group_combo.currentData()
        if group_node:
            for child in group_node.children():
                if isinstance(child, QgsLayerTreeLayer):
                    layer = child.layer()
                    if layer:
                        layer.setCustomProperty("osm_quick_3d/theme", theme_key)

        self._update_live_color_preview()
        self._apply_changes()
        self._apply_advanced_changes()
        self._apply_resolution()

    def _apply_scene_background(self, color=None):
        qcolor = color if color is not None else self.scene_bg_color.color()
        try:
            self.iface.mapCanvas().setCanvasColor(qcolor)
        except Exception:
            pass
        self._refresh_3d_view(auto=True)
        self._apply_changes()

    def _apply_changes(self):
        group_node = self.group_combo.currentData()
        if not group_node:
            return

        canvas = self.iface.mapCanvas()
        canvas.freeze(True)
        try:
            layers = self._get_layers(group_node)
            buildings = layers.get("buildings")
            roads = layers.get("roads")
            base = layers.get("base")
            base_3d = layers.get("base_3d")

            theme_key = self.live_theme_combo.currentData() or "default"
            color_mode = self.live_building_color.currentData()
            classification = self.live_classification.currentData()
            scale = self.live_height_scale.value()
            want_labels = self.live_cb_labels.isChecked()
            want_base = self.live_cb_base.isChecked()

            if buildings:
                buildings.setCustomProperty("osm_quick_3d/theme", theme_key)
                buildings.setCustomProperty("osm_quick_3d/building_color", color_mode)
                buildings.setCustomProperty("osm_quick_3d/classification", classification)
                buildings.setCustomProperty("osm_quick_3d/height_scale", scale)
                styling.style_buildings(buildings, color_mode, classification=classification, theme=theme_key)
                native3d.apply_building_extrusion(
                    buildings,
                    color_hex=styling.building_base_color(color_mode, theme=theme_key),
                    height_scale=scale,
                    color_expr=styling.building_color_expression(
                        color_mode, classification=classification, layer=buildings, theme=theme_key),
                    color_mode=color_mode,
                    classification=classification,
                    theme=theme_key,
                )
                if want_labels:
                    styling.label_by_name(buildings, size=8.0)
                else:
                    buildings.setLabeling(None)
                    buildings.setLabelsEnabled(False)
                    buildings.triggerRepaint()

            if roads:
                if want_labels:
                    styling.label_by_name(roads, size=7.5)
                else:
                    roads.setLabeling(None)
                    roads.setLabelsEnabled(False)
                    roads.triggerRepaint()

            if base:
                transparent_val = base.customProperty("osm_quick_3d/transparent")
                transparent = str(transparent_val).strip().lower() in ("true", "1", "yes", "on")
                bg_color_hex = canvas.canvasColor().name()
                styling.style_base(base, color_mode, transparent=transparent, bg_color_hex=bg_color_hex, theme=theme_key)

            if want_base:
                if not base_3d and base:
                    try:
                        base_3d = base.clone()
                        base_3d.setName("OSM Base 3D")
                        styling.style_base_3d_2d(base_3d)
                        QgsProject.instance().addMapLayer(base_3d, False)
                        group_node.insertLayer(len(group_node.children()), base_3d)
                    except Exception:
                        base_3d = None
                if base_3d:
                    native3d.apply_base_slab(
                        base_3d, depth=BASE_DEPTH_M, color_hex=styling.base_color_hex(color_mode, theme=theme_key))
            elif base_3d:
                base_3d.setRenderer3D(None)
                base_3d.triggerRepaint()
        finally:
            canvas.freeze(False)
            canvas.refresh()
        self._refresh_3d_view(auto=True)

    def _apply_advanced_changes(self):
        group_node = self.group_combo.currentData()
        if not group_node:
            return

        canvas = self.iface.mapCanvas()
        canvas.freeze(True)
        try:
            layers = self._get_layers(group_node)
            buildings = layers.get("buildings")
            roads = layers.get("roads")
            greens = layers.get("greens")
            waterareas = layers.get("waterareas")
            waterlines = layers.get("waterlines")
            trees = layers.get("trees")

            if buildings:
                buildings.setOpacity(self.building_opacity.value() / 100.0)
                buildings.triggerRepaint()

            if roads:
                major_color = self.major_roads_color.color().name()
                for cat in ("major", "primary", "secondary", "tertiary"):
                    styling.set_layer_color(roads, major_color, cat)
                minor_color = self.minor_roads_color.color().name()
                for cat in ("residential", "service", "foot", "other"):
                    styling.set_layer_color(roads, minor_color, cat)

            if greens:
                greens_color = self.greens_color.color().name()
                for cat in ("park", "forest", "green", "pitch", "cemetery"):
                    styling.set_layer_color(greens, greens_color, cat)

            if waterareas:
                styling.set_layer_color(waterareas, self.water_color.color().name())
            if waterlines:
                styling.set_layer_color(waterlines, self.water_color.color().name())

            if trees:
                trees_color = self.trees_color.color().name()
                styling.set_layer_color(trees, trees_color)
                styling.set_trees_size(trees, self.trees_size.value())
                native3d.apply_tree_3d(trees, color_hex=trees_color)
        finally:
            canvas.freeze(False)
            canvas.refresh()
        self._refresh_3d_view(auto=True)

    def _apply_resolution(self):
        res = self.live_map_resolution.currentData()
        if res:
            self._refresh_3d_view(auto=True)
            QgsSettings().setValue(f"{_S}/map_resolution", res)

    def _current_bg_color(self):
        try:
            return self.scene_bg_color.color().name()
        except Exception:
            pass
        try:
            return self.iface.mapCanvas().canvasColor().name()
        except Exception:
            theme_key = self.live_theme_combo.currentData() or self.theme_combo.currentData() or "default"
            return styling.THEMES.get(theme_key, styling.THEMES["default"])["bg"]

    def _on_open_3d_clicked(self):
        if self.embed_3d_view(auto=False):
            self.set_status("Embedded 3D scene ready.")
        else:
            self.set_status("Could not create the embedded 3D scene.", error=True)

    def _dock_for_widget(self, widget):
        current = widget
        while current is not None:
            if isinstance(current, QDockWidget):
                return current
            current = current.parent()
        return None

    def _find_external_3d_dock(self):
        try:
            for dock in self.iface.mainWindow().findChildren(QDockWidget):
                if dock is self:
                    continue
                text = f"{dock.windowTitle() or ''} {dock.objectName() or ''}".lower()
                target = self._embedded_canvas_name.lower()
                if target in text or ("3d" in text and ("map" in text or "harita" in text)):
                    return dock
        except Exception:
            pass
        return None

    def _current_3d_layers(self):
        group_node = self.group_combo.currentData() if hasattr(self, "group_combo") else None
        if group_node:
            layers = []
            for child in group_node.children():
                if isinstance(child, QgsLayerTreeLayer):
                    layer = child.layer()
                    if layer:
                        layers.append(layer)
            if layers:
                return layers
        try:
            layers = list(self.iface.mapCanvas().layers())
            if layers:
                return layers
        except Exception:
            pass
        try:
            return list(QgsProject.instance().mapLayers().values())
        except Exception:
            return []

    def _refresh_3d_view(self, auto=False):
        canvas = self._embedded_canvas or native3d.find_owned_3d_map_canvas(
            self.iface, self._embedded_canvas_name
        )
        if canvas is None:
            return False
        return native3d.configure_3d_map_canvas(
            self.iface,
            canvas,
            self.live_map_resolution.currentData() or self.map_resolution.currentData() or 1024,
            self._current_bg_color(),
            layers=self._current_3d_layers(),
        )

    def _find_3d_canvas(self):
        if self._embedded_canvas is not None:
            return self._embedded_canvas
        return native3d.find_owned_3d_map_canvas(self.iface, self._embedded_canvas_name)

    def embed_3d_view(self, auto=False):
        if not self.isVisible() and auto:
            return False
        if self._embedded_canvas is not None and self._embedded_container is not None:
            self._refresh_3d_view(auto=auto)
            self.tab_widget.setCurrentWidget(self.view_tab)
            return True

        canvas = self._find_3d_canvas()
        if canvas is None and not auto:
            canvas = native3d.create_embedded_3d_map_canvas(
                self.iface,
                resolution=self.live_map_resolution.currentData() or self.map_resolution.currentData() or 1024,
                bg_color_hex=self._current_bg_color(),
                name=self._embedded_canvas_name,
                layers=self._current_3d_layers(),
            )
        if canvas is None:
            if not auto:
                self.set_status("No embedded 3D scene could be created.", error=True)
            return False

        native3d._mark_canvas_owned(canvas, self._embedded_canvas_name)
        self._embedded_canvas = canvas
        self._embedded_original_parent = canvas.parent()
        self._embedded_original_layout = (
            self._embedded_original_parent.layout()
            if self._embedded_original_parent is not None and hasattr(self._embedded_original_parent, "layout")
            else None
        )
        self._embedded_original_dock = (
            self._dock_for_widget(canvas) if isinstance(canvas, QWidget) else self._find_external_3d_dock()
        )

        try:
            if self.view_layout.indexOf(self.view_placeholder) >= 0:
                self.view_layout.removeWidget(self.view_placeholder)
                self.view_placeholder.hide()
        except Exception:
            pass
        try:
            if self._embedded_original_layout is not None:
                self._embedded_original_layout.removeWidget(canvas)
        except Exception:
            pass
        try:
            if isinstance(canvas, QWidget):
                canvas.setParent(self.view_host)
                canvas.setSizePolicy(_size_policy("Expanding"), _size_policy("Expanding"))
                canvas.setMinimumSize(220, 180)
                self.view_layout.addWidget(canvas, 1)
                canvas.show()
                canvas.update()
            else:
                self._embedded_container = QWidget.createWindowContainer(canvas, self.view_host)
                self._embedded_container.setSizePolicy(_size_policy("Expanding"), _size_policy("Expanding"))
                self._embedded_container.setMinimumSize(220, 180)
                self.view_layout.addWidget(self._embedded_container, 1)
                self._embedded_container.show()
                for method in ("requestUpdate", "update"):
                    fn = getattr(canvas, method, None)
                    if fn is None:
                        continue
                    try:
                        fn()
                        break
                    except Exception:
                        continue
        except Exception:
            self._embedded_canvas = None
            self._embedded_container = None
            if not auto:
                self.set_status("The 3D view could not be embedded.", error=True)
            return False

        if self._embedded_original_dock is not None and self._embedded_original_dock is not self:
            try:
                self._embedded_original_dock.hide()
            except Exception:
                pass

        self.tab_widget.setCurrentWidget(self.view_tab)
        native3d.configure_3d_map_canvas(
            self.iface,
            canvas,
            self.live_map_resolution.currentData() or self.map_resolution.currentData() or 1024,
            self._current_bg_color(),
            layers=self._current_3d_layers(),
        )
        if not auto:
            self.set_status("Embedded 3D scene ready.")
        return True

    def cleanup_embedded_3d(self):
        canvas = self._embedded_canvas
        if canvas is None:
            return

        if self._embedded_container is not None:
            try:
                self.view_layout.removeWidget(self._embedded_container)
                self._embedded_container.setParent(None)
                self._embedded_container.deleteLater()
            except Exception:
                pass
        elif isinstance(canvas, QWidget):
            try:
                self.view_layout.removeWidget(canvas)
            except Exception:
                pass

        try:
            self.iface.closeMapCanvas(self._embedded_canvas_name)
        except Exception:
            try:
                canvas.setParent(None)
                canvas.close()
                canvas.deleteLater()
            except Exception:
                pass

        self._embedded_canvas = None
        self._embedded_container = None
        self._embedded_original_parent = None
        self._embedded_original_layout = None
        self._embedded_original_dock = None

        try:
            if self.view_layout.indexOf(self.view_placeholder) < 0:
                self.view_layout.addWidget(self.view_placeholder, 1)
            self.view_placeholder.show()
        except Exception:
            pass

    def showEvent(self, event):
        if hasattr(self, "_embed_timer") and not self._embed_timer.isActive():
            self._embed_timer.start()
        super().showEvent(event)

    def closeEvent(self, event):
        self.cleanup_embedded_3d()
        super().closeEvent(event)
