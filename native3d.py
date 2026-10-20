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

import contextlib
import math

EXTRUSION_EXPRESSION = 'coalesce(to_real("height"), to_int("building_levels") * 3, 9)'
BASE_HEIGHT_EXPRESSION = 'coalesce(to_real("min_height"), 0)'
PRISM_HEIGHT_EXPRESSION = (
    'max(1.5, coalesce(to_real("height"), to_int("building_levels") * 3, 9)'
    ' - coalesce(to_real("min_height"), 0))'
)
MANAGED_3D_CANVAS_NAME = "OSM Quick 3D Scene"


def _extrusion_expression(height_scale=1.0):
    """Per-feature extrusion height (m), optionally exaggerated for flat cities."""
    try:
        scale = float(height_scale)
    except (TypeError, ValueError):
        scale = 1.0
    if scale and abs(scale - 1.0) > 1e-6:
        return f"({EXTRUSION_EXPRESSION}) * {scale:g}"
    return EXTRUSION_EXPRESSION


def _base_height_expression(height_scale=1.0):
    """Per-feature base elevation offset (m above terrain) for ``min_height`` parts."""
    try:
        scale = float(height_scale)
    except (TypeError, ValueError):
        scale = 1.0
    if scale and abs(scale - 1.0) > 1e-6:
        return f"({BASE_HEIGHT_EXPRESSION}) * {scale:g}"
    return BASE_HEIGHT_EXPRESSION


def _prism_extrusion_expression(height_scale=1.0):
    """Net prism height (total height minus min_height), scaled by ``height_scale``."""
    try:
        scale = float(height_scale)
    except (TypeError, ValueError):
        scale = 1.0
    if scale and abs(scale - 1.0) > 1e-6:
        return f"({PRISM_HEIGHT_EXPRESSION}) * {scale:g}"
    return PRISM_HEIGHT_EXPRESSION


def _make_material(color_hex):
    try:
        from qgis._3d import QgsPhongMaterialSettings
    except Exception:
        return None
    from qgis.PyQt.QtGui import QColor
    mat = QgsPhongMaterialSettings()
    col = QColor(color_hex)
    with contextlib.suppress(Exception):
        mat.setDiffuse(col)
        mat.setAmbient(col.darker(160))
        mat.setSpecular(QColor("#101010"))
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
        owner = None
        with contextlib.suppress(Exception):
            mod = __import__("qgis._3d", fromlist=[owner_name])
            owner = getattr(mod, owner_name)
        if owner is None:
            continue
        prop_key = getattr(owner, "Diffuse", None)
        if prop_key is not None:
            break
    if prop_key is None:
        return
    with contextlib.suppress(Exception):
        ddp = ddp_getter()
        ddp.setProperty(prop_key, QgsProperty.fromExpression(color_expr))
        ddp_setter(ddp)


def _clamp_to_terrain(symbol):
    """Rest the prisms on the terrain/basemap rather than at z=0."""
    for module in ("qgis.core", "qgis._3d"):
        with contextlib.suppress(Exception):
            mod = __import__(module, fromlist=["Qgs3DTypes"])
            symbol.setAltitudeClamping(mod.Qgs3DTypes.AltClampTerrain)
            return


def _refresh_3d_layer(layer):
    """Force QGIS 3D views to notice a renderer change."""
    for method in ("trigger3DUpdate", "request3DUpdate", "triggerRepaint"):
        fn = getattr(layer, method, None)
        if fn is None:
            continue
        with contextlib.suppress(Exception):
            fn()


def _bind_renderer_to_layer(renderer, layer):
    """QGIS LTR can leave Python-created 3D renderers detached unless forced."""
    with contextlib.suppress(Exception):
        renderer.setLayer(layer)
    return renderer


def _apply_symbol_extrusion_properties(symbol, height_scale=1.0):
    with contextlib.suppress(Exception):
        symbol.setExtrusionHeight(9.0 * float(height_scale or 1.0))
    with contextlib.suppress(Exception):
        symbol.setAddBackFaces(True)
    with contextlib.suppress(Exception):
        symbol.setRenderedFacade(3)
    with contextlib.suppress(Exception):
        symbol.setEdgesEnabled(True)
        from qgis.PyQt.QtGui import QColor
        symbol.setEdgeColor(QColor("#2b3436"))
        symbol.setEdgeWidth(0.15)
    with contextlib.suppress(Exception):
        from qgis.core import QgsProperty
        from qgis._3d import QgsPolygon3DSymbol
        ddp = symbol.dataDefinedProperties()
        prop_ext = getattr(QgsPolygon3DSymbol, "PropertyExtrusionHeight", None)
        if prop_ext is not None:
            ddp.setProperty(prop_ext, QgsProperty.fromExpression(_prism_extrusion_expression(height_scale)))
        prop_base = getattr(QgsPolygon3DSymbol, "PropertyHeight", None)
        if prop_base is not None:
            ddp.setProperty(prop_base, QgsProperty.fromExpression(_base_height_expression(height_scale)))
        symbol.setDataDefinedProperties(ddp)
    _clamp_to_terrain(symbol)


def _apply_single_building_extrusion(layer, color_hex="#cac5bf", height_scale=1.0, color_expr=None):
    """Visibility-first 3D renderer used as a fallback for QGIS 3D rule quirks."""
    try:
        from qgis._3d import QgsVectorLayer3DRenderer, QgsPolygon3DSymbol
    except Exception:
        return False

    symbol = QgsPolygon3DSymbol()
    _apply_symbol_extrusion_properties(symbol, height_scale)
    material = _make_material(color_hex)
    _apply_material_color_expression(material, color_expr)
    if material is not None:
        try:
            symbol.setMaterialSettings(material)
        except Exception:
            with contextlib.suppress(Exception):
                symbol.setMaterial(material)
    try:
        renderer = _bind_renderer_to_layer(QgsVectorLayer3DRenderer(symbol), layer)
        layer.setRenderer3D(renderer)
        _refresh_3d_layer(layer)
        return True
    except Exception:
        return False


def apply_building_extrusion(layer, color_hex="#cac5bf", height_scale=1.0, color_expr=None,
                             color_mode="function", classification="continuous", theme="default",
                             shade_variation: bool = True):
    """Attach a 3D renderer that extrudes the building polygons.

    Uses QgsRuleBased3DRenderer to support robust, separate colors by function/height in QGIS LTR.
    """
    try:
        from qgis._3d import QgsRuleBased3DRenderer, QgsPolygon3DSymbol
    except Exception:
        return False

    try:
        from . import styling
    except ImportError:
        import styling

    def make_rule(symbol=None, filter_expr="", description="", is_else=False):
        """Create a rule across the slightly different PyQGIS 3D signatures."""
        attempts = (
            (symbol, filter_expr, description),
            (symbol, filter_expr),
            (symbol,),
            tuple(),
        )
        rule = None
        for args in attempts:
            with contextlib.suppress(Exception):
                rule = QgsRuleBased3DRenderer.Rule(*args)
                break
        if rule is None:
            return None
        if symbol is not None:
            for setter in ("setSymbol", "setSymbol3D"):
                fn = getattr(rule, setter, None)
                if fn is None:
                    continue
                with contextlib.suppress(Exception):
                    fn(symbol)
                    break
        if filter_expr:
            for setter in ("setFilterExpression", "setFilter", "setFilterExpressionString"):
                fn = getattr(rule, setter, None)
                if fn is None:
                    continue
                with contextlib.suppress(Exception):
                    fn(filter_expr)
                    break
        if description:
            with contextlib.suppress(Exception):
                rule.setDescription(description)
        if is_else:
            with contextlib.suppress(Exception):
                rule.setIsElse(True)
        return rule

    def append_rule(rule):
        if rule is None:
            return
        with contextlib.suppress(Exception):
            root_rule.appendChild(rule)

    # 1. Create root rule
    root_rule = make_rule(None)
    if root_rule is None:
        return False

    # 2. Get theme configuration
    t = styling.THEMES.get(theme, styling.THEMES["default"])

    # helper to build a symbol with a specific color and height settings
    def make_building_symbol(color):
        sym = QgsPolygon3DSymbol()
        _apply_symbol_extrusion_properties(sym, height_scale)
        mat = _make_material(color)
        if mat is not None:
            for setter in ("setMaterialSettings", "setMaterial"):
                fn = getattr(sym, setter, None)
                if fn is not None:
                    with contextlib.suppress(Exception):
                        fn(mat)
                        break
        return sym

    if color_mode == "function":
        theme_building_colors = t.get("building_colors", styling.BUILDING_COLORS)
        categories = (
            ("Residential", theme_building_colors["residential"],
             "lower(\"building\") IN ('apartments','residential','house','detached','terrace','dormitory','bungalow','semidetached_house','hut')"),
            ("Commercial", theme_building_colors["commercial"],
             "lower(\"building\") IN ('commercial','retail','office','supermarket','kiosk','hotel')"),
            ("Industrial", theme_building_colors["industrial"],
             "lower(\"building\") IN ('industrial','warehouse','manufacture','hangar','factory')"),
            ("Civic", theme_building_colors["civic"],
             "lower(\"building\") IN ('school','university','college','kindergarten','hospital','clinic','public','civic','government','townhall')"),
            ("Worship", theme_building_colors["worship"],
             "lower(\"building\") IN ('church','mosque','temple','synagogue','cathedral','chapel')"),
        )
        bucket_expr = 'coalesce(to_int(right("osm_id", 1)), 0) % 3'
        for label, base_col, filt in categories:
            if shade_variation:
                shades = (
                    (0, base_col, f"{label}"),
                    (1, styling._scale_hex(base_col, 0.89), f"{label} (deep)"),
                    (2, styling._scale_hex(base_col, 1.09), f"{label} (light)"),
                )
                for rem, shade_col, desc in shades:
                    sym = make_building_symbol(shade_col)
                    append_rule(make_rule(sym, f"({filt}) AND ({bucket_expr} = {rem})", desc))
            else:
                sym = make_building_symbol(base_col)
                append_rule(make_rule(sym, filt, label))

        oth_col = theme_building_colors["other"]
        oth_sym = make_building_symbol(oth_col)
        append_rule(make_rule(oth_sym, "", "Other", is_else=True))

    else:
        # Ramp modes: height, soft_gray, soft_warm, teal, soft_salmon, soft_purple, soft_sand
        if color_mode == "height":
            lo_hex, hi_hex = t["building_ramp"]
        else:
            lo_hex, hi_hex = styling._BUILDING_RAMPS.get(color_mode, t["building_ramp"])

        # Get breaks and colors
        min_h, max_h, breaks, colors = styling.get_breaks_and_colors(layer, lo_hex, hi_hex, classification)
        h_expr = EXTRUSION_EXPRESSION

        if classification == "continuous":
            # For continuous, create 10 discrete steps to simulate a gradient
            steps = 10
            step_val = (max_h - min_h) / steps
            for i in range(steps):
                f_min = min_h + i * step_val
                f_max = min_h + (i + 1) * step_val
                factor = (i + 0.5) / steps
                col = styling._interpolate_color(lo_hex, hi_hex, factor)
                sym = make_building_symbol(col)

                if i == 0:
                    filter_str = f"({h_expr}) <= {f_max:.4f}"
                elif i == steps - 1:
                    filter_str = f"({h_expr}) > {f_min:.4f}"
                else:
                    filter_str = f"({h_expr}) > {f_min:.4f} AND ({h_expr}) <= {f_max:.4f}"

                append_rule(make_rule(sym, filter_str, f"Height Step {i+1}"))
        else:
            # Discrete/Quantile: 5 classes separated by 4 breaks: breaks[0..3]
            # colors[0..4]
            b1, b2, b3, b4 = breaks

            # class 1
            sym1 = make_building_symbol(colors[0])
            append_rule(make_rule(sym1, f"({h_expr}) <= {b1:.4f}", f"<= {b1:.1f}m"))

            # class 2
            sym2 = make_building_symbol(colors[1])
            append_rule(make_rule(sym2, f"({h_expr}) > {b1:.4f} AND ({h_expr}) <= {b2:.4f}", f"{b1:.1f}m - {b2:.1f}m"))

            # class 3
            sym3 = make_building_symbol(colors[2])
            append_rule(make_rule(sym3, f"({h_expr}) > {b2:.4f} AND ({h_expr}) <= {b3:.4f}", f"{b2:.1f}m - {b3:.1f}m"))

            # class 4
            sym4 = make_building_symbol(colors[3])
            append_rule(make_rule(sym4, f"({h_expr}) > {b3:.4f} AND ({h_expr}) <= {b4:.4f}", f"{b3:.1f}m - {b4:.1f}m"))

            # class 5
            sym5 = make_building_symbol(colors[4])
            append_rule(make_rule(sym5, f"({h_expr}) > {b4:.4f}", f"> {b4:.1f}m"))

    # 3. Set the rule-based 3D renderer on the layer
    try:
        renderer = QgsRuleBased3DRenderer(root_rule)
        _bind_renderer_to_layer(renderer, layer)
        layer.setRenderer3D(renderer)
        _refresh_3d_layer(layer)
        return True
    except Exception:
        return _apply_single_building_extrusion(layer, color_hex, height_scale, color_expr)


def apply_base_slab(layer, depth=5.0, top_z=-0.15, color_hex="#5e7274"):
    """Extrude the ground base as a recessed filled plinth clamped to the terrain.

    The plinth's solid slab starts at `top_z - depth` relative to the terrain and
    extrude up to `top_z` (e.g., -0.15m to avoid Z-fighting), following any terrain
    elevation model natively.
    """
    try:
        from qgis._3d import QgsVectorLayer3DRenderer, QgsPolygon3DSymbol
    except Exception:
        return False

    symbol = QgsPolygon3DSymbol()
    with contextlib.suppress(Exception):
        symbol.setAddBackFaces(True)
    with contextlib.suppress(Exception):
        symbol.setRenderedFacade(3)  # Walls and roofs (creates a filled solid slab)
    with contextlib.suppress(Exception):
        symbol.setExtrusionHeight(float(depth))

    # Drop the slab so its top lands at top_z: base height = top_z - depth.
    with contextlib.suppress(Exception):
        symbol.setHeight(float(top_z) - float(depth))
    with contextlib.suppress(Exception):
        symbol.setOffset(0.0)  # Reset vertical offset to prevent double shifting
    with contextlib.suppress(Exception):
        from qgis.core import QgsProperty
        ddp = symbol.dataDefinedProperties()
        height_key = getattr(QgsPolygon3DSymbol, "PropertyHeight", None)
        if height_key is not None:
            ddp.setProperty(height_key, QgsProperty.fromValue(float(top_z) - float(depth)))
        ext_key = getattr(QgsPolygon3DSymbol, "PropertyExtrusionHeight", None)
        if ext_key is not None:
            ddp.setProperty(ext_key, QgsProperty.fromValue(float(depth)))
        offset_key = getattr(QgsPolygon3DSymbol, "PropertyOffset", None)
        if offset_key is not None:
            ddp.setProperty(offset_key, QgsProperty.fromValue(0.0))
        symbol.setDataDefinedProperties(ddp)

    # Clamp to terrain so the plinth follows the terrain height model natively.
    for module in ("qgis.core", "qgis._3d"):
        with contextlib.suppress(Exception):
            mod = __import__(module, fromlist=["Qgs3DTypes"])
            symbol.setAltitudeClamping(mod.Qgs3DTypes.AltClampTerrain)
            break

    material = _make_material(color_hex)
    if material is not None:
        for setter in ("setMaterialSettings", "setMaterial"):
            fn = getattr(symbol, setter, None)
            if fn is None:
                continue
            with contextlib.suppress(Exception):
                fn(material)
                break

    try:
        renderer = _bind_renderer_to_layer(QgsVectorLayer3DRenderer(symbol), layer)
        layer.setRenderer3D(renderer)
        _refresh_3d_layer(layer)
        return True
    except Exception:
        return False


def _make_point_3d_symbol(shape_name: str, shape_props: dict, color_hex: str, z_offset: float = 0.0,
                          emissive: bool = False):
    """Build a terrain-clamped QgsPoint3DSymbol with ``shape_name`` ('Sphere' or 'Cylinder')."""
    try:
        from qgis._3d import QgsPoint3DSymbol
    except Exception:
        return None
    symbol = QgsPoint3DSymbol()
    shape_enum = getattr(getattr(QgsPoint3DSymbol, "Shape", QgsPoint3DSymbol), shape_name, None)
    if shape_enum is None:
        shape_enum = getattr(QgsPoint3DSymbol, shape_name, None)
    if shape_enum is not None:
        with contextlib.suppress(Exception):
            symbol.setShape(shape_enum)
    with contextlib.suppress(Exception):
        symbol.setShapeProperties(dict(shape_props))
    _clamp_to_terrain(symbol)
    material = _make_material(color_hex)
    if material is not None:
        if emissive:
            with contextlib.suppress(Exception):
                from qgis.PyQt.QtGui import QColor
                material.setAmbient(QColor(color_hex))
        for setter in ("setMaterialSettings", "setMaterial"):
            fn = getattr(symbol, setter, None)
            if fn is not None:
                with contextlib.suppress(Exception):
                    fn(material)
                    break
    if z_offset:
        with contextlib.suppress(Exception):
            from qgis.PyQt.QtGui import QMatrix4x4
            transform = QMatrix4x4()
            transform.translate(0.0, 0.0, float(z_offset))
            symbol.setTransform(transform)
    return symbol


def apply_tree_3d(layer, color_hex="#5f9e4c", height_scale: float = 1.0):
    """Render tree points as height-tiered 3D trunks + canopies on the terrain.

    Uses 3 height classes from the OSM ``height`` column (small < 5.5 m, medium
    5.5–9 m, tall >= 9 m), each with a brown trunk cylinder and a green crown
    sphere, scaled by ``height_scale``. Falls back to a single-symbol sphere
    renderer when rule-based 3D point rules are unavailable.
    """
    try:
        from qgis._3d import QgsPoint3DSymbol, QgsVectorLayer3DRenderer
    except Exception:
        return False

    try:
        scale = max(0.25, float(height_scale or 1.0))
    except (TypeError, ValueError):
        scale = 1.0

    # Try height-tiered rule-based 3D trees with trunk + canopy first.
    with contextlib.suppress(Exception):
        from qgis._3d import QgsRuleBased3DRenderer
        try:
            from . import styling
        except ImportError:
            import styling

        root_rule = QgsRuleBased3DRenderer.Rule(None)
        h_expr = 'coalesce(to_real("height"), 6.0)'
        tiers = (
            ("Small tree", f"{h_expr} < 5.5", 1.8 * scale, 0.22 * scale, 1.8 * scale,
             styling._scale_hex(color_hex, 1.08)),
            ("Medium tree", f"{h_expr} >= 5.5 AND {h_expr} < 9.0", 2.5 * scale, 0.30 * scale, 2.5 * scale,
             color_hex),
            ("Tall tree", f"{h_expr} >= 9.0", 3.5 * scale, 0.38 * scale, 3.4 * scale,
             styling._scale_hex(color_hex, 0.88)),
        )
        added = 0
        for label, filt, crown_r, trunk_r, trunk_h, crown_col in tiers:
            trunk_sym = _make_point_3d_symbol(
                "Cylinder", {"radius": trunk_r, "length": trunk_h},
                "#6e4f32", z_offset=trunk_h * 0.5,
            )
            crown_sym = _make_point_3d_symbol(
                "Sphere", {"radius": crown_r},
                crown_col, z_offset=trunk_h + crown_r * 0.72,
            )
            for sym, desc in ((trunk_sym, f"{label} trunk"), (crown_sym, f"{label} crown")):
                if sym is None:
                    continue
                rule = QgsRuleBased3DRenderer.Rule(sym, filt, desc)
                root_rule.appendChild(rule)
                added += 1
        if added > 0:
            renderer = _bind_renderer_to_layer(QgsRuleBased3DRenderer(root_rule), layer)
            layer.setRenderer3D(renderer)
            _refresh_3d_layer(layer)
            return True

    symbol = _make_point_3d_symbol("Sphere", {"radius": 2.5 * scale}, color_hex, z_offset=2.5 * scale)
    if symbol is None:
        return False
    try:
        renderer = _bind_renderer_to_layer(QgsVectorLayer3DRenderer(symbol), layer)
        layer.setRenderer3D(renderer)
        _refresh_3d_layer(layer)
        return True
    except Exception:
        return False


def apply_furniture_3d(layer, role: str = "lights") -> bool:
    """Render street furniture point layers (lights, busstops, benches, trashbins) in 3D."""
    if layer is None:
        return False
    try:
        from qgis._3d import QgsVectorLayer3DRenderer
    except Exception:
        return False

    role = (role or "lights").lower()
    if role == "lights":
        with contextlib.suppress(Exception):
            from qgis._3d import QgsRuleBased3DRenderer
            root = QgsRuleBased3DRenderer.Rule(None)
            pole = _make_point_3d_symbol("Cylinder", {"radius": 0.14, "length": 4.2}, "#475569", z_offset=2.1)
            lamp = _make_point_3d_symbol("Sphere", {"radius": 0.42}, "#fde047", z_offset=4.4, emissive=True)
            if pole is not None and lamp is not None:
                root.appendChild(QgsRuleBased3DRenderer.Rule(pole, "", "Lamp pole"))
                root.appendChild(QgsRuleBased3DRenderer.Rule(lamp, "", "Lamp head"))
                renderer = _bind_renderer_to_layer(QgsRuleBased3DRenderer(root), layer)
                layer.setRenderer3D(renderer)
                _refresh_3d_layer(layer)
                return True
        sym = _make_point_3d_symbol("Sphere", {"radius": 0.55}, "#fde047", z_offset=4.0, emissive=True)
    elif role == "busstops":
        sym = _make_point_3d_symbol("Cylinder", {"radius": 0.65, "length": 2.8}, "#0284c7", z_offset=1.4)
    elif role == "benches":
        sym = _make_point_3d_symbol("Cylinder", {"radius": 0.55, "length": 0.65}, "#a66e38", z_offset=0.35)
    else:
        sym = _make_point_3d_symbol("Cylinder", {"radius": 0.30, "length": 0.90}, "#64748b", z_offset=0.45)

    if sym is None:
        return False
    try:
        renderer = _bind_renderer_to_layer(QgsVectorLayer3DRenderer(sym), layer)
        layer.setRenderer3D(renderer)
        _refresh_3d_layer(layer)
        return True
    except Exception:
        return False


def _object_identity(obj):
    try:
        class_name = obj.metaObject().className()
    except Exception:
        class_name = obj.__class__.__name__
    object_name = ""
    with contextlib.suppress(Exception):
        object_name = obj.objectName() or ""
    window_title = ""
    for getter in ("windowTitle", "title"):
        with contextlib.suppress(Exception):
            fn = getattr(obj, getter)
            window_title = fn() or ""
            if window_title:
                break
    return class_name, object_name, window_title


def _mark_canvas_owned(canvas, name=MANAGED_3D_CANVAS_NAME):
    for setter, value in (("setObjectName", name), ("setTitle", name)):
        with contextlib.suppress(Exception):
            getattr(canvas, setter)(value)


def _canvas_matches_name(canvas, name=MANAGED_3D_CANVAS_NAME):
    class_name, object_name, window_title = _object_identity(canvas)
    target = (name or "").strip().lower()
    haystack = " ".join((class_name, object_name, window_title)).lower()
    return bool(target and target in haystack)


def _is_3d_canvas_object(obj):
    with contextlib.suppress(Exception):
        from qgis._3d import Qgs3DMapCanvas
        if isinstance(obj, Qgs3DMapCanvas):
            return True
    class_name, object_name, window_title = _object_identity(obj)
    haystack = " ".join((class_name, object_name, window_title)).lower()
    return "3d" in haystack and "map" in haystack and "canvas" in haystack


def find_3d_map_canvases(iface):
    """Return active Qgs3DMapCanvas objects from widgets and QWindow instances."""
    candidates = []
    with contextlib.suppress(Exception):
        candidates.extend(iface.mapCanvases3D())
    with contextlib.suppress(Exception):
        from qgis.PyQt.QtWidgets import QWidget
        candidates.extend(iface.mainWindow().findChildren(QWidget))
    with contextlib.suppress(Exception):
        from qgis.PyQt.QtWidgets import QApplication
        app = QApplication.instance()
        if app is not None:
            candidates.extend(app.allWidgets())
            candidates.extend(app.allWindows())

    seen = set()
    canvases = []
    for obj in candidates:
        ident = id(obj)
        if ident in seen:
            continue
        seen.add(ident)
        if _is_3d_canvas_object(obj):
            canvases.append(obj)
    return canvases


def find_owned_3d_map_canvas(iface, name=MANAGED_3D_CANVAS_NAME):
    for canvas in find_3d_map_canvases(iface):
        if _canvas_matches_name(canvas, name):
            return canvas
    return None


def _project_3d_layers(iface):
    with contextlib.suppress(Exception):
        layers = list(iface.mapCanvas().layers())
        if layers:
            return layers
    try:
        from qgis.core import QgsProject
        return list(QgsProject.instance().mapLayers().values())
    except Exception:
        return []


def _canvas_extent_and_crs(iface):
    with contextlib.suppress(Exception):
        canvas = iface.mapCanvas()
        return canvas.extent(), canvas.mapSettings().destinationCrs()
    try:
        from qgis.core import QgsProject
        extent = None
        crs = None
        for layer in QgsProject.instance().mapLayers().values():
            with contextlib.suppress(Exception):
                layer_extent = layer.extent()
                if extent is None:
                    extent = layer_extent
                else:
                    extent.combineExtentWith(layer_extent)
                if crs is None:
                    crs = layer.crs()
        return extent, crs
    except Exception:
        return None, None


def _truthy_layer_property(layer, key):
    try:
        value = layer.customProperty(key)
    except Exception:
        return False
    return str(value).strip().lower() in ("1", "true", "yes", "on")


def _apply_basemap_clip(settings, layers, clip_geometry=None):
    with contextlib.suppress(Exception):
        settings.setClippingRegions([])
    if clip_geometry is None or not layers:
        return
    basemaps = [
        layer for layer in layers
        if _truthy_layer_property(layer, "osm_quick_3d/basemap_underlay")
    ]
    if not basemaps:
        return
    with contextlib.suppress(Exception):
        from qgis.core import QgsGeometry, QgsMapClippingRegion
        geom = QgsGeometry(clip_geometry)
        if geom.isEmpty():
            return
        region = QgsMapClippingRegion(geom)
        region.setFeatureClip(QgsMapClippingRegion.FeatureClippingType.ClipPainterOnly)
        region.setRestrictToLayers(True)
        region.setRestrictedLayers(basemaps)
        settings.setClippingRegions([region])


def _sun_direction_vector(azimuth_deg: float = 315.0, altitude_deg: float = 45.0):
    """Compute a 3D unit vector pointing from the sun toward the ground."""
    az = math.radians(float(azimuth_deg))
    alt = math.radians(max(5.0, min(88.0, float(altitude_deg))))
    horiz = math.cos(alt)
    dx = -horiz * math.sin(az)
    dy = -horiz * math.cos(az)
    dz = -math.sin(alt)
    return dx, dy, dz


def apply_3d_atmosphere_and_terrain(
    settings,
    resolution: int = 1024,
    bg_color_hex: str | None = None,
    terrain_layer=None,
    terrain_vertical_scale: float = 1.0,
    edl_enabled: bool = True,
    shadows_enabled: bool = True,
    sun_azimuth: float = 315.0,
    sun_altitude: float = 45.0,
    ambient_occlusion: bool = True,
):
    """Configure DEM/flat terrain, Eye-Dome Lighting (EDL), sun shadows, and SSAO on ``settings``."""
    if settings is None:
        return False

    # 1. Terrain (DEM raster when provided, else Flat terrain)
    terrain_configured = False
    if terrain_layer is not None:
        with contextlib.suppress(Exception):
            from qgis._3d import QgsDemTerrainSettings
            dem = QgsDemTerrainSettings()
            dem.setLayer(terrain_layer)
            with contextlib.suppress(Exception):
                dem.setVerticalScale(max(0.1, float(terrain_vertical_scale or 1.0)))
            with contextlib.suppress(Exception):
                dem.setResolution(256)
            with contextlib.suppress(Exception):
                dem.setSkirtHeight(15.0)
            with contextlib.suppress(Exception):
                dem.setMapTileResolution(int(resolution))
            settings.setTerrainSettings(dem)
            terrain_configured = True

    if not terrain_configured:
        with contextlib.suppress(Exception):
            current = settings.terrainSettings()
            from qgis._3d import QgsFlatTerrainSettings
            if current is None or terrain_layer is None:
                flat = QgsFlatTerrainSettings()
                with contextlib.suppress(Exception):
                    flat.setMapTileResolution(int(resolution))
                settings.setTerrainSettings(flat)

    applied_res = False
    with contextlib.suppress(Exception):
        if hasattr(settings, "terrainSettings"):
            t_settings = settings.terrainSettings()
            if t_settings and hasattr(t_settings, "setMapTileResolution"):
                t_settings.setMapTileResolution(int(resolution))
                settings.setTerrainSettings(t_settings)
                applied_res = True
    if not applied_res:
        with contextlib.suppress(Exception):
            settings.setMapTileResolution(int(resolution))

    with contextlib.suppress(Exception):
        settings.setTerrainRenderingEnabled(True)
    with contextlib.suppress(Exception):
        settings.setTerrainShadingEnabled(bool(terrain_layer is not None))

    # 2. Eye-Dome Lighting (EDL) for crisp architectural depth & silhouette edges
    with contextlib.suppress(Exception):
        settings.setEyeDomeLightingEnabled(bool(edl_enabled))
        settings.setEyeDomeLightingStrength(1000.0)
        settings.setEyeDomeLightingDistance(2)

    # 3. Directional sun light & real-time shadow rendering
    with contextlib.suppress(Exception):
        from qgis._3d import QgsDirectionalLightSettings
        from qgis.core import QgsVector3D
        dx, dy, dz = _sun_direction_vector(sun_azimuth, sun_altitude)
        dlight = QgsDirectionalLightSettings()
        dlight.setDirection(QgsVector3D(dx, dy, dz))
        with contextlib.suppress(Exception):
            dlight.setIntensity(1.0)
        if hasattr(settings, "setLightSources"):
            settings.setLightSources([dlight])
        elif hasattr(settings, "setDirectionalLights"):
            settings.setDirectionalLights([dlight])

    with contextlib.suppress(Exception):
        from qgis._3d import QgsShadowRenderingSettings
        sh = QgsShadowRenderingSettings()
        sh.setRenderShadows(bool(shadows_enabled))
        with contextlib.suppress(Exception):
            sh.setMaximumShadowRenderingDistance(1800.0)
        with contextlib.suppress(Exception):
            sh.setShadowMapResolution(2048)
        settings.setShadowRenderingSettings(sh)

    # 4. Screen-Space Ambient Occlusion (SSAO)
    with contextlib.suppress(Exception):
        if hasattr(settings, "ambientOcclusionSettings") and hasattr(settings, "setAmbientOcclusionSettings"):
            ao = settings.ambientOcclusionSettings()
            if ao is not None and hasattr(ao, "setEnabled"):
                ao.setEnabled(bool(ambient_occlusion))
                settings.setAmbientOcclusionSettings(ao)

    # 5. Background / Sky colour
    if bg_color_hex:
        with contextlib.suppress(Exception):
            from qgis.PyQt.QtGui import QColor
            settings.setBackgroundColor(QColor(bg_color_hex))
    with contextlib.suppress(Exception):
        settings.setIsSkyboxEnabled(False)

    return True


def configure_3d_map_canvas(iface, canvas, resolution=1024, bg_color_hex=None, layers=None,
                            clip_geometry=None, terrain_layer=None, terrain_vertical_scale=1.0,
                            edl_enabled=True, shadows_enabled=True, sun_azimuth=315.0,
                            sun_altitude=45.0, ambient_occlusion=True):
    """Apply layers, extent, terrain, atmosphere and camera framing to one QGIS 3D canvas."""
    if canvas is None:
        return False
    try:
        from qgis.core import QgsProject, QgsVector3D
        settings = canvas.mapSettings()
        if settings is None:
            return False

        layer_list = list(layers) if layers is not None else _project_3d_layers(iface)
        if layers is not None and hasattr(settings, "setLayers"):
            settings.setLayers(layer_list)
        elif layer_list and hasattr(settings, "setLayers"):
            settings.setLayers(layer_list)
        _apply_basemap_clip(settings, layer_list, clip_geometry=clip_geometry)

        extent, crs = _canvas_extent_and_crs(iface)
        if crs is not None:
            with contextlib.suppress(Exception):
                settings.setCrs(crs)
        if extent is not None and not extent.isEmpty():
            with contextlib.suppress(Exception):
                settings.setExtent(extent)
            with contextlib.suppress(Exception):
                center = extent.center()
                settings.setOrigin(QgsVector3D(center.x(), center.y(), 0.0))

        with contextlib.suppress(Exception):
            project = QgsProject.instance()
            settings.setTransformContext(project.transformContext())
            settings.setPathResolver(project.pathResolver())
            settings.resolveReferences(project)

        apply_3d_atmosphere_and_terrain(
            settings,
            resolution=resolution,
            bg_color_hex=bg_color_hex,
            terrain_layer=terrain_layer,
            terrain_vertical_scale=terrain_vertical_scale,
            edl_enabled=edl_enabled,
            shadows_enabled=shadows_enabled,
            sun_azimuth=sun_azimuth,
            sun_altitude=sun_altitude,
            ambient_occlusion=ambient_occlusion,
        )

        scene = None
        with contextlib.suppress(Exception):
            scene = canvas.scene()
            if scene:
                scene.setSceneUpdatesEnabled(False)
                scene.setSceneUpdatesEnabled(True)
                if extent is not None and not extent.isEmpty():
                    scene.setViewFrom2DExtent(extent)
                else:
                    scene.viewZoomFull()
        with contextlib.suppress(Exception):
            controller = canvas.cameraController()
            if controller and extent is not None and not extent.isEmpty():
                center = extent.center()
                distance = max(extent.width(), extent.height(), 100.0) * 1.35
                controller.setLookingAtMapPoint(QgsVector3D(center.x(), center.y(), 0.0), distance, 45.0, 0.0)

        for layer in layer_list:
            _refresh_3d_layer(layer)
        with contextlib.suppress(Exception):
            canvas.requestUpdate()
        return True
    except Exception:
        return False


def set_3d_map_tile_resolution(iface, resolution=1024, bg_color_hex=None,
                               terrain_layer=None, terrain_vertical_scale=1.0,
                               edl_enabled=True, shadows_enabled=True,
                               sun_azimuth=315.0, sun_altitude=45.0,
                               ambient_occlusion=True):
    """Refresh all active QGIS 3D canvases with resolution, terrain, and lighting settings."""
    ok = False
    for canvas in find_3d_map_canvases(iface):
        try:
            settings = canvas.mapSettings()
            if settings is not None:
                apply_3d_atmosphere_and_terrain(
                    settings,
                    resolution=resolution,
                    bg_color_hex=bg_color_hex,
                    terrain_layer=terrain_layer,
                    terrain_vertical_scale=terrain_vertical_scale,
                    edl_enabled=edl_enabled,
                    shadows_enabled=shadows_enabled,
                    sun_azimuth=sun_azimuth,
                    sun_altitude=sun_altitude,
                    ambient_occlusion=ambient_occlusion,
                )
                with contextlib.suppress(Exception):
                    canvas.requestUpdate()
                ok = True
        except Exception:
            ok = configure_3d_map_canvas(
                iface, canvas, resolution, bg_color_hex,
                terrain_layer=terrain_layer,
                terrain_vertical_scale=terrain_vertical_scale,
                edl_enabled=edl_enabled,
                shadows_enabled=shadows_enabled,
                sun_azimuth=sun_azimuth,
                sun_altitude=sun_altitude,
                ambient_occlusion=ambient_occlusion,
            ) or ok
    return ok


def create_managed_3d_map_canvas(iface, resolution=1024, bg_color_hex=None,
                                 name=MANAGED_3D_CANVAS_NAME, layers=None,
                                 clip_geometry=None, terrain_layer=None,
                                 terrain_vertical_scale=1.0, edl_enabled=True,
                                 shadows_enabled=True, sun_azimuth=315.0,
                                 sun_altitude=45.0, ambient_occlusion=True):
    """Create or reuse the plugin-owned native 3D canvas.

    The canvas stays owned by QGIS as a normal 3D Map View. The plugin controls
    its settings, layers, styling and camera, but never reparents the QWindow
    into the dock because that is unstable on Windows/Qt.
    """
    canvas = find_owned_3d_map_canvas(iface, name)
    if canvas is None:
        create_fn = getattr(iface, "createNewMapCanvas3D", None)
        if create_fn is None:
            return None
        try:
            canvas = create_fn(name)
        except TypeError:
            try:
                canvas = create_fn(name, None)
            except Exception:
                canvas = None
        except Exception:
            canvas = None
    if canvas is None:
        return None
    _mark_canvas_owned(canvas, name)
    configure_3d_map_canvas(
        iface, canvas, resolution, bg_color_hex, layers=layers,
        clip_geometry=clip_geometry, terrain_layer=terrain_layer,
        terrain_vertical_scale=terrain_vertical_scale,
        edl_enabled=edl_enabled, shadows_enabled=shadows_enabled,
        sun_azimuth=sun_azimuth, sun_altitude=sun_altitude,
        ambient_occlusion=ambient_occlusion,
    )
    return canvas


def open_3d_view(iface, resolution=1024, bg_color_hex=None):
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

    triggered = False
    for obj_name in ("mActionNew3DMapView", "mActionNew3DMapCanvas", "mActionNew3DMap"):
        act = win.findChild(QAction, obj_name)
        if act is not None:
            with contextlib.suppress(Exception):
                act.trigger()
                triggered = True
                break

    if not triggered:
        # Fall back to matching the action by its (localized) text.
        with contextlib.suppress(Exception):
            for act in win.findChildren(QAction):
                text = (act.text() or "").lower()
                if "3d" in text and ("map" in text or "harita" in text):
                    act.trigger()
                    triggered = True
                    break

    if triggered:
        # Give QGIS 3D Map View time to spawn, then apply settings repeatedly.
        with contextlib.suppress(Exception):
            from qgis.PyQt.QtCore import QTimer
            for delay in (600, 1500, 3000):
                QTimer.singleShot(delay, lambda: set_3d_map_tile_resolution(iface, resolution, bg_color_hex))
        return True
    return False
