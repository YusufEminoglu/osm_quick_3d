# -*- coding: utf-8 -*-
"""Functional 2D styling for the downloaded OSM layers.

Every layer lands already categorized the way a planner reads a city, mirroring
the web viewer's semantic colours but as native QGIS renderers: buildings by OSM
function, roads by ``highway`` class (colour and width), water blue, greens
green. This is the "like qgis2threejs but more functional" colouring — ready-made
function categories instead of a flat single symbol.
"""
from __future__ import annotations

from qgis.core import (
    QgsCategorizedSymbolRenderer,
    QgsFillSymbol,
    QgsLineSymbol,
    QgsMarkerSymbol,
    QgsProperty,
    QgsRendererCategory,
    QgsSingleSymbolRenderer,
    QgsSymbolLayer,
)

# ── palettes ────────────────────────────────────────────────────────────────
# Muted European/North-American massing tones, matching the web viewer defaults.
BUILDING_COLORS = {
    "residential": "#d8c3b1",
    "commercial": "#b7c2d0",
    "industrial": "#c6b9a4",
    "civic": "#cdd6d2",
    "worship": "#d8cfe2",
    "other": "#cac5bf",
}
# class -> (colour, width in mm, dashed)
ROAD_STYLE = {
    "major": ("#e1846f", 1.3, False),
    "primary": ("#efb066", 1.1, False),
    "secondary": ("#f1d784", 0.95, False),
    "tertiary": ("#f6f0df", 0.8, False),
    "residential": ("#eae5da", 0.6, False),
    "service": ("#e4dfd4", 0.4, False),
    "foot": ("#d2b59a", 0.5, True),
    "other": ("#e0dbd0", 0.5, False),
}
GREEN_COLORS = {
    "park": ("#a9c08a", "#7c8a68"),
    "forest": ("#7f9e6a", "#5f7e4a"),
    "pitch": ("#b6cf93", "#8da96d"),
    "cemetery": ("#9bae8f", "#788a6f"),
    "green": ("#a7b98f", "#7c8f68"),
    "parking": ("#cbd1d6", "#a0a7ad"),     # asphalt grey, darker grey outline
    "pedestrian": ("#e3e1db", "#b5b3ad"),  # stone/paved light grey, darker outline
}

# OSM tag values are emitted lower-cased by osm_download; lower() here is a
# harmless safety net so the categories still match if that ever changes.
BUILDING_CLASS_EXPR = (
    "CASE"
    " WHEN lower(\"building\") IN ('apartments','residential','house','detached',"
    "'terrace','dormitory','bungalow','semidetached_house','hut') THEN 'residential'"
    " WHEN lower(\"building\") IN ('commercial','retail','office','supermarket',"
    "'kiosk','hotel') THEN 'commercial'"
    " WHEN lower(\"building\") IN ('industrial','warehouse','manufacture','hangar',"
    "'factory') THEN 'industrial'"
    " WHEN lower(\"building\") IN ('school','university','college','kindergarten',"
    "'hospital','clinic','public','civic','government','townhall') THEN 'civic'"
    " WHEN lower(\"building\") IN ('church','mosque','temple','synagogue',"
    "'cathedral','chapel') THEN 'worship'"
    " ELSE 'other' END"
)
ROAD_CLASS_EXPR = (
    "CASE"
    " WHEN lower(\"highway\") IN ('motorway','trunk','motorway_link','trunk_link') THEN 'major'"
    " WHEN lower(\"highway\") IN ('primary','primary_link') THEN 'primary'"
    " WHEN lower(\"highway\") IN ('secondary','secondary_link') THEN 'secondary'"
    " WHEN lower(\"highway\") IN ('tertiary','tertiary_link') THEN 'tertiary'"
    " WHEN lower(\"highway\") IN ('residential','unclassified','living_street','road') THEN 'residential'"
    " WHEN lower(\"highway\") IN ('service','track') THEN 'service'"
    " WHEN lower(\"highway\") IN ('footway','path','pedestrian','steps','corridor','bridleway') THEN 'foot'"
    " ELSE 'other' END"
)
GREEN_CLASS_EXPR = (
    "CASE"
    " WHEN lower(\"amenity\") = 'parking' OR lower(\"landuse\") = 'parking' THEN 'parking'"
    " WHEN lower(\"highway\") IN ('pedestrian','footway','living_street') OR lower(\"place\") = 'square' OR lower(\"amenity\") = 'marketplace' THEN 'pedestrian'"
    " WHEN lower(\"leisure\") IN ('park','garden') OR lower(\"landuse\") IN ('grass','recreation_ground','meadow','village_green','orchard','vineyard','farmland','allotments','greenfield') THEN 'park'"
    " WHEN lower(\"landuse\") = 'forest' OR lower(\"natural\") IN ('wood','woodland','scrub','grassland','heath','nature_reserve','common') THEN 'forest'"
    " WHEN lower(\"leisure\") IN ('pitch','playground','dog_park','golf_course') THEN 'pitch'"
    " WHEN lower(\"landuse\") = 'cemetery' THEN 'cemetery'"
    " ELSE 'green' END"
)


# ── building colour modes ────────────────────────────────────────────────────
# Selectable in the dialog. "function" keeps the categorized OSM-use palette;
# the rest are soft height-graduated ramps (light = low, deeper = tall) in a
# single tint, applied identically to the 2D fill and the 3D diffuse colour.
BUILDING_COLOR_FUNCTION = "function"

# (light, deep) endpoints of each soft tinted ramp.
_BUILDING_RAMPS = {
    "height": ("#e2e8ee", "#56657d"),     # cool neutral, light steel -> slate
    "soft_gray": ("#e8e9e8", "#6e746f"),  # tinted gray
    "soft_warm": ("#ede2d4", "#8a7460"),  # tinted warm
    "teal": ("#dbeae7", "#3c7c77"),       # soft teal
    "soft_salmon": ("#f1ded7", "#b9776a"),  # soft salmon -> terracotta
    "soft_purple": ("#e4dded", "#6f5c86"),  # soft lilac -> deep purple
    "soft_sand": ("#ece6d6", "#9a8a63"),    # soft sand -> slate-sand
}

# (value, label) for the dialog combo.
BUILDING_COLOR_MODES = (
    (BUILDING_COLOR_FUNCTION, "By function (OSM use)"),
    ("height", "By height (graduated)"),
    ("soft_gray", "Soft tinted gray"),
    ("soft_warm", "Soft tinted warm"),
    ("teal", "Soft teal"),
    ("soft_salmon", "Soft salmon"),
    ("soft_purple", "Soft purple"),
    ("soft_sand", "Soft sand"),
)

# Per-feature extrusion/colour height, in metres (tagged height, else floors*3).
_BUILDING_HEIGHT_EXPR = 'coalesce(to_real("height"), to_int("building_levels") * 3, 9)'
_RAMP_LO_M, _RAMP_HI_M = 3.0, 60.0


def _hex_to_rgb(value):
    value = value.lstrip("#")
    return int(value[0:2], 16), int(value[2:4], 16), int(value[4:6], 16)


def _scale_hex(value, factor):
    """Darken (factor<1) or lighten-toward-white (factor>1) a hex colour."""
    r, g, b = _hex_to_rgb(value)
    if factor <= 1.0:
        r, g, b = r * factor, g * factor, b * factor
    else:
        t = min(1.0, factor - 1.0)
        r, g, b = r + (255 - r) * t, g + (255 - g) * t, b + (255 - b) * t
    return "#%02x%02x%02x" % (int(r), int(g), int(b))


_BASE_NEUTRAL = "#5e7274"


def base_color_hex(mode=BUILDING_COLOR_FUNCTION) -> str:
    """The 3D ground-base slab colour, harmonised with the building colour mode.

    For a tinted ramp the base is a darkened tone of that tint, so the plinth and
    the city read as one palette; function/height keep a neutral slate.
    """
    if mode in _BUILDING_RAMPS:
        return _scale_hex(_BUILDING_RAMPS[mode][1], 0.72)
    return _BASE_NEUTRAL


def building_base_color(mode=BUILDING_COLOR_FUNCTION) -> str:
    """A single fallback hex color for the building color mode."""
    if mode in _BUILDING_RAMPS:
        return _BUILDING_RAMPS[mode][1]
    return "#cac5bf"


def _interpolate_color(c1_hex, c2_hex, factor):
    """Interpolate between two hex colors by factor [0.0, 1.0]."""
    r1, g1, b1 = _hex_to_rgb(c1_hex)
    r2, g2, b2 = _hex_to_rgb(c2_hex)
    r = int(r1 + (r2 - r1) * factor)
    g = int(g1 + (g2 - g1) * factor)
    b = int(b1 + (b2 - b1) * factor)
    return "#%02x%02x%02x" % (r, g, b)


def get_breaks_and_colors(layer, lo_hex, hi_hex, classification="continuous"):
    """Calculate dynamic min, max, breaks, and category colors from actual building heights."""
    min_h = 3.0
    max_h = 30.0
    b = [9.0, 15.0, 21.0, 27.0]

    if layer is not None and layer.featureCount() > 0:
        try:
            heights = []
            for feat in layer.getFeatures():
                h = feat["height"]
                if h is None or h <= 0:
                    levels = feat["building_levels"]
                    if levels is not None and levels > 0:
                        h = levels * 3.0
                    else:
                        h = 9.0
                else:
                    h = float(h)
                heights.append(h)
            
            if heights:
                heights.sort()
                min_h = heights[0]
                max_h = heights[-1]
                if max_h <= min_h:
                    max_h = min_h + 10.0
                
                n = len(heights)
                classification = str(classification).lower()
                if classification == "quantile" and n >= 5:
                    b1 = heights[int(n * 0.2)]
                    b2 = heights[int(n * 0.4)]
                    b3 = heights[int(n * 0.6)]
                    b4 = heights[int(n * 0.8)]
                    b = [b1, b2, b3, b4]
                else:
                    step = (max_h - min_h) / 5.0
                    b = [min_h + step, min_h + 2 * step, min_h + 3 * step, min_h + 4 * step]
        except Exception:
            pass

    c1 = lo_hex
    c2 = _interpolate_color(lo_hex, hi_hex, 0.25)
    c3 = _interpolate_color(lo_hex, hi_hex, 0.5)
    c4 = _interpolate_color(lo_hex, hi_hex, 0.75)
    c5 = hi_hex
    
    return min_h, max_h, b, [c1, c2, c3, c4, c5]


def building_color_expression(mode=BUILDING_COLOR_FUNCTION, classification="continuous", layer=None) -> str:
    """A QGIS expression returning each building's colour for ``mode``.

    For ``function`` it wraps ``BUILDING_CLASS_EXPR`` into the OSM-use hex palette;
    for the height/tinted modes it returns a soft ``color_rgb`` ramp by height. The
    same expression drives the 2D fill and the native 3D diffuse colour, so the
    massing always matches the flat map.
    """
    if mode in _BUILDING_RAMPS:
        lo_hex, hi_hex = _BUILDING_RAMPS[mode]
        classification = str(classification).lower()
        min_h, max_h, breaks, colors = get_breaks_and_colors(layer, lo_hex, hi_hex, classification)
        h = _BUILDING_HEIGHT_EXPR
        
        if classification == "continuous":
            r1, g1, b1 = _hex_to_rgb(lo_hex)
            r2, g2, b2 = _hex_to_rgb(hi_hex)
            def part(a, b):
                return f"scale_linear({h}, {min_h}, {max_h}, {a}, {b})"
            return f"color_rgb({part(r1, r2)}, {part(g1, g2)}, {part(b1, b2)})"
        else:
            c1, c2, c3, c4, c5 = colors
            b1, b2, b3, b4 = breaks
            return (
                f"CASE "
                f" WHEN {h} <= {b1} THEN '{c1}'"
                f" WHEN {h} <= {b2} THEN '{c2}'"
                f" WHEN {h} <= {b3} THEN '{c3}'"
                f" WHEN {h} <= {b4} THEN '{c4}'"
                f" ELSE '{c5}' END"
            )
    cases = " ".join(f"WHEN '{key}' THEN '{hexv}'" for key, hexv in BUILDING_COLORS.items())
    return f"CASE ({BUILDING_CLASS_EXPR}) {cases} ELSE '{BUILDING_COLORS['other']}' END"


def building_color_swatches(mode=BUILDING_COLOR_FUNCTION):
    """Hex stops representing ``mode`` for a dialog preview swatch.

    For ramp modes it's (light, deep); for ``function`` it's the OSM-use palette.
    """
    if mode in _BUILDING_RAMPS:
        return list(_BUILDING_RAMPS[mode])
    return list(BUILDING_COLORS.values())


# ── symbol factories ────────────────────────────────────────────────────────
def _fill(color_hex, outline="#8d8378", outline_w=0.16):
    return QgsFillSymbol.createSimple({
        "color": color_hex,
        "outline_color": outline,
        "outline_width": str(outline_w),
        "style": "solid",
    })


def _line(color_hex, width, dashed=False):
    props = {"color": color_hex, "width": str(width), "capstyle": "round", "joinstyle": "round"}
    if dashed:
        props["line_style"] = "dash"
    return QgsLineSymbol.createSimple(props)


def _marker(color_hex, size=2.0, outline="#5f574d"):
    return QgsMarkerSymbol.createSimple({
        "name": "circle", "color": color_hex, "size": str(size),
        "outline_color": outline, "outline_width": "0.2",
    })


def _labelize(key):
    return key.replace("_", " ").capitalize()


def _categorized(expression, mapping_to_symbol):
    cats = [QgsRendererCategory(key, sym, _labelize(key)) for key, sym in mapping_to_symbol.items()]
    return QgsCategorizedSymbolRenderer(expression, cats)


# ── per-layer styling ───────────────────────────────────────────────────────
def style_buildings(layer, mode=BUILDING_COLOR_FUNCTION, classification="continuous"):
    """Style buildings by ``mode``: function categories or a soft height ramp.

    Ramp modes use one fill symbol whose colour is data-defined by the same
    expression the 3D massing uses, so 2D and 3D stay identical.
    """
    if mode in _BUILDING_RAMPS:
        symbol = _fill("#cfd6dd")
        expr = building_color_expression(mode, classification=classification, layer=layer)
        try:
            symbol.symbolLayer(0).setDataDefinedProperty(
                QgsSymbolLayer.PropertyFillColor, QgsProperty.fromExpression(expr))
        except Exception:
            pass
        layer.setRenderer(QgsSingleSymbolRenderer(symbol))
    else:
        mapping = {k: _fill(v) for k, v in BUILDING_COLORS.items()}
        layer.setRenderer(_categorized(BUILDING_CLASS_EXPR, mapping))
    layer.triggerRepaint()


def style_roads(layer):
    mapping = {k: _line(c, w, d) for k, (c, w, d) in ROAD_STYLE.items()}
    layer.setRenderer(_categorized(ROAD_CLASS_EXPR, mapping))
    layer.triggerRepaint()


def style_greens(layer):
    mapping = {k: _fill(color, outline=out_color, outline_w=0.12) for k, (color, out_color) in GREEN_COLORS.items()}
    layer.setRenderer(_categorized(GREEN_CLASS_EXPR, mapping))
    layer.triggerRepaint()


def style_water(layer):
    layer.setRenderer(QgsSingleSymbolRenderer(_line("#6fa8c7", 0.9)))
    layer.triggerRepaint()


def style_waterareas(layer):
    layer.setRenderer(QgsSingleSymbolRenderer(_fill("#a5c9eb", outline="#7298ba", outline_w=0.12)))
    layer.triggerRepaint()


def style_bikelanes(layer):
    layer.setRenderer(QgsSingleSymbolRenderer(_line("#8fbf8f", 0.6, dashed=True)))
    layer.triggerRepaint()


def style_trees(layer):
    layer.setRenderer(QgsSingleSymbolRenderer(_marker("#6f9e5c", 1.8)))
    layer.triggerRepaint()


def label_by_name(layer, size=8.0, color_hex="#3a4042", field="name"):
    """Label a layer by its ``field`` (default OSM ``name``), with a white halo.

    Only non-empty names are drawn. Fully defensive: returns False on builds
    without the labeling API so the run still succeeds, just unlabelled.
    """
    try:
        from qgis.core import (
            QgsPalLayerSettings,
            QgsTextBufferSettings,
            QgsTextFormat,
            QgsVectorLayerSimpleLabeling,
        )
        from qgis.PyQt.QtGui import QColor
    except Exception:
        return False
    try:
        settings = QgsPalLayerSettings()
        # Expression so only features with a real name get a label.
        settings.fieldName = f"CASE WHEN \"{field}\" IS NOT NULL AND \"{field}\" <> '' THEN \"{field}\" END"
        settings.isExpression = True

        fmt = QgsTextFormat()
        fmt.setSize(size)
        fmt.setColor(QColor(color_hex))
        buffer = QgsTextBufferSettings()
        buffer.setEnabled(True)
        buffer.setSize(0.9)
        buffer.setColor(QColor("#ffffff"))
        fmt.setBuffer(buffer)
        settings.setFormat(fmt)

        layer.setLabeling(QgsVectorLayerSimpleLabeling(settings))
        layer.setLabelsEnabled(True)
        layer.triggerRepaint()
        return True
    except Exception:
        return False


def style_base(layer, mode=BUILDING_COLOR_FUNCTION, transparent=False, bg_color_hex="#ffffff"):
    """Subtle 2D ground fill, tinted to match the building colour ``mode``.

    If ``transparent`` is True, we use QgsInvertedPolygonRenderer to mask out the
    draped basemap outside the study area with the map canvas background color.
    """
    slab = base_color_hex(mode)
    outline = _scale_hex(slab, 1.25)
    try:
        from qgis.PyQt.QtGui import QPainter
        layer.setBlendMode(QPainter.CompositionMode_SourceOver)
    except Exception:
        pass
    if transparent:
        try:
            from qgis.core import QgsInvertedPolygonRenderer
            symbol = QgsFillSymbol.createSimple({
                "color": bg_color_hex,
                "outline_color": "0,0,0,0",
                "outline_width": "0.0",
                "style": "solid",
            })
            layer.setRenderer(QgsInvertedPolygonRenderer(QgsSingleSymbolRenderer(symbol)))
        except Exception:
            # Fallback if inverted renderer fails
            symbol = QgsFillSymbol.createSimple({
                "color": "0,0,0,0",
                "outline_color": outline,
                "outline_width": "0.2",
                "style": "no",
            })
            layer.setRenderer(QgsSingleSymbolRenderer(symbol))
    else:
        fill = _scale_hex(slab, 1.6)        # light tint of the slab colour
        symbol = _fill(fill, outline=outline, outline_w=0.2)
        layer.setRenderer(QgsSingleSymbolRenderer(symbol))
    layer.triggerRepaint()


def style_points(layer, color_hex="#b9897a", size=1.8):
    layer.setRenderer(QgsSingleSymbolRenderer(_marker(color_hex, size)))
    layer.triggerRepaint()


def style_base_3d_2d(layer):
    """Make the 2D representation of the 3D plinth layer completely invisible."""
    symbol = QgsFillSymbol.createSimple({
        "color": "0,0,0,0",
        "outline_color": "0,0,0,0",
        "style": "no",
        "outline_width": "0.0",
    })
    layer.setRenderer(QgsSingleSymbolRenderer(symbol))
    layer.triggerRepaint()


def set_layer_color(layer, color_hex, category_val=None):
    if layer is None:
        return
    from qgis.core import QgsCategorizedSymbolRenderer, QgsSingleSymbolRenderer
    from qgis.PyQt.QtGui import QColor
    
    renderer = layer.renderer()
    if category_val is not None and isinstance(renderer, QgsCategorizedSymbolRenderer):
        for i, cat in enumerate(renderer.categories()):
            if str(cat.value()).lower() == str(category_val).lower():
                sym = cat.symbol().clone()
                sym.setColor(QColor(color_hex))
                renderer.updateCategorySymbol(i, sym)
                break
    elif isinstance(renderer, QgsSingleSymbolRenderer):
        sym = renderer.symbol().clone()
        sym.setColor(QColor(color_hex))
        renderer.setSymbol(sym)
    layer.triggerRepaint()


def set_trees_size(layer, size):
    if layer is None:
        return
    from qgis.core import QgsSingleSymbolRenderer
    renderer = layer.renderer()
    if isinstance(renderer, QgsSingleSymbolRenderer):
        sym = renderer.symbol().clone()
        if hasattr(sym, "setSize"):
            sym.setSize(float(size))
            renderer.setSymbol(sym)
    layer.triggerRepaint()


def get_layer_color(layer, category_val=None) -> str:
    """Get the color of a layer or category as a hex string."""
    if layer is None:
        return ""
    from qgis.core import QgsCategorizedSymbolRenderer, QgsSingleSymbolRenderer
    renderer = layer.renderer()
    if category_val is not None and isinstance(renderer, QgsCategorizedSymbolRenderer):
        for cat in renderer.categories():
            if str(cat.value()).lower() == str(category_val).lower():
                return cat.symbol().color().name()
    elif isinstance(renderer, QgsSingleSymbolRenderer):
        return renderer.symbol().color().name()
    return ""


def get_trees_size(layer) -> float:
    """Get the marker size of the tree layer."""
    if layer is None:
        return 1.8
    from qgis.core import QgsSingleSymbolRenderer
    renderer = layer.renderer()
    if isinstance(renderer, QgsSingleSymbolRenderer):
        sym = renderer.symbol()
        if hasattr(sym, "size"):
            return sym.size()
    return 1.8
