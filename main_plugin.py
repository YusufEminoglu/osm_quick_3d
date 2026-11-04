# -*- coding: utf-8 -*-
"""OSM Quick 3D — main plugin class.

One toolbar button opens the controller dock; on run it downloads OpenStreetMap
for the chosen study area (rectangle, rounded rectangle, circle, hexagon, or the
user's own selected polygon), adds the layers to QGIS already styled by function,
extrudes the buildings with native 3D symbology over a recessed ground base, and
(optionally) opens a 3D Map View. No web server, no browser — built to scale to
larger areas than the Three.js companion plugin (osm_3d_model).

The download runs in a QgsTask on a worker thread (contract C1/C2 of
DEVELOPMENT_SET_v0.52): the worker only downloads and parses; every layer is
styled, saved and added to the project here, on the QGIS main thread, so QGIS
stays responsive and Cancel answers at once.
"""
from __future__ import annotations

import contextlib
import os
import re
import sqlite3
import tempfile
import time
import traceback

from qgis.PyQt.QtCore import QObject, Qt, QThread, QTimer, pyqtSignal
from qgis.PyQt.QtGui import QColor, QIcon
from qgis.PyQt.QtWidgets import QAction, QFileDialog
from qgis.core import (
    Qgis,
    QgsApplication,
    QgsCoordinateReferenceSystem,
    QgsCoordinateTransform,
    QgsFeedback,
    QgsGeometry,
    QgsMessageLog,
    QgsProject,
    QgsRasterLayer,
    QgsRectangle,
    QgsTask,
    QgsVectorFileWriter,
    QgsVectorLayer,
)

from . import native3d, osm_download, styling
from .osm_download import (
    BASE_DEPTH_M,
    SHAPE_POLYGON,
    SHAPE_RECTANGLE,
    build_base_layer,
    shape_study_area,
    utm_epsg_for,
)

TITLE = "OSM Quick 3D"
CANCELLED_TEXT = "Download cancelled."
#: A cancelled download that has not ended after this long is let go, so the
#: dock is free again within 2 s whatever the network does.
_CANCEL_GRACE_MS = 1500
#: Plain names of the result layers, for the summary.
_PLAIN_NAMES = {
    "buildings": "buildings",
    "roads": "roads",
    "bikelanes": "cycle paths",
    "greens": "green areas",
    "waterareas": "water areas",
    "waterlines": "waterways",
    "trees": "trees",
    "busstops": "bus stops",
    "benches": "benches",
    "lights": "street lights",
    "trashbins": "litter bins",
}
TEMPORARY_NOTE = (
    "These layers are temporary: they vanish when the project is reopened. "
    "Tick “Save result to GeoPackage” to keep them."
)


def _is_polygon_geometry(geom) -> bool:
    """True for a (multi)polygon geometry across QGIS 3.x/4.x geometry-type enums.

    ``QgsWkbTypes.GeometryType`` (3.x) and ``Qgis.GeometryType`` (4.x) both number
    polygons as 2, and ``int()`` works on either sip- or Qt6-scoped enum, so this
    stays correct without importing a version-specific enum name.
    """
    try:
        return int(geom.type()) == 2
    except Exception:
        return False


def _enum(owner, scope, name):
    holder = getattr(owner, scope, None)
    if holder is not None and hasattr(holder, name):
        return getattr(holder, name)
    return getattr(owner, name)


def log(text, level="Info"):
    """Details for the QGIS log panel (OSM Quick 3D tab); the dock stays short."""
    with contextlib.suppress(Exception):
        QgsMessageLog.logMessage(str(text), TITLE, _enum(Qgis, "MessageLevel", level))


def short_text(text, limit=220) -> str:
    """One or two plain sentences for the dock: no addresses, no wall of text."""
    text = re.sub(r"https?://([^/\s]+)\S*", r"\1", str(text or ""))
    text = " ".join(text.split())
    if len(text) <= limit:
        return text
    cut = max(text.rfind(". ", 0, limit), text.rfind("? ", 0, limit), text.rfind("! ", 0, limit))
    if cut > 40:
        return text[:cut + 1]
    return text[:limit - 1].rstrip() + "…"


def _cancel_errors():
    return tuple(
        cls for cls in (getattr(osm_download, "OsmDownloadCancelled", None),
                        getattr(osm_download, "OsmCancelledError", None))
        if isinstance(cls, type)
    )


def _cancel_quietly(task):
    """Cancel a download that may already have ended (its C++ side deleted)."""
    with contextlib.suppress(RuntimeError):
        task.cancel()


def _hand_to_main_thread(result):
    """Move the memory layers the worker built to the main thread (contract C2)."""
    app = QgsApplication.instance()
    if app is None or not isinstance(result, dict):
        return
    main = app.thread()
    here = QThread.currentThread()
    for value in result.values():
        if isinstance(value, QObject):
            with contextlib.suppress(Exception):
                if value.thread() == here and here != main:
                    value.moveToThread(main)


class DownloadTask(QgsTask):
    """Downloads and parses OSM on a worker thread; builds nothing in the project.

    Each step is reported through ``statusChanged`` (emitted on the worker,
    delivered queued on the main thread). Cancelling cancels the QgsFeedback,
    which ends the transfer at once. An answer that has already arrived is
    kept, even when Cancel comes a moment too late.
    """

    statusChanged = pyqtSignal(str)

    def __init__(self, area_utm, epsg, use_cache, datasets):
        super().__init__(f"{TITLE}: download OpenStreetMap", _enum(QgsTask, "Flag", "CanCancel"))
        self.area_utm = QgsGeometry(area_utm)
        self.epsg = int(epsg)
        self.use_cache = bool(use_cache)
        self.datasets = tuple(datasets)
        self.feedback = QgsFeedback()
        self.callback = None
        self.result_data = None
        self.error_text = ""
        self.error_detail = ""
        self.cancelled = False

    def cancel(self):
        with contextlib.suppress(RuntimeError):
            self.feedback.cancel()
        super().cancel()

    def _status(self, text):
        self.statusChanged.emit(str(text))

    def run(self):
        if self.feedback.isCanceled() or self.isCanceled():
            self.cancelled = True
            self.error_text = CANCELLED_TEXT
            return False
        try:
            result = osm_download.download_osm_for_area(
                self.area_utm, self.epsg,
                use_cache=self.use_cache,
                datasets=self.datasets,
                status=self._status,
                feedback=self.feedback,
            )
        except Exception as exc:
            if isinstance(exc, _cancel_errors()) or self.feedback.isCanceled():
                self.cancelled = True
                self.error_text = CANCELLED_TEXT
            else:
                self.error_text = str(exc) or type(exc).__name__
                if not isinstance(exc, osm_download.OsmDownloadError):
                    self.error_detail = traceback.format_exc()
            return False
        _hand_to_main_thread(result)
        self.result_data = result
        return True

    def finished(self, result):
        callback, self.callback = self.callback, None
        if callback is not None:
            callback(self)


class GpkgTarget:
    """One GeoPackage the run writes into, without harming anything already there.

    The file is never recreated and no existing table is replaced: each layer
    goes into a new table. When ``buildings`` (say) is already taken, the
    whole run uses the next free number (``buildings_2``, ``roads_2``…), so a
    re-run into the same file works even while the earlier layers are loaded.
    """

    def __init__(self, path):
        self.path = path
        self.suffix = ""
        self.written = []
        self.failed = []
        self.first_error = ""

    @staticmethod
    def existing_tables(path):
        """Lower-case table names of an existing GeoPackage; raises ValueError if it is none."""
        if not os.path.exists(path):
            return set()
        names = set()
        try:
            con = sqlite3.connect(GpkgTarget.sqlite_uri(path), uri=True, timeout=1.0)
            try:
                names = {str(row[0]).lower() for row in con.execute(
                    "SELECT name FROM sqlite_master WHERE type IN ('table', 'view')")}
            finally:
                con.close()
        except sqlite3.OperationalError as exc:
            # Locked or unreadable (another program has it open): not the
            # same as a file that is no database at all.
            log(f"Could not read {path}: {exc}", "Warning")
            raise ValueError(
                f"{os.path.basename(path)} could not be read (is it open in another program?), "
                "so nothing was downloaded. Close it there or choose another file."
            ) from exc
        except sqlite3.DatabaseError:
            names = set()
        if "gpkg_contents" not in names:
            raise ValueError(
                f"{os.path.basename(path)} is not a GeoPackage, so nothing was written to it. "
                "Choose a new file name or an existing GeoPackage."
            )
        return names

    @staticmethod
    def sqlite_uri(path):
        """A read-only SQLite URI; a network share (\\\\host\\share\\x) keeps its host."""
        share = "//" if path.startswith("\\\\") else ""
        return ("file:" + share + path.replace("\\", "/").replace("%", "%25").replace("?", "%3F")
                .replace("#", "%23") + "?mode=ro")

    @staticmethod
    def folder_writable(folder):
        """True when a file can really be created in ``folder`` (ACLs and shares included)."""
        try:
            with tempfile.TemporaryFile(dir=folder):
                return True
        except OSError:
            return False

    def check(self):
        """Refuse a path that cannot work before anything is downloaded."""
        name = os.path.basename(self.path)
        folder = os.path.dirname(self.path) or "."
        if not os.path.isdir(folder):
            raise ValueError(f"The folder for {name} does not exist.")
        self.existing_tables(self.path)
        if os.path.exists(self.path) and not os.access(self.path, os.W_OK):
            raise ValueError(
                f"Could not save to {name}: the file is read-only, so nothing was downloaded. "
                "Choose another file.")
        # SQLite writes a journal next to the file, so the folder must take new files too.
        if not self.folder_writable(folder):
            raise ValueError(
                f"Could not save to {name}: its folder cannot be written to, so nothing was "
                "downloaded. Choose another folder.")

    def choose_names(self, keys):
        """Pick one free numbered suffix for every table of this run."""
        try:
            taken = self.existing_tables(self.path)
        except ValueError:
            taken = set()
        number = 1
        while True:
            suffix = "" if number == 1 else f"_{number}"
            if not any(f"{key}{suffix}".lower() in taken for key in keys):
                self.suffix = suffix
                return
            number += 1

    def table(self, key):
        return f"{key}{self.suffix}"

    def write(self, layer, key):
        """Write ``layer`` as a new table; return the layer read back, or None."""
        table = self.table(key)
        options = QgsVectorFileWriter.SaveVectorOptions()
        options.driverName = "GPKG"
        options.layerName = table
        options.fileEncoding = "UTF-8"
        action = getattr(QgsVectorFileWriter, "ActionOnExistingFile", QgsVectorFileWriter)
        if os.path.exists(self.path):
            # The table name is new, so this adds a table and replaces nothing.
            options.actionOnExistingFile = getattr(action, "CreateOrOverwriteLayer")
        else:
            options.actionOnExistingFile = getattr(action, "CreateOrOverwriteFile")
        error = ""
        try:
            if os.path.exists(self.path) and table.lower() in self.existing_tables(self.path):
                error = f"a table called {table} already exists"
            else:
                result = QgsVectorFileWriter.writeAsVectorFormatV3(
                    layer, str(self.path), QgsProject.instance().transformContext(), options)
                code, message = result[0], result[1]
                no_error = _enum(QgsVectorFileWriter, "WriterError", "NoError")
                if code != no_error:
                    error = str(message or f"write error {code}")
        except Exception as exc:
            error = str(exc) or type(exc).__name__
        loaded = None
        if not error:
            loaded = QgsVectorLayer(f"{self.path}|layername={table}", layer.name(), "ogr")
            if not loaded.isValid():
                error = "the saved layer could not be read back"
                loaded = None
        if loaded is None:
            self.failed.append(key)
            if not self.first_error:
                self.first_error = error
            log(f"Could not save {key} to {self.path}: {error}", "Warning")
            return None
        with contextlib.suppress(Exception):
            loaded.setCustomProperty("osm_quick_3d/written_by_plugin", True)
        self.written.append(table)
        return loaded


def save_style_in_gpkg(layer):
    """Store the 2D + 3D style in the GeoPackage's layer_styles table."""
    with contextlib.suppress(Exception):
        save_v2 = getattr(layer, "saveStyleToDatabaseV2", None)
        if save_v2 is not None:
            save_v2(layer.name(), "OSM Quick 3D default style", True, "")
        else:
            layer.saveStyleToDatabase(layer.name(), "OSM Quick 3D default style", True, "")


class OsmQuick3DPlugin:
    MENU_NAME = "&OSM Quick 3D"

    def __init__(self, iface):
        self.iface = iface
        self.plugin_dir = os.path.dirname(__file__)
        self.icon_path = os.path.join(self.plugin_dir, "icons", "icon.png")
        self.action = None
        self.dock_action = None
        self.dock = None
        self._task = None
        self._retired_tasks = []
        self._pending = None
        self._run_started = 0.0
        self._unloaded = False

    def initGui(self):
        self.action = QAction(QIcon(self.icon_path), "OSM Quick 3D", self.iface.mainWindow())
        self.action.setStatusTip("Open the OSM Quick 3D panel")
        self.action.triggered.connect(self.show_dock)
        self.iface.addToolBarIcon(self.action)
        self.iface.addPluginToMenu(self.MENU_NAME, self.action)

        self.dock_action = QAction("3D Controller Dock", self.iface.mainWindow())
        self.dock_action.setStatusTip("Open the OSM Quick 3D panel")
        self.dock_action.triggered.connect(self.show_dock)
        self.iface.addPluginToMenu(self.MENU_NAME, self.dock_action)

    def unload(self):
        self._unloaded = True
        task, self._task = self._task, None
        self._pending = None
        if task is not None:
            self._retire(task)
            # Cancel from the event loop, after unload has returned (the timer
            # cannot fire before). A cancelled transfer tears its Qt objects
            # down on the worker, and that teardown needs the Python GIL while
            # holding one of Qt's shared signal/slot locks; the dock teardown
            # below runs with the GIL held and takes those same locks, so
            # cancelling first could deadlock the GUI thread (a hang in
            # dock.setParent(None) while the worker waited in
            # PyGILState_Ensure under ~QObject).
            QTimer.singleShot(0, lambda: _cancel_quietly(task))
        for action in (self.action, self.dock_action):
            if action is None:
                continue
            with contextlib.suppress(TypeError, RuntimeError):
                action.triggered.disconnect(self.show_dock)
            self.iface.removePluginMenu(self.MENU_NAME, action)
            if action is self.action:
                self.iface.removeToolBarIcon(action)
            action.setParent(None)  # detached now, not when the deferred delete runs
            action.deleteLater()
        self.action = None
        self.dock_action = None
        dock, self.dock = self.dock, None
        if dock is not None:
            for signal, slot in self._dock_connections(dock):
                with contextlib.suppress(TypeError, RuntimeError):
                    signal.disconnect(slot)
            with contextlib.suppress(Exception):
                dock.prepare_for_unload()
            with contextlib.suppress(Exception):
                self.iface.removeDockWidget(dock)
            dock.close()
            dock.setParent(None)
            dock.deleteLater()

    def _retire(self, task):
        """Let a running download go: no callback into the dock, no status
        lines, kept referenced until it really ends, then forgotten."""
        if task not in self._retired_tasks:
            self._retired_tasks.append(task)
        task.callback = self._forget_task
        with contextlib.suppress(TypeError, RuntimeError):
            task.statusChanged.disconnect()

    def _forget_task(self, task):
        """A retired download has ended: drop the last reference to it."""
        with contextlib.suppress(ValueError):
            self._retired_tasks.remove(task)

    def _dock_connections(self, dock):
        return (
            (dock.runRequested, self.run_action),
            (dock.basemapRequested, self.add_osm_basemap),
            (dock.cancelRequested, self._cancel_run),
        )

    def show_dock(self):
        if self._unloaded:
            return
        if self.dock is None:
            from .dock import PluginDockWidget
            self.dock = PluginDockWidget(self.iface, self.iface.mainWindow())
            for signal, slot in self._dock_connections(self.dock):
                signal.connect(slot)
            try:
                right_area = Qt.DockWidgetArea.RightDockWidgetArea
            except AttributeError:
                right_area = getattr(Qt, "RightDockWidgetArea")
            self.iface.addDockWidget(right_area, self.dock)
            with contextlib.suppress(Exception):
                try:
                    horizontal = Qt.Orientation.Horizontal
                except AttributeError:
                    horizontal = getattr(Qt, "Horizontal")
                self.iface.mainWindow().resizeDocks([self.dock], [430], horizontal)
        if not self.dock.is_running():
            self.dock.refresh_groups()
        self.dock.show()
        self.dock.raise_()
        with contextlib.suppress(Exception):
            self.dock.activateWindow()

    # ── OSM basemap ────────────────────────────────────────────────────────
    OSM_BASEMAP_NAME = "OpenStreetMap"
    OSM_BASEMAP_URI = (
        "type=xyz&url=https://tile.openstreetmap.org/%7Bz%7D/%7Bx%7D/%7By%7D.png"
        "&zmax=19&zmin=0&crs=EPSG3857"
    )

    def add_osm_basemap(self):
        """Add a live OpenStreetMap XYZ tile layer to the canvas as a basemap.

        A one-click way to see and frame the study area before downloading. The
        layer is added to the bottom of the layer tree so it sits under any other
        layers, and re-clicking reuses the existing basemap instead of stacking
        duplicates.
        """
        project = QgsProject.instance()
        root = project.layerTreeRoot()

        # Reuse an existing OSM basemap rather than stacking duplicates.
        for layer in project.mapLayers().values():
            if (
                isinstance(layer, QgsRasterLayer)
                and layer.name() == self.OSM_BASEMAP_NAME
                and (layer.source() == self.OSM_BASEMAP_URI
                     or "tile.openstreetmap.org" in layer.source())
            ):
                node = root.findLayer(layer.id())
                if node is not None:
                    node.setItemVisibilityChecked(True)
                self._set_status("The OpenStreetMap basemap is already on the map.")
                return

        layer = QgsRasterLayer(self.OSM_BASEMAP_URI, self.OSM_BASEMAP_NAME, "wms")
        if not layer.isValid():
            self._set_status(
                "Could not load the OpenStreetMap basemap (no network, or the tiles are unavailable).",
                error=True,
            )
            return

        layer.setAttribution("© OpenStreetMap contributors")
        layer.setAttributionUrl("https://www.openstreetmap.org/copyright")
        project.addMapLayer(layer, False)
        root.addLayer(layer)  # append = bottom of the tree, under other layers
        self._set_status("Added the OpenStreetMap basemap. Zoom to your area, then click Run.")

    # ── helpers ────────────────────────────────────────────────────────────
    def _set_status(self, text, error=False, **kwargs):
        if self.dock:
            self.dock.set_status(text, error=error, **kwargs)

    def _fail(self, text, detail=""):
        """A failure in the dock in short plain words; the details go to the QGIS log."""
        log(f"{text}\n{detail}".strip(), "Warning")
        message = short_text(text)
        if detail:
            message = f"{message} Details are in the QGIS log (OSM Quick 3D)."
        self._set_status(message, error=True)

    def _area_rect_and_crs(self, area_source):
        """Return (QgsRectangle, source CRS) for the chosen area source."""
        canvas = self.iface.mapCanvas()
        if area_source == "selection":
            layer = self.iface.activeLayer()
            count = getattr(layer, "selectedFeatureCount", None)
            if count is None or count() == 0:
                raise ValueError(
                    "No features are selected. Select features in the active layer, "
                    "or set “Area from” to the current map view."
                )
            return layer.boundingBoxOfSelected(), layer.crs()
        return canvas.extent(), canvas.mapSettings().destinationCrs()

    def _area_utm(self, rect, src_crs, max_km2):
        """Reproject the area rectangle to its UTM zone, clamped to max_km2.

        Returns (QgsRectangle in UTM, EPSG, area_km2). The study-area shape is
        applied to this rectangle by the caller.
        """
        project = QgsProject.instance()
        wgs = QgsCoordinateReferenceSystem.fromEpsgId(4326)
        wgs_rect = QgsCoordinateTransform(src_crs, wgs, project).transformBoundingBox(rect)
        cx = (wgs_rect.xMinimum() + wgs_rect.xMaximum()) / 2.0
        cy = (wgs_rect.yMinimum() + wgs_rect.yMaximum()) / 2.0
        epsg = utm_epsg_for(cx, cy)
        utm = QgsCoordinateReferenceSystem.fromEpsgId(epsg)
        urect = QgsCoordinateTransform(src_crs, utm, project).transformBoundingBox(rect)
        w, h = urect.width(), urect.height()
        area_km2 = (w * h) / 1.0e6
        if max_km2 and area_km2 > max_km2 and area_km2 > 0:
            f = (max_km2 / area_km2) ** 0.5
            c = urect.center()
            urect = QgsRectangle(
                c.x() - w * f / 2.0, c.y() - h * f / 2.0,
                c.x() + w * f / 2.0, c.y() + h * f / 2.0,
            )
            area_km2 = max_km2
        return urect, epsg, area_km2

    def _selected_polygon_geom(self):
        """Dissolved geometry of the active layer's selected polygon features.

        Returns ``(QgsGeometry, source CRS)`` for the union of the selected polygon
        features, or ``(None, None)`` when there is no usable polygon selection.
        This is what the "Polygon" study-area shape works over, so the user can
        clip OSM to their own outline rather than an inscribed rectangle shape.
        """
        layer = self.iface.activeLayer()
        get_selected = getattr(layer, "selectedFeatures", None)
        if get_selected is None:
            return None, None
        geoms = []
        try:
            for feat in get_selected():
                g = feat.geometry()
                if g is None or g.isEmpty() or not _is_polygon_geometry(g):
                    continue
                geoms.append(QgsGeometry(g))
        except Exception:
            return None, None
        if not geoms:
            return None, None
        if len(geoms) == 1:
            merged = geoms[0]
        else:
            merged = QgsGeometry.unaryUnion(geoms)
            if merged is None or merged.isEmpty():
                merged = geoms[0]
        return merged, layer.crs()

    def _resolve_study_area(self, p):
        """Return ``(area geometry in UTM, EPSG, area_km2)`` for the chosen shape.

        Geometric shapes (rectangle/rounded/circle/hexagon) are inscribed in the
        canvas/selection extent, capped to ``max_km2``. ``polygon`` instead uses the
        user's own selected polygon feature(s): reprojected to the area's UTM zone
        and cropped to the same area cap, so a huge selection can't flood Overpass.
        """
        shape = p.get("shape", SHAPE_RECTANGLE)
        max_km2 = p.get("max_km2", 6.0)
        if shape == SHAPE_POLYGON:
            geom, src_crs = self._selected_polygon_geom()
            if geom is None:
                raise ValueError(
                    "The Polygon shape needs a selected polygon. Select one or more "
                    "polygons in the active layer and run again, or pick another shape."
                )
            rect = geom.boundingBox()
            if rect.isEmpty():
                raise ValueError("The selected polygon has no extent.")
            urect, epsg, area_km2 = self._area_utm(rect, src_crs, max_km2)
            area_utm = QgsGeometry(geom)
            try:
                to_utm = QgsCoordinateTransform(
                    src_crs, QgsCoordinateReferenceSystem.fromEpsgId(epsg),
                    QgsProject.instance())
                area_utm.transform(to_utm)
            except Exception:
                raise ValueError("The selected polygon could not be placed on the map in metres.")
            cap = QgsGeometry.fromRect(urect)
            cropped = area_utm.intersection(cap)
            if cropped is not None and not cropped.isEmpty():
                area_utm = cropped
            with contextlib.suppress(Exception):
                if not area_utm.isGeosValid():
                    fixed = area_utm.makeValid()
                    if fixed is not None and not fixed.isEmpty():
                        area_utm = fixed
            return area_utm, epsg, area_km2

        rect, src_crs = self._area_rect_and_crs(p["area_source"])
        if rect.isEmpty():
            raise ValueError("The map is empty. Click “Add OSM basemap”, then zoom to your area.")
        urect, epsg, area_km2 = self._area_utm(rect, src_crs, max_km2)
        return shape_study_area(urect, shape), epsg, area_km2

    def _layer_specs(self, p):
        """(result key, wanted?, style function) in bottom-to-top draw order."""
        color_mode = p.get("building_color", styling.BUILDING_COLOR_FUNCTION)
        classification = p.get("classification", "continuous")
        theme = p.get("theme", "default")
        shade_var = p.get("shade_variation", True)
        shadow_2d = p.get("shadow_2d", True)
        mode_25d = p.get("mode_25d", False)
        return [
            ("greens", p["want_greens"], lambda layer: styling.style_greens(layer, theme=theme)),
            ("waterareas", p["want_water"], lambda layer: styling.style_waterareas(layer, theme=theme)),
            ("waterlines", p["want_water"], lambda layer: styling.style_water(layer, theme=theme)),
            ("bikelanes", p["want_roads"], styling.style_bikelanes),
            ("roads", p["want_roads"], lambda layer: styling.style_roads(layer, theme=theme)),
            ("trees", p["want_trees"], lambda layer: styling.style_trees(layer, theme=theme)),
            ("busstops", p["want_furniture"], lambda layer: styling.style_furniture(layer, "busstops", theme=theme)),
            ("benches", p["want_furniture"], lambda layer: styling.style_furniture(layer, "benches", theme=theme)),
            ("lights", p["want_furniture"], lambda layer: styling.style_furniture(layer, "lights", theme=theme)),
            ("trashbins", p["want_furniture"], lambda layer: styling.style_furniture(layer, "trashbins", theme=theme)),
            ("buildings", p["want_buildings"],
             lambda layer: styling.style_buildings(
                 layer, color_mode, classification=classification, theme=theme,
                 shade_variation=shade_var, shadow_2d=shadow_2d, mode_25d=mode_25d,
             )),
        ]

    def _ask_gpkg_path(self):
        """Ask where to save; return the path or None if the user cancelled.

        An existing GeoPackage is a valid choice: the layers are added to it,
        so the file dialog does not warn about replacing it.
        """
        options = _enum(QFileDialog, "Option", "DontConfirmOverwrite")
        path, _ = QFileDialog.getSaveFileName(
            self.iface.mainWindow(),
            "OSM Quick 3D — Save to GeoPackage (new layers are added to an existing file)",
            "osm_quick_3d.gpkg",
            "GeoPackage (*.gpkg)",
            "",
            options,
        )
        if not path:
            return None
        if not path.lower().endswith(".gpkg"):
            path += ".gpkg"
        return path

    @staticmethod
    def _building_totals(layer):
        """(footprint_m2, gfa_m2) summed over a buildings layer, or None on failure."""
        if layer is None:
            return None
        try:
            fields = layer.fields()
            if fields.indexFromName("footprint_m2") < 0:
                return None
            footprint = gfa = 0.0
            for feat in layer.getFeatures():
                fp = feat["footprint_m2"]
                gf = feat["gfa_m2"]
                if fp is not None:
                    footprint += float(fp)
                if gf is not None:
                    gfa += float(gf)
            return footprint, gfa
        except Exception:
            return None

    def _make_group(self, epsg):
        """A fresh layer-tree group at the top, to keep the legend tidy on big areas."""
        try:
            root = QgsProject.instance().layerTreeRoot()
            return root.insertGroup(0, f"OSM Quick 3D — EPSG:{epsg}")
        except Exception:
            return None

    def _setup_basemap_masking(self, basemap, group):
        if basemap is None or group is None:
            return

        # Move a clone of the existing layer-tree node; QGIS owns/deletes the
        # original node when it is removed, so reusing that node is unsafe.
        with contextlib.suppress(Exception):
            root = QgsProject.instance().layerTreeRoot()
            node = root.findLayer(basemap.id())
            if node is None:
                return
            if node.parent() is group:
                return
            with contextlib.suppress(Exception):
                basemap.setCustomProperty("osm_quick_3d/basemap_underlay", True)
            clone = node.clone()
            parent = node.parent() or root
            parent.removeChildNode(node)
            group.addChildNode(clone)

    def _add_base_layer(self, p, area_utm, epsg, group, gpkg):
        """Build, persist (optionally), style and 3D-extrude the ground base.

        The base goes to the bottom of the group so it underlies the city in 2D,
        and is extruded as a recessed slab in 3D. Returns the base layer or None.
        """
        try:
            base = build_base_layer(area_utm, epsg)
        except Exception:
            return None
        if gpkg is not None:
            loaded = gpkg.write(base, "base")
            if loaded is not None:
                base = loaded
        color_mode = p.get("building_color", styling.BUILDING_COLOR_FUNCTION)
        theme = p.get("theme", "default")
        transparent = p.get("basemap") is not None
        with contextlib.suppress(Exception):
            base.setCustomProperty("osm_quick_3d/transparent", transparent)
            base.setCustomProperty("osm_quick_3d/base_clip", True)
        with contextlib.suppress(Exception):
            bg_color_hex = self.iface.mapCanvas().canvasColor().name()
            styling.style_base(base, color_mode, transparent=transparent, bg_color_hex=bg_color_hex, theme=theme)
        project = QgsProject.instance()
        if group is not None:
            project.addMapLayer(base, False)
            group.addLayer(base)  # appended last == bottom of the group
        else:
            project.addMapLayer(base)

        if p.get("extrude_3d") and p.get("want_base"):
            try:
                base_3d = base.clone()
                base_3d.setName("OSM Base 3D")
                styling.style_base_3d_2d(base_3d)
                project.addMapLayer(base_3d, False)
                if group is not None:
                    # Insert base_3d at the bottom of the group (below base)
                    group.insertLayer(len(group.children()), base_3d)
                else:
                    project.addMapLayer(base_3d)
                native3d.apply_base_slab(
                    base_3d, depth=BASE_DEPTH_M, color_hex=styling.base_color_hex(color_mode, theme=theme))
            except Exception:
                # Fallback to main base layer if clone fails
                native3d.apply_base_slab(
                    base, depth=BASE_DEPTH_M, color_hex=styling.base_color_hex(color_mode, theme=theme))
        return base

    # ── main run ───────────────────────────────────────────────────────────
    def run_action(self, p):
        """Start a run: check the area here, download on a worker thread."""
        if self._unloaded or self._task is not None:
            return  # one run at a time
        if self.dock is not None and self.dock.is_running():
            return
        if not any(p.get(k) for k in ("want_buildings", "want_roads", "want_water",
                                      "want_greens", "want_trees", "want_furniture")):
            self._fail("Pick at least one kind of layer to download.")
            return
        try:
            area_utm, epsg, area_km2 = self._resolve_study_area(p)
        except ValueError as exc:
            self._fail(str(exc))
            return

        gpkg_path = None
        if p.get("save_gpkg"):
            gpkg_path = self._ask_gpkg_path()
            if gpkg_path is None:
                self._set_status("No file chosen, so nothing was downloaded.", kind="info")
                return
            try:
                GpkgTarget(gpkg_path).check()
            except ValueError as exc:
                self._fail(str(exc))
                return

        datasets = tuple(
            name for name, wanted in (
                ("buildings", p.get("want_buildings")),
                ("roads", p.get("want_roads")),
                ("water", p.get("want_water")),
                ("greens", p.get("want_greens")),
                ("trees", p.get("want_trees")),
                ("furniture", p.get("want_furniture")),
            ) if wanted
        )
        task = DownloadTask(area_utm, epsg, p.get("use_cache", True), datasets)
        task.callback = self._on_task_finished
        self._task = task
        self._pending = (p, area_utm, epsg, gpkg_path)
        self._run_started = time.monotonic()
        if self.dock is not None:
            self.dock.set_summary("")
            self.dock.set_running(True)
            task.statusChanged.connect(self.dock.show_progress, Qt.ConnectionType.QueuedConnection)
            self.dock.set_status(
                f"Starting the OpenStreetMap download for about {area_km2:.1f} km²…", busy=True)
        log(f"Run started: about {area_km2:.2f} km², EPSG:{epsg}, layers {', '.join(datasets)}.")
        QgsApplication.taskManager().addTask(task)

    def is_running(self) -> bool:
        return self._task is not None

    def _cancel_run(self):
        """Cancel button: stop the download; the dock is free again within 2 s."""
        task = self._task
        if task is None:
            return
        if task.result_data is not None:
            # The answer is already here: keep it rather than throw it away.
            self._set_status("The download had already finished; adding the layers…", busy=True)
            return
        task.cancel()
        QTimer.singleShot(_CANCEL_GRACE_MS, lambda: self._abandon_task(task))

    def _abandon_task(self, task):
        """A cancelled download still running after the grace period is let go."""
        if self._unloaded or task is not self._task or task.result_data is not None:
            return
        self._task = None
        self._pending = None
        self._retire(task)  # kept alive until it really ends, then forgotten
        if self.dock is not None:
            self.dock.set_running(False)
        self._set_status(CANCELLED_TEXT, kind="info")

    def _on_task_finished(self, task):
        """Continue on the main thread once the worker has finished."""
        if task in self._retired_tasks:
            self._retired_tasks.remove(task)
        if self._unloaded or task is not self._task:
            return
        self._task = None
        pending, self._pending = self._pending, None
        with contextlib.suppress(TypeError, RuntimeError):
            task.statusChanged.disconnect()
        result = task.result_data
        if result is None or pending is None:
            if self.dock is not None:
                self.dock.set_running(False)
            if task.cancelled:
                self._set_status(CANCELLED_TEXT, kind="info")
            else:
                self._fail(task.error_text or "The download stopped without an answer.", task.error_detail)
            return
        p, area_utm, epsg, gpkg_path = pending
        if self.dock is not None:
            self.dock.set_status("Adding the layers to the map…", busy=True)
        try:
            self._build_result(p, area_utm, epsg, result, gpkg_path)
        except Exception:
            self._fail("The layers could not be built.", traceback.format_exc())
        finally:
            if self.dock is not None:
                self.dock.set_running(False)

    def _build_result(self, p, area_utm, epsg, result, gpkg_path):
        """Style, save (optionally) and add the downloaded layers, on the main thread."""
        theme = p.get("theme", "default")
        color_mode = p.get("building_color", styling.BUILDING_COLOR_FUNCTION)
        classification = p.get("classification", "continuous")
        project = QgsProject.instance()

        wanted = [
            (key, style_fn) for key, want, style_fn in self._layer_specs(p)
            if want and result.get(key) is not None and result.get(key).featureCount() > 0
        ]
        notes = [str(n) for n in (result.get("notes") or []) if str(n).strip()]
        if not notes:
            download_info = result.get("download") or {}
            failed_tiles = tuple(download_info.get("tiles_failed") or ())
            if failed_tiles:
                notes.append(
                    f"{len(failed_tiles)} of {download_info.get('tiles_total', '?')} parts of the "
                    "area could not be downloaded; run again to fill the gap."
                )
        if not wanted:
            self._set_status(
                "OpenStreetMap has none of the chosen layers in this area. "
                "Try a larger area or other layers.", kind="warn")
            if notes and self.dock:
                self.dock.set_summary("Partial result: " + " ".join(notes))
            return

        gpkg = None
        if gpkg_path is not None:
            gpkg = GpkgTarget(gpkg_path)
            gpkg.choose_names([key for key, _ in wanted] + (["base"] if p.get("want_base") else []))

        group = self._make_group(epsg)
        canvas = self.iface.mapCanvas()
        canvas.freeze(True)
        added, buildings_layer, trees_layer = [], None, None
        styled_layers, furniture_layers, saved_layers = [], {}, []
        extruded = False
        try:
            t_data = styling.THEMES.get(theme, styling.THEMES["default"])
            canvas.setCanvasColor(QColor(t_data["bg"]))
            for key, style_fn in wanted:
                layer = result.get(key)
                count = layer.featureCount()
                if gpkg is not None:
                    loaded = gpkg.write(layer, key)
                    if loaded is not None:
                        layer = loaded
                        saved_layers.append(layer)
                with contextlib.suppress(Exception):
                    style_fn(layer)
                with contextlib.suppress(Exception):
                    layer.setCustomProperty("osm_quick_3d/theme", theme)
                    layer.setCustomProperty("osm_quick_3d/building_color", color_mode)
                    layer.setCustomProperty("osm_quick_3d/classification", classification)
                    layer.setCustomProperty("osm_quick_3d/height_scale", p.get("height_scale", 1.0))
                if group is not None:
                    # addToLegend=False, then insert at the top of our group so the
                    # spec's bottom-to-top order ends with buildings drawn on top.
                    project.addMapLayer(layer, False)
                    group.insertLayer(0, layer)
                else:
                    project.addMapLayer(layer)
                styled_layers.append(layer)
                added.append((key, count))
                if p.get("want_labels") and key in ("buildings", "roads"):
                    size = 8.0 if key == "buildings" else 7.5
                    with contextlib.suppress(Exception):
                        styling.label_by_name(layer, size=size)
                if key == "buildings":
                    buildings_layer = layer
                elif key == "trees":
                    trees_layer = layer
                elif key in ("busstops", "benches", "lights", "trashbins"):
                    furniture_layers[key] = layer

            if p["extrude_3d"] and buildings_layer is not None:
                extruded = native3d.apply_building_extrusion(
                    buildings_layer,
                    color_hex=styling.building_base_color(color_mode, theme=theme),
                    height_scale=p.get("height_scale", 1.0),
                    color_expr=styling.building_color_expression(
                        color_mode, classification=classification, layer=buildings_layer, theme=theme),
                    color_mode=color_mode,
                    classification=classification,
                    theme=theme,
                    shade_variation=p.get("shade_variation", True),
                )
            if p["extrude_3d"] and trees_layer is not None:
                native3d.apply_tree_3d(trees_layer, color_hex=t_data["trees"],
                                       height_scale=p.get("height_scale", 1.0))
            if p["extrude_3d"] and furniture_layers:
                for furn_role, furn_layer in furniture_layers.items():
                    native3d.apply_furniture_3d(furn_layer, role=furn_role)

            if p.get("want_base"):
                base_layer = self._add_base_layer(p, area_utm, epsg, group, gpkg)
                if base_layer is not None and gpkg is not None and base_layer.providerType() == "ogr":
                    saved_layers.append(base_layer)

            # 2D + 3D symbology into the GeoPackage's layer_styles table, so the
            # .gpkg opens with the full look in any project.
            for lyr in saved_layers:
                save_style_in_gpkg(lyr)

            if p["basemap"] is not None:
                self._setup_basemap_masking(p["basemap"], group)

            with contextlib.suppress(Exception):
                to_canvas = QgsCoordinateTransform(
                    QgsCoordinateReferenceSystem.fromEpsgId(epsg),
                    canvas.mapSettings().destinationCrs(), project,
                )
                canvas.setExtent(to_canvas.transformBoundingBox(area_utm.boundingBox()))
        finally:
            canvas.freeze(False)
            canvas.refresh()

        self._report(p, added, buildings_layer, gpkg, notes, extruded)
        if self.dock:
            with contextlib.suppress(Exception):
                self.dock.refresh_groups()
                self.dock.sync_from_run_params(p)
                self.dock.set_scene_clip_geometry(area_utm if p["basemap"] is not None else None)
        if p["open_3d"] and self.dock:
            opened = self.dock.open_managed_3d_view(auto=False, quiet=True)
            for delay in (600, 1500, 3000):
                QTimer.singleShot(delay, lambda: self.dock and self.dock._refresh_3d_view(auto=True))
            if not opened:
                self.iface.messageBar().pushInfo(
                    TITLE, "The 3D view could not be opened in this QGIS; the layers are set up "
                           "for 3D, so View ▸ New 3D Map View shows them.")
        if p["extrude_3d"] and buildings_layer is not None and not extruded:
            self.iface.messageBar().pushInfo(TITLE, "This QGIS has no 3D support; the layers were added in 2D.")

    def _report(self, p, added, buildings_layer, gpkg, notes, extruded):
        """The summary (kept in the dock) and a one-line status."""
        seconds = int(time.monotonic() - self._run_started)
        counts = ", ".join(f"{n:,} {_PLAIN_NAMES.get(key, key)}" for key, n in reversed(added))
        parts = [counts + "."]
        totals = self._building_totals(buildings_layer)
        if totals is not None and totals[0] > 0:
            footprint, gfa = totals
            parts.append(f"Building footprint ≈ {footprint:,.0f} m²; "
                         f"estimated floor area ≈ {gfa:,.0f} m².")
        problems = []
        if notes:
            parts.append("Partial result: " + " ".join(notes))
            problems.append("part of the area is missing")
        if gpkg is None:
            parts.append(TEMPORARY_NOTE)
        else:
            name = os.path.basename(gpkg.path)
            if gpkg.written:
                parts.append(f"Saved in {name} as {', '.join(gpkg.written)}.")
            if gpkg.failed:
                failed = ", ".join(_PLAIN_NAMES.get(k, k) for k in gpkg.failed)
                parts.append(
                    f"Could not save {failed} to {name} ({short_text(gpkg.first_error, 120)}). "
                    "Those layers are temporary: they vanish when the project is reopened."
                )
                problems.append("saving to the GeoPackage failed")
        summary = " ".join(parts)
        if self.dock:
            self.dock.set_summary(summary)
        log(f"Run finished in {seconds} s: {summary}")
        if problems:
            self._set_status(f"Done in {seconds} s, but {' and '.join(problems)}.", kind="warn")
            self.iface.messageBar().pushWarning(TITLE, short_text(summary, 300))
        else:
            self._set_status(f"Done in {seconds} s.", kind="ok")
            self.iface.messageBar().pushSuccess(TITLE, short_text(summary, 300))
