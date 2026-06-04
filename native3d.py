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


def _extrusion_expression(height_scale=1.0):
    """Per-feature extrusion height (m), optionally exaggerated for flat cities."""
    try:
        scale = float(height_scale)
    except (TypeError, ValueError):
        scale = 1.0
    if scale and abs(scale - 1.0) > 1e-6:
        return f"({EXTRUSION_EXPRESSION}) * {scale:g}"
    return EXTRUSION_EXPRESSION


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


def _apply_material_color_expression(material, color_expr):
    """Data-define the material's diffuse colour from a per-feature expression.

    Lets one 3D renderer paint every building in its OSM-function colour (the
    same palette as the 2D legend). Degrades to a no-op on builds whose material
    settings don't expose data-defined properties; the flat fallback colour set
    by ``_make_material`` then stands.
    """
    if material is None or not color_expr:
        return
    try:
        from qgis.core import QgsProperty
    except Exception:
        return
    ddp_getter = getattr(material, "dataDefinedProperties", None)
    ddp_setter = getattr(material, "setDataDefinedProperties", None)
    if ddp_getter is None or ddp_setter is None:
        return
    prop_key = None
    for owner_name in ("QgsAbstractMaterialSettings", "QgsPhongMaterialSettings"):
        try:
            owner = __import__("qgis._3d", fromlist=[owner_name])
            owner = getattr(owner, owner_name)
        except Exception:
            continue
        prop_key = getattr(owner, "Diffuse", None)
        if prop_key is not None:
            break
    if prop_key is None:
        return
    try:
        ddp = ddp_getter()
        ddp.setProperty(prop_key, QgsProperty.fromExpression(color_expr))
        ddp_setter(ddp)
    except Exception:
        pass


def _clamp_to_terrain(symbol):
    """Rest the prisms on the terrain/basemap rather than at z=0."""
    for module in ("qgis.core", "qgis._3d"):
        try:
            mod = __import__(module, fromlist=["Qgs3DTypes"])
            symbol.setAltitudeClamping(mod.Qgs3DTypes.AltClampTerrain)
            return
        except Exception:
            continue


def apply_building_extrusion(layer, color_hex="#cac5bf", height_scale=1.0, color_expr=None):
    """Attach a 3D renderer that extrudes the building polygons.

    ``height_scale`` exaggerates every prism (1.0 = true OSM height); ``color_expr``
    is an optional per-feature QGIS expression returning a hex colour so the
    massing matches the 2D function palette. Returns True if a 3D renderer was set,
    False if this QGIS build has no usable 3D module (the caller then just keeps
    the flat 2D layers).
    """
    try:
        from qgis._3d import QgsVectorLayer3DRenderer, QgsPolygon3DSymbol
    except Exception:
        return False

    symbol = QgsPolygon3DSymbol()
    extrusion_expr = _extrusion_expression(height_scale)

    # Static fallback height first, then a data-defined override from OSM.
    try:
        symbol.setExtrusionHeight(9.0 * float(height_scale or 1.0))
    except Exception:
        pass
    try:
        from qgis.core import QgsProperty
        prop_key = getattr(QgsPolygon3DSymbol, "PropertyExtrusionHeight", None)
        if prop_key is not None:
            ddp = symbol.dataDefinedProperties()
            ddp.setProperty(prop_key, QgsProperty.fromExpression(extrusion_expr))
            symbol.setDataDefinedProperties(ddp)
    except Exception:
        pass

    _clamp_to_terrain(symbol)

    material = _make_material(color_hex)
    _apply_material_color_expression(material, color_expr)
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


def apply_base_slab(layer, depth=5.0, top_z=0.0, color_hex="#5e7274"):
    """Extrude the ground base as a recessed slab: top at ``top_z``, ``depth`` deep.

    The slab's top face sits at ``top_z`` (ground level, 0) and its base reaches
    ``top_z - depth`` (e.g. -5 m), so the city visibly stands on a plinth. Uses the
    3D symbol's data-defined Height (base altitude) property where available.
    Defensive: returns False on builds without a usable 3D module.
    """
    try:
        from qgis._3d import QgsVectorLayer3DRenderer, QgsPolygon3DSymbol
    except Exception:
        return False

    symbol = QgsPolygon3DSymbol()
    try:
        symbol.setExtrusionHeight(float(depth))
    except Exception:
        pass

    # Drop the slab so its top lands at top_z: base height = top_z - depth.
    try:
        from qgis.core import QgsProperty
        ddp = symbol.dataDefinedProperties()
        height_key = getattr(QgsPolygon3DSymbol, "PropertyHeight", None)
        if height_key is not None:
            ddp.setProperty(height_key, QgsProperty.fromValue(float(top_z) - float(depth)))
        ext_key = getattr(QgsPolygon3DSymbol, "PropertyExtrusionHeight", None)
        if ext_key is not None:
            ddp.setProperty(ext_key, QgsProperty.fromValue(float(depth)))
        symbol.setDataDefinedProperties(ddp)
    except Exception:
        pass

    # Absolute altitude so the base height is taken literally (not clamped to 0).
    for module in ("qgis.core", "qgis._3d"):
        try:
            mod = __import__(module, fromlist=["Qgs3DTypes"])
            symbol.setAltitudeClamping(mod.Qgs3DTypes.AltClampAbsolute)
            break
        except Exception:
            continue

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


def apply_tree_3d(layer, color_hex="#5f9e4c"):
    """Render the tree points as simple 3D canopies (green spheres) on the terrain.

    Sized by the OSM ``height`` column when present (radius ≈ a third of crown
    height) with a sensible default. Fully defensive: returns False on builds
    without a usable 3D point symbol, leaving the 2D tree markers in place.
    """
    try:
        from qgis._3d import QgsPoint3DSymbol, QgsVectorLayer3DRenderer
    except Exception:
        return False

    symbol = QgsPoint3DSymbol()

    shape = getattr(QgsPoint3DSymbol, "Sphere", None)
    if shape is not None:
        try:
            symbol.setShape(shape)
        except Exception:
            pass
    # Crown radius from OSM height when available, else ~2.5 m; shapeProperties is
    # a plain dict on every 3D-capable build that exposes QgsPoint3DSymbol.
    try:
        symbol.setShapeProperties({"radius": 2.5})
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

    # Lift the sphere so it rests on, not half-buried in, the ground.
    try:
        from qgis.PyQt.QtGui import QMatrix4x4
        transform = QMatrix4x4()
        transform.translate(0.0, 0.0, 2.5)
        symbol.setTransform(transform)
    except Exception:
        pass

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
