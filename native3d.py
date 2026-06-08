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

EXTRUSION_EXPRESSION = 'coalesce(to_real("height"), to_int("building_levels") * 3, 9)'
EMBEDDED_3D_CANVAS_NAME = "OSM Quick 3D Embedded"


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


def _refresh_3d_layer(layer):
    """Force QGIS 3D views to notice a renderer change."""
    for method in ("trigger3DUpdate", "request3DUpdate", "triggerRepaint"):
        fn = getattr(layer, method, None)
        if fn is None:
            continue
        try:
            fn()
        except Exception:
            pass


def _bind_renderer_to_layer(renderer, layer):
    """QGIS LTR can leave Python-created 3D renderers detached unless forced."""
    try:
        renderer.setLayer(layer)
    except Exception:
        pass
    return renderer


def _apply_symbol_extrusion_properties(symbol, height_scale=1.0):
    try:
        symbol.setExtrusionHeight(9.0 * float(height_scale or 1.0))
    except Exception:
        pass
    try:
        symbol.setAddBackFaces(True)
    except Exception:
        pass
    try:
        symbol.setRenderedFacade(3)
    except Exception:
        pass
    try:
        symbol.setEdgesEnabled(True)
        from qgis.PyQt.QtGui import QColor
        symbol.setEdgeColor(QColor("#2b3436"))
        symbol.setEdgeWidth(0.15)
    except Exception:
        pass
    try:
        from qgis.core import QgsProperty
        from qgis._3d import QgsPolygon3DSymbol
        prop_key = getattr(QgsPolygon3DSymbol, "PropertyExtrusionHeight", None)
        if prop_key is not None:
            ddp = symbol.dataDefinedProperties()
            ddp.setProperty(prop_key, QgsProperty.fromExpression(_extrusion_expression(height_scale)))
            symbol.setDataDefinedProperties(ddp)
    except Exception:
        pass
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
            try:
                symbol.setMaterial(material)
            except Exception:
                pass
    try:
        renderer = _bind_renderer_to_layer(QgsVectorLayer3DRenderer(symbol), layer)
        layer.setRenderer3D(renderer)
        _refresh_3d_layer(layer)
        return True
    except Exception:
        return False


def apply_building_extrusion(layer, color_hex="#cac5bf", height_scale=1.0, color_expr=None,
                             color_mode="function", classification="continuous", theme="default"):
    """Attach a 3D renderer that extrudes the building polygons.

    Uses QgsRuleBased3DRenderer to support robust, separate colors by function/height in QGIS LTR.
    """
    try:
        from qgis._3d import QgsRuleBased3DRenderer, QgsPolygon3DSymbol
        from qgis.core import QgsProperty
    except Exception:
        return False

    from . import styling

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
            try:
                rule = QgsRuleBased3DRenderer.Rule(*args)
                break
            except Exception:
                continue
        if rule is None:
            return None
        if symbol is not None:
            for setter in ("setSymbol", "setSymbol3D"):
                fn = getattr(rule, setter, None)
                if fn is None:
                    continue
                try:
                    fn(symbol)
                    break
                except Exception:
                    continue
        if filter_expr:
            for setter in ("setFilterExpression", "setFilter", "setFilterExpressionString"):
                fn = getattr(rule, setter, None)
                if fn is None:
                    continue
                try:
                    fn(filter_expr)
                    break
                except Exception:
                    continue
        if description:
            try:
                rule.setDescription(description)
            except Exception:
                pass
        if is_else:
            try:
                rule.setIsElse(True)
            except Exception:
                pass
        return rule

    def append_rule(rule):
        if rule is None:
            return
        try:
            root_rule.appendChild(rule)
        except Exception:
            pass

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
                    try:
                        fn(mat)
                        break
                    except Exception:
                        pass
        return sym

    if color_mode == "function":
        theme_building_colors = t.get("building_colors", styling.BUILDING_COLORS)

        # Add rules for each function
        # residential
        res_sym = make_building_symbol(theme_building_colors["residential"])
        append_rule(make_rule(res_sym, "lower(\"building\") IN ('apartments','residential','house','detached','terrace','dormitory','bungalow','semidetached_house','hut')", "Residential"))

        # commercial
        com_sym = make_building_symbol(theme_building_colors["commercial"])
        append_rule(make_rule(com_sym, "lower(\"building\") IN ('commercial','retail','office','supermarket','kiosk','hotel')", "Commercial"))

        # industrial
        ind_sym = make_building_symbol(theme_building_colors["industrial"])
        append_rule(make_rule(ind_sym, "lower(\"building\") IN ('industrial','warehouse','manufacture','hangar','factory')", "Industrial"))

        # civic
        civ_sym = make_building_symbol(theme_building_colors["civic"])
        append_rule(make_rule(civ_sym, "lower(\"building\") IN ('school','university','college','kindergarten','hospital','clinic','public','civic','government','townhall')", "Civic"))

        # worship
        wor_sym = make_building_symbol(theme_building_colors["worship"])
        append_rule(make_rule(wor_sym, "lower(\"building\") IN ('church','mosque','temple','synagogue','cathedral','chapel')", "Worship"))

        # other
        oth_sym = make_building_symbol(theme_building_colors["other"])
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
    try:
        symbol.setAddBackFaces(True)
    except Exception:
        pass
    try:
        symbol.setRenderedFacade(3)  # Walls and roofs (creates a filled solid slab)
    except Exception:
        pass
    try:
        symbol.setExtrusionHeight(float(depth))
    except Exception:
        pass

    # Drop the slab so its top lands at top_z: base height = top_z - depth.
    try:
        symbol.setHeight(float(top_z) - float(depth))
    except Exception:
        pass
    try:
        symbol.setOffset(0.0)  # Reset vertical offset to prevent double shifting
    except Exception:
        pass
    try:
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
    except Exception:
        pass

    # Clamp to terrain so the plinth follows the terrain height model natively.
    for module in ("qgis.core", "qgis._3d"):
        try:
            mod = __import__(module, fromlist=["Qgs3DTypes"])
            symbol.setAltitudeClamping(mod.Qgs3DTypes.AltClampTerrain)
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
        renderer = _bind_renderer_to_layer(QgsVectorLayer3DRenderer(symbol), layer)
        layer.setRenderer3D(renderer)
        _refresh_3d_layer(layer)
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
        renderer = _bind_renderer_to_layer(QgsVectorLayer3DRenderer(symbol), layer)
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
    try:
        object_name = obj.objectName() or ""
    except Exception:
        pass
    window_title = ""
    for getter in ("windowTitle", "title"):
        try:
            fn = getattr(obj, getter)
            window_title = fn() or ""
            if window_title:
                break
        except Exception:
            continue
    return class_name, object_name, window_title


def _mark_canvas_owned(canvas, name=EMBEDDED_3D_CANVAS_NAME):
    for setter, value in (("setObjectName", name), ("setTitle", name)):
        try:
            getattr(canvas, setter)(value)
        except Exception:
            pass


def _canvas_matches_name(canvas, name=EMBEDDED_3D_CANVAS_NAME):
    class_name, object_name, window_title = _object_identity(canvas)
    target = (name or "").strip().lower()
    haystack = " ".join((class_name, object_name, window_title)).lower()
    return bool(target and target in haystack)


def _is_3d_canvas_object(obj):
    try:
        from qgis._3d import Qgs3DMapCanvas
        if isinstance(obj, Qgs3DMapCanvas):
            return True
    except Exception:
        pass
    class_name, object_name, window_title = _object_identity(obj)
    haystack = " ".join((class_name, object_name, window_title)).lower()
    return "3d" in haystack and "map" in haystack and "canvas" in haystack


def find_3d_map_canvases(iface):
    """Return active Qgs3DMapCanvas objects from widgets and QWindow instances."""
    candidates = []
    try:
        candidates.extend(iface.mapCanvases3D())
    except Exception:
        pass
    try:
        from qgis.PyQt.QtWidgets import QWidget
        candidates.extend(iface.mainWindow().findChildren(QWidget))
    except Exception:
        pass
    try:
        from qgis.PyQt.QtWidgets import QApplication
        app = QApplication.instance()
        if app is not None:
            candidates.extend(app.allWidgets())
            candidates.extend(app.allWindows())
    except Exception:
        pass

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


def find_owned_3d_map_canvas(iface, name=EMBEDDED_3D_CANVAS_NAME):
    for canvas in find_3d_map_canvases(iface):
        if _canvas_matches_name(canvas, name):
            return canvas
    return None


def _project_3d_layers(iface):
    try:
        layers = list(iface.mapCanvas().layers())
        if layers:
            return layers
    except Exception:
        pass
    try:
        from qgis.core import QgsProject
        return list(QgsProject.instance().mapLayers().values())
    except Exception:
        return []


def _canvas_extent_and_crs(iface):
    try:
        canvas = iface.mapCanvas()
        return canvas.extent(), canvas.mapSettings().destinationCrs()
    except Exception:
        pass
    try:
        from qgis.core import QgsProject
        extent = None
        crs = None
        for layer in QgsProject.instance().mapLayers().values():
            try:
                layer_extent = layer.extent()
                if extent is None:
                    extent = layer_extent
                else:
                    extent.combineExtentWith(layer_extent)
                if crs is None:
                    crs = layer.crs()
            except Exception:
                continue
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
    try:
        settings.setClippingRegions([])
    except Exception:
        pass
    if clip_geometry is None or not layers:
        return
    basemaps = [
        layer for layer in layers
        if _truthy_layer_property(layer, "osm_quick_3d/basemap_underlay")
    ]
    if not basemaps:
        return
    try:
        from qgis.core import QgsGeometry, QgsMapClippingRegion
        geom = QgsGeometry(clip_geometry)
        if geom.isEmpty():
            return
        region = QgsMapClippingRegion(geom)
        region.setFeatureClip(QgsMapClippingRegion.FeatureClippingType.ClipPainterOnly)
        region.setRestrictToLayers(True)
        region.setRestrictedLayers(basemaps)
        settings.setClippingRegions([region])
    except Exception:
        pass


def configure_3d_map_canvas(iface, canvas, resolution=1024, bg_color_hex=None, layers=None,
                            clip_geometry=None):
    """Apply layers, extent, terrain and camera framing to one QGIS 3D canvas."""
    if canvas is None:
        return False
    try:
        from qgis.core import QgsProject, QgsVector3D
        from qgis.PyQt.QtGui import QColor
        settings = canvas.mapSettings()
        if settings is None:
            return False

        layer_list = list(layers) if layers is not None else _project_3d_layers(iface)
        if layer_list and hasattr(settings, "setLayers"):
            settings.setLayers(layer_list)
        _apply_basemap_clip(settings, layer_list, clip_geometry=clip_geometry)

        extent, crs = _canvas_extent_and_crs(iface)
        if crs is not None:
            try:
                settings.setCrs(crs)
            except Exception:
                pass
        if extent is not None and not extent.isEmpty():
            try:
                settings.setExtent(extent)
            except Exception:
                pass
            try:
                center = extent.center()
                settings.setOrigin(QgsVector3D(center.x(), center.y(), 0.0))
            except Exception:
                pass

        try:
            project = QgsProject.instance()
            settings.setTransformContext(project.transformContext())
            settings.setPathResolver(project.pathResolver())
            settings.resolveReferences(project)
        except Exception:
            pass

        try:
            if settings.terrainSettings() is None:
                from qgis._3d import QgsFlatTerrainSettings
                settings.setTerrainSettings(QgsFlatTerrainSettings())
        except Exception:
            pass

        applied = False
        try:
            if hasattr(settings, "terrainSettings"):
                t_settings = settings.terrainSettings()
                if t_settings and hasattr(t_settings, "setMapTileResolution"):
                    t_settings.setMapTileResolution(int(resolution))
                    settings.setTerrainSettings(t_settings)
                    applied = True
        except Exception:
            pass
        if not applied:
            try:
                settings.setMapTileResolution(int(resolution))
            except Exception:
                pass

        if bg_color_hex:
            try:
                settings.setBackgroundColor(QColor(bg_color_hex))
            except Exception:
                pass
        try:
            settings.setIsSkyboxEnabled(False)
        except Exception:
            pass
        try:
            settings.setTerrainRenderingEnabled(True)
        except Exception:
            pass

        scene = None
        try:
            scene = canvas.scene()
            if scene:
                scene.setSceneUpdatesEnabled(False)
                scene.setSceneUpdatesEnabled(True)
                if extent is not None and not extent.isEmpty():
                    scene.setViewFrom2DExtent(extent)
                else:
                    scene.viewZoomFull()
        except Exception:
            pass
        try:
            controller = canvas.cameraController()
            if controller and extent is not None and not extent.isEmpty():
                center = extent.center()
                distance = max(extent.width(), extent.height(), 100.0) * 1.35
                controller.setLookingAtMapPoint(QgsVector3D(center.x(), center.y(), 0.0), distance, 45.0, 0.0)
        except Exception:
            pass

        for layer in layer_list:
            _refresh_3d_layer(layer)
        try:
            canvas.requestUpdate()
        except Exception:
            pass
        return True
    except Exception:
        return False


def set_3d_map_tile_resolution(iface, resolution=1024, bg_color_hex=None):
    """Refresh all active QGIS 3D canvases; retained for existing UI hooks."""
    ok = False
    for canvas in find_3d_map_canvases(iface):
        ok = configure_3d_map_canvas(iface, canvas, resolution, bg_color_hex) or ok
    return ok


def create_embedded_3d_map_canvas(iface, resolution=1024, bg_color_hex=None,
                                  name=EMBEDDED_3D_CANVAS_NAME, layers=None,
                                  clip_geometry=None):
    """Create or reuse the plugin-owned 3D canvas without triggering the menu action."""
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
        iface, canvas, resolution, bg_color_hex, layers=layers, clip_geometry=clip_geometry)
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
            try:
                act.trigger()
                triggered = True
                break
            except Exception:
                pass

    if not triggered:
        # Fall back to matching the action by its (localized) text.
        try:
            for act in win.findChildren(QAction):
                text = (act.text() or "").lower()
                if "3d" in text and ("map" in text or "harita" in text):
                    act.trigger()
                    triggered = True
                    break
        except Exception:
            pass

    if triggered:
        # Give QGIS 3D Map View time to spawn, then apply settings repeatedly.
        try:
            from qgis.PyQt.QtCore import QTimer
            for delay in (600, 1500, 3000):
                QTimer.singleShot(delay, lambda: set_3d_map_tile_resolution(iface, resolution, bg_color_hex))
        except Exception:
            pass
        return True
    return False
