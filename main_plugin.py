# -*- coding: utf-8 -*-
"""OSM Quick 3D — main plugin class.

One toolbar button opens a dialog; on run it downloads OpenStreetMap for the
chosen area, adds the layers to QGIS already styled by function, extrudes the
buildings with native 3D symbology, and (optionally) opens a 3D Map View. No
web server, no browser — built to scale to larger areas than the Three.js
companion plugin (osm_3d_model).
"""
from __future__ import annotations

import os

from qgis.PyQt.QtCore import Qt
from qgis.PyQt.QtGui import QIcon
from qgis.PyQt.QtWidgets import QAction, QApplication, QFileDialog, QMessageBox
from qgis.core import (
    QgsCoordinateReferenceSystem,
    QgsCoordinateTransform,
    QgsGeometry,
    QgsProject,
    QgsRectangle,
    QgsVectorLayer,
)

from . import native3d, styling
from .osm_download import (
    OsmDownloadError,
    download_osm_for_area,
    utm_epsg_for,
    write_layer_to_gpkg,
)


class OsmQuick3DPlugin:
    MENU_NAME = "&OSM Quick 3D"

    def __init__(self, iface):
        self.iface = iface
        self.plugin_dir = os.path.dirname(__file__)
        self.icon_path = os.path.join(self.plugin_dir, "icons", "icon.png")
        self.action = None
        self.dialog = None

    def initGui(self):
        self.action = QAction(QIcon(self.icon_path), "OSM Quick 3D", self.iface.mainWindow())
        self.action.setStatusTip("OpenStreetMap'i indir, QGIS'te native 3D olarak aç")
        self.action.triggered.connect(self.show_dialog)
        self.iface.addToolBarIcon(self.action)
        self.iface.addPluginToMenu(self.MENU_NAME, self.action)

    def unload(self):
        if self.action:
            self.iface.removePluginMenu(self.MENU_NAME, self.action)
            self.iface.removeToolBarIcon(self.action)
            self.action = None
        if self.dialog:
            self.dialog.close()
            self.dialog = None

    def show_dialog(self):
        if self.dialog is None:
            from .dialog import PluginDialog

            self.dialog = PluginDialog(self.iface, self.iface.mainWindow())
            self.dialog.runRequested.connect(self.run_action)
        self.dialog.show()
        self.dialog.raise_()
        self.dialog.activateWindow()

    # ── helpers ────────────────────────────────────────────────────────────
    def _set_status(self, text, error=False):
        if self.dialog:
            self.dialog.set_status(text, error=error)
        QApplication.processEvents()

    def _area_rect_and_crs(self, area_source):
        """Return (QgsRectangle, source CRS) for the chosen area source."""
        canvas = self.iface.mapCanvas()
        if area_source == "selection":
            layer = self.iface.activeLayer()
            if (layer is None or not hasattr(layer, "selectedFeatureCount")
                    or layer.selectedFeatureCount() == 0):
                raise ValueError(
                    "Seçili obje yok. Aktif vektör katmanda obje seçin "
                    "ya da 'Görünür harita alanı'nı kullanın."
                )
            return layer.boundingBoxOfSelected(), layer.crs()
        return canvas.extent(), canvas.mapSettings().destinationCrs()

    def _area_utm(self, rect, src_crs, max_km2):
        """Reproject the area rectangle to its UTM zone, clamped to max_km2."""
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
        return QgsGeometry.fromRect(urect), epsg, area_km2

    def _layer_specs(self, p):
        """(result key, wanted?, style function) in bottom-to-top draw order."""
        def points(color_hex, size):
            return lambda layer: styling.style_points(layer, color_hex, size)

        return [
            ("greens", p["want_greens"], styling.style_greens),
            ("waterlines", p["want_water"], styling.style_water),
            ("bikelanes", p["want_roads"], styling.style_bikelanes),
            ("roads", p["want_roads"], styling.style_roads),
            ("trees", p["want_trees"], styling.style_trees),
            ("busstops", p["want_furniture"], points("#c7976a", 2.0)),
            ("benches", p["want_furniture"], points("#a98c6a", 1.6)),
            ("lights", p["want_furniture"], points("#d8c98a", 1.6)),
            ("trashbins", p["want_furniture"], points("#8a9a8a", 1.4)),
            ("buildings", p["want_buildings"], styling.style_buildings),
        ]

    def _ask_gpkg_path(self):
        """Prompt for a GeoPackage save path; return it or None if cancelled."""
        path, _ = QFileDialog.getSaveFileName(
            self.iface.mainWindow(),
            "OSM Quick 3D — GeoPackage olarak kaydet",
            "osm_quick_3d.gpkg",
            "GeoPackage (*.gpkg)",
        )
        if not path:
            return None
        if not path.lower().endswith(".gpkg"):
            path += ".gpkg"
        return path

    def _persist_to_gpkg(self, layer, key, gpkg_path, first):
        """Write ``layer`` into the GeoPackage and return the reloaded OGR layer.

        Returns (loaded_layer, error_str). On any failure the caller keeps the
        original in-memory layer so the run still succeeds, just not persisted.
        """
        err = write_layer_to_gpkg(layer, gpkg_path, key, first)
        if err:
            return None, err
        loaded = QgsVectorLayer(f"{gpkg_path}|layername={key}", layer.name(), "ogr")
        if not loaded.isValid():
            return None, "reload failed"
        return loaded, None

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

    def _move_basemap_bottom(self, basemap):
        try:
            root = QgsProject.instance().layerTreeRoot()
            node = root.findLayer(basemap.id())
            if node is None:
                return
            parent = node.parent() or root
            root.insertChildNode(-1, node.clone())
            parent.removeChildNode(node)
        except Exception:
            pass

    # ── main run ───────────────────────────────────────────────────────────
    def run_action(self, p):
        if not any(p[k] for k in ("want_buildings", "want_roads", "want_water",
                                  "want_greens", "want_trees", "want_furniture")):
            self._error("Katman seçilmedi", "En az bir katman türü seçin.")
            return

        try:
            rect, src_crs = self._area_rect_and_crs(p["area_source"])
            if rect.isEmpty():
                raise ValueError("Alan boş. Haritada bir yere yakınlaşın ya da obje seçin.")
            area_utm, epsg, area_km2 = self._area_utm(rect, src_crs, p["max_km2"])
        except ValueError as exc:
            self._error("Alan hatası", str(exc))
            self._set_status(str(exc), error=True)
            return

        self._set_status(f"İndiriliyor… (~{area_km2:.1f} km², EPSG:{epsg})")
        QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
        try:
            result = download_osm_for_area(
                area_utm, epsg, feedback=self._set_status,
                use_cache=p.get("use_cache", True),
            )
        except OsmDownloadError as exc:
            self._error("OSM indirme hatası", str(exc))
            self._set_status(str(exc), error=True)
            return
        except Exception as exc:
            self._error("Beklenmeyen hata", str(exc))
            self._set_status(str(exc), error=True)
            return
        finally:
            QApplication.restoreOverrideCursor()

        gpkg_path = None
        if p.get("save_gpkg"):
            gpkg_path = self._ask_gpkg_path()
            if gpkg_path is None:
                self._set_status("GeoPackage kaydı iptal edildi; bellek katmanları kullanılıyor.")

        project = QgsProject.instance()
        group = self._make_group(epsg)
        added, total, buildings_layer, trees_layer = [], 0, None, None
        gpkg_first, gpkg_failed = True, False
        for key, wanted, style_fn in self._layer_specs(p):
            if not wanted:
                continue
            layer = result.get(key)
            if layer is None or layer.featureCount() == 0:
                continue
            # Persist to GeoPackage first (write the raw memory layer, then reload
            # and style the durable copy), so closing the project doesn't lose it.
            if gpkg_path is not None:
                loaded, err = self._persist_to_gpkg(layer, key, gpkg_path, gpkg_first)
                if loaded is not None:
                    layer = loaded
                    gpkg_first = False
                else:
                    gpkg_failed = True
            try:
                style_fn(layer)
            except Exception:
                pass
            if group is not None:
                # addToLegend=False, then insert at the top of our group so the
                # spec's bottom-to-top order ends with buildings drawn on top.
                project.addMapLayer(layer, False)
                group.insertLayer(0, layer)
            else:
                project.addMapLayer(layer)
            total += layer.featureCount()
            added.append(f"{layer.featureCount()} {key}")
            if key == "buildings":
                buildings_layer = layer
            elif key == "trees":
                trees_layer = layer

        if not added:
            self._error("Sonuç yok", "Bu alanda seçilen katmanlarda OSM objesi bulunamadı.")
            self._set_status("0 obje.", error=True)
            if group is not None:
                try:
                    QgsProject.instance().layerTreeRoot().removeChildNode(group)
                except Exception:
                    pass
            return

        extruded = False
        if p["extrude_3d"] and buildings_layer is not None:
            extruded = native3d.apply_building_extrusion(
                buildings_layer,
                height_scale=p.get("height_scale", 1.0),
                color_expr=styling.building_color_expression(),
            )
        # Tree points get a matching 3D pass (green canopies) when 3D is on.
        if p["extrude_3d"] and trees_layer is not None:
            native3d.apply_tree_3d(trees_layer)

        if p["basemap"] is not None:
            self._move_basemap_bottom(p["basemap"])

        try:
            canvas = self.iface.mapCanvas()
            to_canvas = QgsCoordinateTransform(
                QgsCoordinateReferenceSystem.fromEpsgId(epsg),
                canvas.mapSettings().destinationCrs(), project,
            )
            canvas.setExtent(to_canvas.transformBoundingBox(area_utm.boundingBox()))
            canvas.refresh()
        except Exception:
            pass

        opened_3d = native3d.open_3d_view(self.iface) if p["open_3d"] else False

        summary = f"{total} obje eklendi: " + ", ".join(added) + f" (EPSG:{epsg})."
        totals = self._building_totals(buildings_layer)
        if totals is not None and totals[0] > 0:
            footprint, gfa = totals
            summary += (
                f" Bina taban alanı ≈ {footprint:,.0f} m², "
                f"tahmini brüt kat alanı ≈ {gfa:,.0f} m²."
            )
        if gpkg_path is not None and not gpkg_failed:
            summary += f" GeoPackage: {gpkg_path}"
        self.iface.messageBar().pushSuccess("OSM Quick 3D", summary)
        self._set_status(summary)
        if gpkg_path is not None and gpkg_failed:
            self.iface.messageBar().pushWarning(
                "OSM Quick 3D",
                "Bazı katmanlar GeoPackage'a yazılamadı; o katmanlar bellek katmanı olarak "
                "eklendi (proje kapanınca kaybolur).",
            )
        if p["extrude_3d"] and not extruded:
            self.iface.messageBar().pushInfo(
                "OSM Quick 3D",
                "Bu QGIS derlemesinde 3D modülü bulunamadı; katmanlar 2D olarak eklendi.",
            )
        if p["open_3d"] and not opened_3d:
            self.iface.messageBar().pushInfo(
                "OSM Quick 3D",
                "3D görünümü otomatik açılamadı — Görünüm ▸ Yeni 3D Harita Görünümü'nü "
                "açın (binalar zaten ekstrüde).",
            )

    def _error(self, title, text):
        QMessageBox.critical(self.iface.mainWindow(), title, text)
