# -*- coding: utf-8 -*-
"""Native QGIS 3D for the building massing — no browser, no Three.js.

Buildings are extruded with QGIS's own 3D symbology (``QgsPolygon3DSymbol``) so
they show in a QGIS 3D Map View. Flat roofs, no animation: a clean massing model.
Extrusion height comes straight from OSM —
``coalesce("height", "building_levels" * 3, 9)`` metres — so tagged heights win,
floor counts are the fallback, and 9 m (≈3 floors) is the last resort.

The "basemap underlay" needs no special code here: a QGIS 3D Map View drapes the
2D project map over its (flat) terrain, so whatever basemap sits at the bottom of
the layer tree becomes the ground the massing stands on.

Everything is wrapped defensively: the 3D API moved across QGIS releases and the
3D module is an optional build, so each step degrades to a no-op and the caller
falls back to a "open a 3D Map View yourself" hint.
"""
from __future__ import annotations

EXTRUSION_EXPRESSION = 'coalesce("height", "building_levels" * 3, 9)'


def _make_material(color_hex):
    try:
        from qgis._3d import QgsPhongMaterialSettings
    except Exception:
        return None
    from qgis.PyQt.QtGui import QColor
    mat = QgsPhongMaterialSettings()
    col = QColor(color_hex)
    try:
        mat.setDiffuse(col)
        mat.setAmbient(col.darker(160))
        mat.setSpecular(QColor("#101010"))
    except Exception:
        pass
    return mat


def _clamp_to_terrain(symbol):
    """Rest the prisms on the terrain/basemap rather than at z=0."""
    for module in ("qgis.core", "qgis._3d"):
        try:
            mod = __import__(module, fromlist=["Qgs3DTypes"])
            symbol.setAltitudeClamping(mod.Qgs3DTypes.AltClampTerrain)
            return
        except Exception:
            continue


def apply_building_extrusion(layer, color_hex="#cac5bf"):
    """Attach a 3D renderer that extrudes the building polygons.

    Returns True if a 3D renderer was set, False if this QGIS build has no usable
    3D module (the caller then just keeps the flat 2D layers).
    """
    try:
        from qgis._3d import QgsVectorLayer3DRenderer, QgsPolygon3DSymbol
    except Exception:
        return False

    symbol = QgsPolygon3DSymbol()

    # Static fallback height first, then a data-defined override from OSM.
    try:
        symbol.setExtrusionHeight(9.0)
    except Exception:
        pass
    try:
        from qgis.core import QgsProperty
        prop_key = getattr(QgsPolygon3DSymbol, "PropertyExtrusionHeight", None)
        if prop_key is not None:
            ddp = symbol.dataDefinedProperties()
            ddp.setProperty(prop_key, QgsProperty.fromExpression(EXTRUSION_EXPRESSION))
            symbol.setDataDefinedProperties(ddp)
    except Exception:
        pass

    _clamp_to_terrain(symbol)

    material = _make_material(color_hex)
    if material is not None:
        for setter in ("setMaterialSettings", "setMaterial"):
            fn = getattr(symbol, setter, None)
            if fn is None:
                continue
            try:
                fn(material)
                break
            except Exception:
                continue

    try:
        layer.setRenderer3D(QgsVectorLayer3DRenderer(symbol))
        return True
    except Exception:
        return False


def open_3d_view(iface):
    """Best-effort: open a QGIS 3D Map View by triggering the built-in action.

    There is no clean public API to spawn a 3D canvas, so we trigger the same
    menu action the user would (View ▸ New 3D Map View). Returns True if an
    action was found and triggered, False otherwise (caller shows a hint).
    """
    try:
        from qgis.PyQt.QtWidgets import QAction
    except Exception:
        return False
    try:
        win = iface.mainWindow()
    except Exception:
        return False

    for obj_name in ("mActionNew3DMapView", "mActionNew3DMapCanvas", "mActionNew3DMap"):
        act = win.findChild(QAction, obj_name)
        if act is not None:
            try:
                act.trigger()
                return True
            except Exception:
                pass

    # Fall back to matching the action by its (localized) text.
    try:
        for act in win.findChildren(QAction):
            text = (act.text() or "").lower()
            if "3d" in text and ("map" in text or "harita" in text):
                act.trigger()
                return True
    except Exception:
        pass
    return False
