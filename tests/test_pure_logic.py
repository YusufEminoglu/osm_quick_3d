# -*- coding: utf-8 -*-
"""Headless tests for the pure-Python logic of OSM Quick 3D.

The plugin's heavy lifting depends on QGIS, which can't be imported outside a
QGIS Python environment. But a meaningful slice is plain Python — OSM tag
parsing, floor-count inference, the UTM-zone formula, the building-colour
expression builder and the Overpass disk cache — and that slice is exactly the
part most prone to silent regressions across releases.

This harness injects lightweight stub modules for ``qgis`` / ``qgis.PyQt`` so the
two modules import, then exercises the pure functions. No QGIS, no network.

Run:  py -3 tests/test_pure_logic.py   (from the plugin dir)
"""
from __future__ import annotations

import os
import sys
import types

PLUGIN_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


# ── qgis stubs ───────────────────────────────────────────────────────────────
class _Anything:
    """A class whose every attribute/call returns another harmless stub."""

    def __init__(self, *args, **kwargs):
        pass

    def __getattr__(self, name):
        return _Anything()

    def __call__(self, *args, **kwargs):
        return _Anything()


class _StubModule(types.ModuleType):
    """A module where any attribute access yields a stub class."""

    def __getattr__(self, name):
        return _Anything


def _install_qgis_stubs():
    for name in ("qgis", "qgis.core", "qgis.gui", "qgis.PyQt",
                 "qgis.PyQt.QtCore", "qgis.PyQt.QtGui", "qgis.PyQt.QtWidgets",
                 "qgis._3d"):
        sys.modules.setdefault(name, _StubModule(name))
    # QVariant needs concrete-looking type attributes for the field definitions.
    qvariant = type("QVariant", (), {"String": 10, "Int": 2, "Double": 6})
    sys.modules["qgis.PyQt.QtCore"].QVariant = qvariant


_install_qgis_stubs()
sys.path.insert(0, PLUGIN_DIR)

import osm_download as osm  # noqa: E402
import styling  # noqa: E402


# ── tiny assert harness ──────────────────────────────────────────────────────
_failures = []


def check(label, condition):
    status = "ok  " if condition else "FAIL"
    if not condition:
        _failures.append(label)
    print(f"  [{status}] {label}")


# ── tests ────────────────────────────────────────────────────────────────────
def test_parse_osm_number():
    print("_parse_osm_number")
    check("plain int", osm._parse_osm_number("12") == 12.0)
    check("decimal", osm._parse_osm_number("12.5") == 12.5)
    check("metres suffix", osm._parse_osm_number("12 m") == 12.0)
    check("semicolon list takes first", osm._parse_osm_number("3;4") == 3.0)
    check("None -> None", osm._parse_osm_number(None) is None)
    check("garbage -> None", osm._parse_osm_number("abc") is None)


def test_building_levels():
    print("_building_levels")
    check("explicit levels win", osm._building_levels({"building:levels": "5"}) == 5)
    check("levels + roof:levels", osm._building_levels(
        {"building:levels": "5", "roof:levels": "1"}) == 6)
    check("height/3 fallback", osm._building_levels({"height": "9"}) == 3)
    check("type default apartments=4",
          osm._building_levels({"building": "apartments"}) == 4)
    check("unknown type default=3", osm._building_levels({"building": "weird"}) == 3)
    check("never below 1", osm._building_levels({"building:levels": "0"}) >= 1)


def test_utm_epsg():
    print("utm_epsg_for")
    check("Istanbul ~29E,41N -> 32635", osm.utm_epsg_for(29.0, 41.0) == 32635)
    check("Izmir ~27E,38N -> 32635", osm.utm_epsg_for(27.0, 38.0) == 32635)
    check("south hemisphere uses 327xx", osm.utm_epsg_for(20.0, -10.0) // 100 == 327)
    check("zone clamped 1..60", 32601 <= osm.utm_epsg_for(-179.0, 10.0) <= 32660)


def test_waterway_width():
    print("_waterway_width")
    check("river default 8", osm._waterway_width({"waterway": "river"}) == 8.0)
    check("explicit width wins", osm._waterway_width(
        {"waterway": "river", "width": "20"}) == 20.0)
    check("unknown class -> 3", osm._waterway_width({"waterway": "weird"}) == 3.0)


def test_building_color_expression():
    print("building_color_expression")
    expr = styling.building_color_expression()
    check("function default is CASE", expr.startswith("CASE"))
    check("maps residential hex", styling.BUILDING_COLORS["residential"] in expr)
    check("has ELSE fallback", "ELSE" in expr and styling.BUILDING_COLORS["other"] in expr)
    check("references building class column", '"building"' in expr)


def test_building_color_modes():
    print("building colour modes (height/tints)")
    expected = 1 + len(styling._BUILDING_RAMPS)
    check("modes = function + ramps", len(styling.BUILDING_COLOR_MODES) == expected)
    check("function is first", styling.BUILDING_COLOR_MODES[0][0] == styling.BUILDING_COLOR_FUNCTION)
    combo_values = {value for value, _ in styling.BUILDING_COLOR_MODES}
    for mode in styling._BUILDING_RAMPS:
        check(f"{mode} is offered in the combo", mode in combo_values)
        expr = styling.building_color_expression(mode)
        check(f"{mode} ramp uses color_rgb", expr.startswith("color_rgb("))
        check(f"{mode} ramp scales by height", "scale_linear(coalesce" in expr)
    check("hex parse", styling._hex_to_rgb("#ff8000") == (255, 128, 0))
    check("teal swatch is its 2 ramp stops",
          styling.building_color_swatches("teal") == list(styling._BUILDING_RAMPS["teal"]))
    check("function swatch is the use palette",
          styling.building_color_swatches() == list(styling.BUILDING_COLORS.values()))
    check("function base is neutral slate", styling.base_color_hex() == styling._BASE_NEUTRAL)
    teal_base = styling.base_color_hex("teal")
    check("teal base is a hex", teal_base.startswith("#") and len(teal_base) == 7)
    check("teal base differs from neutral", teal_base != styling._BASE_NEUTRAL)
    check("scale_hex darkens", styling._scale_hex("#ffffff", 0.5) == "#7f7f7f")


def test_cache_roundtrip():
    print("overpass cache roundtrip")
    query = "test-query-osm-quick-3d-unique-12345"
    payload = {"elements": [{"type": "node", "id": 1}], "marker": "hello"}
    osm._write_cache(query, payload)
    back = osm._read_cache(query)
    check("reads back what was written", back == payload)
    check("missing query -> None", osm._read_cache("definitely-not-cached-xyz") is None)
    try:
        os.remove(osm._cache_path(query))
    except OSError:
        pass


def test_clear_cache():
    print("clear_cache")
    osm._write_cache("clear-test-a", {"x": 1})
    osm._write_cache("clear-test-b", {"y": 2})
    removed, freed = osm.clear_cache()
    check("removed at least the two written", removed >= 2)
    check("freed some bytes", freed > 0)
    check("cache empty afterwards", osm._read_cache("clear-test-a") is None)


def test_shape_and_base_constants():
    print("shape + base constants")
    check("four area shapes", len(osm.AREA_SHAPES) == 4)
    check("rectangle in shapes", osm.SHAPE_RECTANGLE in osm.AREA_SHAPES)
    check("hexagon in shapes", osm.SHAPE_HEXAGON in osm.AREA_SHAPES)
    check("base depth 5 m", osm.BASE_DEPTH_M == 5.0)
    check("base buffer 5 m", osm.BASE_BUFFER_M == 5.0)


def main():
    for test in (test_parse_osm_number, test_building_levels, test_utm_epsg,
                 test_waterway_width, test_building_color_expression,
                 test_building_color_modes, test_cache_roundtrip,
                 test_clear_cache, test_shape_and_base_constants):
        test()
    print()
    if _failures:
        print(f"FAILED ({len(_failures)}): " + ", ".join(_failures))
        return 1
    print("ALL PASSED")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
