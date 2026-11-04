# -*- coding: utf-8 -*-
"""Headless tests for the pure-Python logic of OSM Quick 3D.

The plugin's heavy lifting depends on QGIS, which can't be imported outside a
QGIS Python environment. But a meaningful slice is plain Python — OSM tag
parsing, floor-count inference, the UTM-zone formula, the building-colour
expression builder and the Overpass disk cache — and that slice is exactly the
part most prone to silent regressions across releases.

This harness injects lightweight stub modules for ``qgis`` / ``qgis.PyQt`` so the
modules import, then exercises the pure functions. No QGIS, no network.  The
Overpass rules (``net/``) and the part-by-part download loop are tested with a
scripted ``_post_once``; geometry needs real QGIS and is tested in
``test_osm_fidelity_qgis``.  The answer cache is pointed at a new empty
folder first, so the user's real cache is never read, written or cleared.

Run:  py -3 tests/test_pure_logic.py   (from the plugin dir)
"""
from __future__ import annotations

import os
import shutil
import sys
import tempfile
import types

PLUGIN_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


# ── qgis stubs ───────────────────────────────────────────────────────────────
class _MetaAnything(type):
    def __getattr__(cls, name):
        return _Anything


class _Anything(metaclass=_MetaAnything):
    """A class whose every attribute/call returns another harmless stub."""

    def __init__(self, *args, **kwargs):
        pass

    def __getattr__(self, name):
        if name == "isEmpty":
            return lambda: False
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
                 "qgis.PyQt.QtNetwork",
                 "qgis._3d"):
        sys.modules.setdefault(name, _StubModule(name))
    # QVariant needs concrete-looking type attributes for the field definitions.
    qvariant = type("QVariant", (), {"String": 10, "Int": 2, "Double": 6})
    sys.modules["qgis.PyQt.QtCore"].QVariant = qvariant


_install_qgis_stubs()
# Import the plugin as a package (osm_download uses relative imports of net/)
# without running its __init__, which would import the whole QGIS GUI side.
_PACKAGE = "osm_quick_3d"
if _PACKAGE not in sys.modules:
    _package = types.ModuleType(_PACKAGE)
    _package.__path__ = [PLUGIN_DIR]
    sys.modules[_PACKAGE] = _package

from osm_quick_3d import osm_download as osm  # noqa: E402
from osm_quick_3d import styling  # noqa: E402
from osm_quick_3d import native3d  # noqa: E402  (module-level has no qgis imports)
try:
    from osm_quick_3d.net import hosts as net_hosts  # noqa: E402
    from osm_quick_3d.net import transport as net_transport  # noqa: E402
except ImportError:  # 0.51.1 has no net/: its checks fail instead of crashing
    net_hosts = net_transport = None

#: The user's real cache folder; no check may touch it.
_REAL_CACHE = os.path.join(tempfile.gettempdir(), "osm_quick_3d_cache")
# The system temp folder itself is redirected too: a build without
# CACHE_DIR (0.51.1) keeps its cache there, and its clear_cache() would wipe
# the user's copies.
_SANDBOX = tempfile.mkdtemp(prefix="osm_quick_3d-pure-")
tempfile.tempdir = _SANDBOX
_CACHE = os.path.join(_SANDBOX, "cache")
osm.CACHE_DIR = _CACHE


# ── tiny assert harness ──────────────────────────────────────────────────────
_failures = []


def check(label, condition, detail=""):
    status = "ok  " if condition else "FAIL"
    if not condition:
        _failures.append(label)
    print(f"  [{status}] {label}" + (f" - {detail}" if detail and not condition else ""))


def _safe(job):
    """The result of `job()`, or the exception it raised (a failed check, not a crash)."""
    try:
        return job()
    except Exception as error:  # noqa: BLE001 - reported through the check
        return error


# ── tests ────────────────────────────────────────────────────────────────────
def test_parse_osm_number():
    print("_parse_osm_number")
    check("plain int", osm._parse_osm_number("12") == 12.0)
    check("decimal", osm._parse_osm_number("12.5") == 12.5)
    check("metres suffix", osm._parse_osm_number("12 m") == 12.0)
    check("semicolon list takes first", osm._parse_osm_number("3;4") == 3.0)
    check("None -> None", osm._parse_osm_number(None) is None)
    check("garbage -> None", osm._parse_osm_number("abc") is None)
    check("'inf' is absent (0.51.1 returned inf)", osm._parse_osm_number("inf") is None)
    check("'nan' is absent", osm._parse_osm_number("nan") is None)
    check("400 nines is absent (0.51.1 returned inf)", osm._parse_osm_number("9" * 400) is None)
    check("1e10 parses finite", osm._parse_osm_number("1e10") == 1e10)
    check("feet are converted", abs((osm._parse_osm_number("45 ft") or 0) - 13.716) < 1e-9)
    check("comma decimal", osm._parse_osm_number("12,5 m") == 12.5)


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
    check("levels=inf: no OverflowError, the type default (0.51.1 raised)",
          _safe(lambda: osm._building_levels({"building": "yes", "building:levels": "inf"})) == 3)
    check("levels=1e10 capped at 200 (0.51.1 overflowed the Int field)",
          _safe(lambda: osm._building_levels({"building:levels": "1e10"})) == 200)
    check("levels + roof:levels capped at 200",
          _safe(lambda: osm._building_levels({"building:levels": "199", "roof:levels": "1e9"})) == 200)
    check("height of 400 nines: the type default (0.51.1 raised)",
          _safe(lambda: osm._building_levels({"building": "house", "height": "9" * 400})) == 2)
    check("height 99999 m: levels capped at 200",
          _safe(lambda: osm._building_levels({"height": "99999"})) == 200)
    check("building height capped at 1000 m",
          _safe(lambda: osm._building_height({"height": "99999"})) == 1000.0)
    check("building height below 1 m raised to 1 m",
          _safe(lambda: osm._building_height({"height": "0.2"})) == 1.0)
    check("negative height is absent", _safe(lambda: osm._building_height({"height": "-5"})) is None)
    check("min_height capped at 1000 m", osm._building_min_height({"min_height": "1e9"}) == 1000.0)
    check("min_level=inf is absent", osm._building_min_height({"building:min_level": "inf"}) == 0.0)
    check("road width capped at 100 m", _safe(lambda: osm._road_width({"width": "1e9"})) == 100.0)
    check("road width of 400 nines is absent", _safe(lambda: osm._road_width({"width": "9" * 400})) is None)
    check("tree height falls back to 6 m", _safe(lambda: osm._tree_height({"height": "9" * 400})) == 6.0)


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
    check("absurd width capped at 1000 m", osm._waterway_width({"width": "1e9"}) == 1000.0)


def test_surface_ways():
    print("tunnels and unbuilt roads (P14)")
    drawn = getattr(osm, "_is_drawn_highway", lambda _tags: True)
    for value in ("proposed", "construction", "abandoned", "razed", "demolished"):
        check(f"highway={value} is not drawn", not drawn({"highway": value}))
    check("tunnel=yes is not drawn", not drawn({"highway": "primary", "tunnel": "yes"}))
    check("tunnel=culvert is not drawn", not drawn({"highway": "service", "tunnel": "culvert"}))
    check("building passage stays", drawn({"highway": "service", "tunnel": "building_passage"}))
    check("bridge stays", drawn({"highway": "residential", "bridge": "yes"}))
    check("ordinary street stays", drawn({"highway": "residential"}))


def test_building_color_expression():
    print("building_color_expression")
    expr = styling.building_color_expression()
    check("function default is CASE", expr.startswith("CASE"))
    check("maps residential hex", styling.BUILDING_COLORS["residential"] in expr)
    check("has ELSE fallback", "ELSE" in expr and styling.BUILDING_COLORS["other"] in expr)
    check("references building class column", '"building"' in expr)

    expr_discrete = styling.building_color_expression("teal", classification="discrete")
    check("discrete ramp uses CASE", expr_discrete.startswith("CASE"))
    check("discrete ramp contains WHEN", "WHEN" in expr_discrete)

    expr_cont = styling.building_color_expression("teal", classification="continuous")
    check("continuous ramp uses color_rgb", expr_cont.startswith("color_rgb("))


def test_road_width_and_themes():
    print("road width + themes")
    check("road width expression uses width field", '"width"' in styling.ROAD_WIDTH_M_EXPR)
    check("road render width clamps minimum", "< 1.5" in styling.ROAD_RENDER_WIDTH_EXPR)
    check("major road default is wide", styling.ROAD_WIDTH_M["major"] > styling.ROAD_WIDTH_M["residential"])
    check("new atlas theme exists", "atlas" in styling.THEMES)
    check("new mediterranean theme exists", "mediterranean" in styling.THEMES)
    check("new nightprint theme exists", "nightprint" in styling.THEMES)


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


def test_cache_isolated():
    print("the user's real cache is never touched (P17)")
    here = os.path.normcase(os.path.abspath(osm._cache_dir()))
    check("the cache folder is this run's own", here == os.path.normcase(os.path.abspath(_CACHE)))
    check("not the real osm_quick_3d_cache in the system temp folder",
          here != os.path.normcase(os.path.abspath(_REAL_CACHE)))


def test_cache_roundtrip():
    print("overpass cache roundtrip")
    query = "test-query-osm-quick-3d-unique-12345"
    payload = {"elements": [{"type": "node", "id": 1}], "marker": "hello"}
    osm._write_cache(query, payload)
    back = osm._read_cache(query)
    check("reads back what was written", back == payload)
    check("missing query -> None", osm._read_cache("definitely-not-cached-xyz") is None)
    osm._write_cache("empty-q", {"elements": []})
    check("an empty answer is never kept (0.51.1 kept it a week)", osm._read_cache("empty-q") is None)
    osm._write_cache("remark-q", {"elements": [{"id": 1}], "remark": "timed out"})
    check("an answer with a remark is never kept", osm._read_cache("remark-q") is None)
    # A 0.51.1 cache file holding an empty answer is discarded, not replayed.
    with open(osm._cache_path("old-empty"), "w", encoding="utf-8") as handle:
        handle.write('{"elements": []}')
    check("an empty saved answer is discarded on read",
          osm._read_cache("old-empty") is None and not os.path.exists(osm._cache_path("old-empty")))
    try:
        os.remove(osm._cache_path(query))
    except OSError:
        pass


def test_overpass_tiling_and_validation():
    print("Overpass tiling + payload validation")
    small = (41.0, 29.0, 41.02, 29.02)
    check("small bbox stays one tile", len(osm.tile_bbox(*small)) == 1)
    city = (40.90, 28.75, 41.15, 29.25)
    tiles = osm.tile_bbox(*city)
    check("large bbox is tiled", len(tiles) > 1)
    check("tile count stays bounded", len(tiles) <= osm.MAX_TILES)
    check("each tile respects request area", all(
        osm.bbox_area_km2(*tile) <= osm.MAX_TILE_AREA_KM2 * 1.001
        for tile in tiles
    ))
    check("tiles preserve outer bbox", (
        min(tile[0] for tile in tiles) == city[0]
        and min(tile[1] for tile in tiles) == city[1]
        and max(tile[2] for tile in tiles) == city[2]
        and max(tile[3] for tile in tiles) == city[3]
    ))
    try:
        osm.tile_bbox(30.0, 20.0, 34.0, 24.0)
        oversized_rejected = False
    except osm.OsmDownloadError:
        oversized_rejected = True
    check("regional extract is rejected", oversized_rejected)
    check("valid payload accepted", osm._validate_payload({"elements": []}) == {"elements": []})
    query = _safe(lambda: osm._overpass_query(*small, datasets=("buildings",), timeout_s=40)) or ""
    check("request claims [timeout:40][maxsize:134217728] (0.51.1: [timeout:60], no maxsize)",
          query.startswith("[out:json][timeout:40][maxsize:134217728];"))
    try:
        osm._validate_payload({"remark": "timed out", "elements": []})
        remark_rejected = False
    except osm.OsmDownloadError:
        remark_rejected = True
    check("Overpass remark is rejected", remark_rejected)
    buildings_query = osm._overpass_query(*small, datasets=("buildings",))
    check("selected dataset is queried", '["building"]' in buildings_query)
    check("unselected roads are omitted", '["highway"]' not in buildings_query)


def test_feature_batch():
    print("provider feature batching")

    class Provider:
        def __init__(self):
            self.calls = []

        def addFeatures(self, features):
            self.calls.append(list(features))

    provider = Provider()
    batch = osm._FeatureBatch(provider)
    batch.addFeatures([1])
    batch.addFeatures([2, 3])
    check("writes are deferred", not provider.calls)
    batch.flush()
    check("one provider call", len(provider.calls) == 1)
    check("features keep order", provider.calls[0] == [1, 2, 3])


def test_transport_rules():
    print("net.transport: the status is read first, plain words (P2, P6)")
    if net_transport is None:
        check("net/transport.py exists (missing in 0.51.1)", False)
        return
    t = net_transport
    check("the answer cut is 64 MiB", t.MAX_RESPONSE_BYTES == 64 * 1024 * 1024)
    check("the request names this plugin's tracker", t.ISSUES_URL.endswith("/osm_quick_3d/issues"))
    qt_error = "Error transferring http://x - server replied: Too Many Requests"
    check("429 is a rate limit even when QGIS reports an error",
          t.classify(status=429, error_code=299, error_text=qt_error).kind == t.RATE)
    check("403 and 406 block", t.classify(status=403).kind == t.BLOCKED and t.classify(status=406).kind == t.BLOCKED)
    check("502 is busy", t.classify(status=502, body=b"busy").kind == t.BUSY)
    check("400 is an invalid request", t.classify(status=400).kind == t.BAD_QUERY)
    check("404 is the server's failure", t.classify(status=404).kind == t.HOST_ERROR)
    check("an empty body with no status is a client timeout", t.classify().kind == t.CLIENT_TIMEOUT)
    check("a dropped connection earns one re-ask", t.classify(error_code=2).retry_same_host)
    check("an oversize transfer is too large", t.classify(oversize=True).kind == t.TOO_LARGE)
    check("a remark is never data", t.classify(status=200, body=b'{"elements": [], "remark": "Query timed out"}').kind
          == t.SERVER_TIMEOUT)
    check("good JSON is data", t.classify(status=200, body=b'{"elements": [{"id": 1}]}').ok)
    check("transfer T = min(55 s, QGIS timeout - 2 s)",
          t.transfer_seconds(60000) == 55.0 and t.transfer_seconds(4000) == 2.0)
    check("[timeout:N] = max(10, T - 15)", t.server_timeout_seconds(55.0) == 40)
    answers = [t.HostAnswer("a.example", t.RATE, "asked this computer to slow down (HTTP 429)"),
               t.HostAnswer("b.example", t.BUSY, "was busy (HTTP 502)")]
    message = t.failure_report(answers, 60).message
    check("the failure message names every server (0.51.1 dropped the first)",
          "a.example: asked" in message and "b.example: was busy" in message)
    check("the failure message uses no jargon or Qt words",
          not [w for w in t.JARGON if w in message.casefold()] and "server replied" not in message)
    note = t.missing_parts_note(2, 6, answers, t.STOP_RESTING)
    check("a partial result names the gap and every server",
          note.startswith("2 of 6 parts of the area could not be downloaded")
          and "a.example" in note and "b.example" in note and "resting" in note)
    check("the gap note uses no jargon", not [w for w in t.JARGON if w in note.casefold()])
    check("nothing missing, no note", t.missing_parts_note(0, 6) == "")


class _Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


def test_host_board():
    print("net.hosts: rests, one request per server, budget (P2)")
    if net_hosts is None:
        check("net/hosts.py exists (missing in 0.51.1)", False)
        return
    h, t = net_hosts, net_transport
    clock, wall = _Clock(), _Clock()
    board = h.HostBoard(clock=clock, wall=wall, spacing_s=0)
    check("429 rests 60 s", board.strike("a", t.RATE) == 60.0)
    check("403/406 rests an hour", board.strike("b", t.BLOCKED) == 3600.0)
    check("5xx rests 45 s", board.strike("c", t.BUSY) == 45.0)
    check("a timeout rests 120 s, doubling, capped",
          board.strike("d", t.CLIENT_TIMEOUT) == 120.0 and board.strike("d", t.CLIENT_TIMEOUT) == 240.0
          and h.cooldown_seconds(t.CLIENT_TIMEOUT, 30) == 900.0)
    check("a resting server is not offered", board.order(("a", "e")) == ("e",))
    snap = board.snapshot()
    again = h.HostBoard(clock=clock, wall=wall)
    again.restore(snap)
    check("rests survive a restart (snapshot/restore)", 59 <= again.remaining("a") <= 60)
    check("garbage in the stored rests is ignored", _safe(lambda: again.restore({"hosts": {"x": "y"}})) is None)

    calls = []

    def attempt(host):
        calls.append(host)
        return t.Verdict(t.RATE, "asked this computer to slow down (HTTP 429)")

    fresh_board = h.HostBoard(clock=clock, wall=wall, spacing_s=0)
    result = h.ask_hosts(("a", "b"), attempt, fresh_board, lambda *_a: None)
    check("429 everywhere: one request per server, no retry (0.51.1: two each)",
          calls == ["a", "b"] and result.kind == h.LOST)
    calls.clear()
    budget = h.RequestBudget(1)
    result = h.ask_hosts(("x", "y"), attempt, h.HostBoard(clock=clock, wall=wall, spacing_s=0),
                         lambda *_a: None, budget=budget)
    check("the click budget stops requests", calls == ["x"] and result.kind == h.NO_BUDGET)
    check("one click may send at most 64 requests", h.MAX_CLICK_REQUESTS == 64)


class _Feedback:
    def __init__(self, cancelled=False):
        self.cancelled = cancelled

    def isCanceled(self):
        return self.cancelled


def _scripted_click(script, bbox, use_cache=False, status=None, feedback=None):
    """One click through osm._fetch with `_post_once` scripted per server."""
    calls = []
    hosts = ("a.example", "b.example")

    def post_once(url, body, transfer_s, feedback=None):
        host = url.split("/")[2]
        calls.append(host)
        return script(host, len(calls))

    saved = (osm._post_once, osm.OVERPASS_ENDPOINTS, osm.BOARD)
    osm._post_once = post_once
    osm.OVERPASS_ENDPOINTS = tuple(f"https://{host}/api/interpreter" for host in hosts)
    osm.BOARD = net_hosts.HostBoard(spacing_s=0)
    try:
        result = _safe(lambda: osm.fetch_overpass_ex(*bbox, use_cache=use_cache, status=status,
                                                     feedback=feedback))
    finally:
        osm._post_once, osm.OVERPASS_ENDPOINTS, osm.BOARD = saved
    return result, calls


def test_part_loop():
    print("the part-by-part download: stop early, budget, gaps in notes (P2, P3, C1)")
    if net_hosts is None or not hasattr(osm, "fetch_overpass_ex"):
        check("fetch_overpass_ex exists (missing in 0.51.1)", False)
        return
    t = net_transport
    big = (40.90, 28.75, 41.15, 29.25)          # Istanbul: many parts
    parts = len(osm.tile_bbox(*big))
    small = (38.42, 27.14, 38.429, 27.1515)
    osm.clear_cache()

    def ok(_host, _n):
        return t.Verdict(t.OK, "delivered the data", {"elements": [{"type": "node", "id": _n}]})

    def busy(_host, _n):
        return t.Verdict(t.BUSY, "was busy (HTTP 502)")

    result, calls = _scripted_click(busy, big)
    check(f"every server busy on part 1 of {parts}: two requests in all, then the click stops "
          f"(0.51.1: up to {6 * parts})", len(calls) == 2 and isinstance(result, osm.OsmDownloadError))
    message = str(result)
    check("the failure names both servers in plain words",
          "a.example" in message and "b.example" in message and "Nothing was downloaded." in message)

    def first_ok_then_busy(_host, n):
        return ok(_host, n) if n == 1 else busy(_host, n)

    result, calls = _scripted_click(first_ok_then_busy, big)
    download = result.get("_download", {}) if isinstance(result, dict) else {}
    notes = download.get("notes") or []
    check("part 1 delivered, then both servers busy: 3 requests, a partial result",
          len(calls) == 3 and download.get("tiles_succeeded") == 1
          and len(download.get("tiles_failed") or ()) == parts - 1, f"{len(calls)} {download}")
    check("the partial result carries a note naming the gap and the servers",
          len(notes) == 1 and f"{parts - 1} of {parts} parts" in notes[0] and "a.example" in notes[0]
          and "resting" in notes[0])

    def always_ok(host, n):
        return ok(host, n)

    osm.clear_cache()
    saved_limit = osm.MAX_CLICK_REQUESTS
    osm.MAX_CLICK_REQUESTS = 5
    try:
        result, calls = _scripted_click(always_ok, big)
    finally:
        osm.MAX_CLICK_REQUESTS = saved_limit
    download = result.get("_download", {}) if isinstance(result, dict) else {}
    check("the click budget caps the requests and the gap is reported",
          len(calls) == 5 and len(download.get("tiles_failed") or ()) == parts - 5
          and "all 5 of its requests" in " ".join(download.get("notes") or ()), f"{len(calls)} {download}")
    result, calls = _scripted_click(always_ok, big, use_cache=True)
    download = result.get("_download", {}) if isinstance(result, dict) else {}
    check("a re-run takes the saved parts from the cache and asks only for the missing ones",
          len(calls) == parts - 5 and download.get("tiles_cached") == 5 and not download.get("tiles_failed"),
          f"{len(calls)} {download}")
    osm.clear_cache()

    def empty(_host, _n):
        return t.Verdict(t.OK, "delivered the data", {"elements": []})

    result, calls = _scripted_click(empty, small, use_cache=True)
    check("an empty answer is used once ...", isinstance(result, dict) and result.get("elements") == [])
    result, calls = _scripted_click(empty, small, use_cache=True)
    check("... and never cached: the next click asks again (0.51.1 replayed it a week)", calls == ["a.example"])

    lines = []
    result, calls = _scripted_click(ok, small, status=lines.append)
    check("status sentences are plain and end with the server that delivered",
          lines and lines[-1] == "a.example delivered the data.")
    result, calls = _scripted_click(ok, small, feedback=_Feedback(True))
    check("a cancelled feedback raises 'Download cancelled.' and sends nothing",
          isinstance(result, osm.OsmDownloadCancelled) and str(result) == "Download cancelled." and not calls)
    check("OsmCancelledError (0.51 name) still catches a cancel",
          issubclass(osm.OsmDownloadCancelled, osm.OsmCancelledError))


def test_contract_c1():
    print("contract C1")
    import inspect

    params = inspect.signature(osm.download_osm_for_area).parameters
    names = list(params)
    check("download_osm_for_area keeps its 0.51 parameters in order",
          names[:6] == ["area_utm", "epsg_dest", "feedback", "use_cache", "datasets", "cancel_check"])
    check("download_osm_for_area takes status as a keyword argument",
          "status" in params and params["status"].default is None)
    cancelled = getattr(osm, "OsmDownloadCancelled", None)
    check("OsmDownloadCancelled(OsmDownloadError) says 'Download cancelled.'",
          cancelled is not None and issubclass(cancelled, osm.OsmDownloadError)
          and str(cancelled()) == "Download cancelled.")
    check("servers: overpass-api.de then overpass.private.coffee, no kumi.systems alias",
          tuple(osm.OVERPASS_ENDPOINTS) == ("https://overpass-api.de/api/interpreter",
                                            "https://overpass.private.coffee/api/interpreter"))
    check("Referer names osm_quick_3d", getattr(osm, "REFERER", "") == "https://github.com/YusufEminoglu/osm_quick_3d")
    check("User-Agent suffix names OSM-Quick-3D and the Referer",
          getattr(osm, "USER_AGENT", "").startswith("OSM-Quick-3D-QGIS-Plugin/")
          and "https://github.com/YusufEminoglu/osm_quick_3d" in getattr(osm, "USER_AGENT", ""))
    check("rests are kept under osm_quick_3d/hosts", getattr(osm, "SETTINGS_GROUP", "") == "osm_quick_3d/hosts")


def test_extrusion_expression():
    print("native3d._extrusion_expression")
    check("scale 1.0 is the bare expression",
          native3d._extrusion_expression(1.0) == native3d.EXTRUSION_EXPRESSION)
    check("default arg is 1.0", native3d._extrusion_expression() == native3d.EXTRUSION_EXPRESSION)
    check("scale 2.0 multiplies", native3d._extrusion_expression(2.0)
          == f"({native3d.EXTRUSION_EXPRESSION}) * 2")
    check("bad scale falls back to bare", native3d._extrusion_expression("x")
          == native3d.EXTRUSION_EXPRESSION)
    check("uses OSM height/levels coalesce", "building_levels" in native3d.EXTRUSION_EXPRESSION)


def test_clear_cache():
    print("clear_cache")
    osm._write_cache("clear-test-a", {"elements": [{"id": 1}]})
    osm._write_cache("clear-test-b", {"elements": [{"id": 2}]})
    removed, freed = osm.clear_cache()
    check("removed at least the two written", removed >= 2)
    check("freed some bytes", freed > 0)
    check("cache empty afterwards", osm._read_cache("clear-test-a") is None)


def test_shape_and_base_constants():
    print("shape + base constants")
    check("five area shapes", len(osm.AREA_SHAPES) == 5)
    check("rectangle in shapes", osm.SHAPE_RECTANGLE in osm.AREA_SHAPES)
    check("hexagon in shapes", osm.SHAPE_HEXAGON in osm.AREA_SHAPES)
    check("polygon in shapes", osm.SHAPE_POLYGON in osm.AREA_SHAPES)
    check("polygon value is 'polygon'", osm.SHAPE_POLYGON == "polygon")
    check("base depth 2 m", osm.BASE_DEPTH_M == 2.0)
    check("base buffer 5 m", osm.BASE_BUFFER_M == 5.0)


def test_themes():
    print("map themes (schema + new themes)")
    required = ("label", "bg", "base", "roads_major", "roads_minor",
                "greens", "water", "trees", "building_ramp", "building_colors")
    functions = ("residential", "commercial", "industrial", "civic", "worship", "other")
    for key, theme in styling.THEMES.items():
        missing = [rk for rk in required if rk not in theme]
        check(f"{key} has all keys", not missing)
        ramp = theme.get("building_ramp")
        check(f"{key} ramp is a 2-tuple", isinstance(ramp, tuple) and len(ramp) == 2)
        bc = theme.get("building_colors", {})
        check(f"{key} has all building functions", all(fn in bc for fn in functions))
    for new_theme in ("anime", "desert", "candy", "vapor"):
        check(f"new theme '{new_theme}' exists", new_theme in styling.THEMES)
        fexpr = styling.building_color_expression("function", theme=new_theme)
        check(f"{new_theme} function expr is CASE", fexpr.startswith("CASE"))
        rexpr = styling.building_color_expression("height", theme=new_theme)
        check(f"{new_theme} height ramp is color_rgb", rexpr.startswith("color_rgb("))
        base = styling.base_color_hex("function", theme=new_theme)
        check(f"{new_theme} base is a 7-char hex", base.startswith("#") and len(base) == 7)


def test_geometry_transform_result_helper():
    print("_geometry_transform_ok")
    check("None counts as success", osm._geometry_transform_ok(None) is True)
    check("0 counts as success", osm._geometry_transform_ok(0) is True)
    check("non-zero int counts as failure", osm._geometry_transform_ok(2) is False)
    check("success enum string counts as success", osm._geometry_transform_ok("GeometryOperationResult.Success") is True)


def test_new_features():
    print("new features (water areas, paved areas, tree scatter, relations)")
    # _is_water_area
    check("water natural", osm._is_water_area({"natural": "water"}) is True)
    check("water riverbank", osm._is_water_area({"waterway": "riverbank"}) is True)
    check("water reservoir", osm._is_water_area({"landuse": "reservoir"}) is True)
    check("water other", osm._is_water_area({"water": "lake"}) is True)
    check("not water", osm._is_water_area({"highway": "residential"}) is False)

    # _is_paved_area
    check("pedestrian area", osm._is_paved_area({"highway": "pedestrian", "area": "yes"}) is True)
    check("place square", osm._is_paved_area({"place": "square"}) is True)
    check("amenity marketplace", osm._is_paved_area({"amenity": "marketplace"}) is True)
    check("not paved", osm._is_paved_area({"highway": "pedestrian"}) is False)

    # _green_scatter_kind
    check("scatter forest", osm._green_scatter_kind({"landuse": "forest"}) == "forest")
    check("scatter park", osm._green_scatter_kind({"leisure": "park"}) == "park")
    check("scatter grass", osm._green_scatter_kind({"natural": "grass"}) == "grass")
    check("no scatter", osm._green_scatter_kind({"landuse": "residential"}) == "")

    # Ring assembly needs real QGIS geometry: see test_osm_fidelity_qgis.

    # Check green colors additions
    check("parking is in green colors", "parking" in styling.GREEN_COLORS)
    check("pedestrian is in green colors", "pedestrian" in styling.GREEN_COLORS)
    check("parking in class expression", "parking" in styling.GREEN_CLASS_EXPR)
    check("pedestrian in class expression", "pedestrian in class expression" and "pedestrian" in styling.GREEN_CLASS_EXPR)


def test_v051_enhancements():
    print("v0.51 enhancements (building:part, min_height, shade variation, 3D sun, cancel)")
    q = osm._overpass_query(41.0, 29.0, 41.01, 29.01, datasets=("buildings",))
    check("building:part queried", '["building:part"]' in q)
    check("min_height parsed", osm._building_min_height({"min_height": "6.5 m"}) == 6.5)
    check("min_level fallback * 3", osm._building_min_height({"building:min_level": "2"}) == 6.0)
    check("absent min_height -> 0.0", osm._building_min_height({}) == 0.0)
    check("building:part type fallback", osm._building_levels({"building:part": "office"}) == 5)

    # New ramps & shade variation
    check("copper_gold ramp exists", "copper_gold" in styling._BUILDING_RAMPS)
    check("nordic_ice ramp exists", "nordic_ice" in styling._BUILDING_RAMPS)
    shade_expr = styling._shade_variant_expression("#d8cfc4")
    check("shade expression uses osm_id bucket", '"osm_id"' in shade_expr and "CASE" in shade_expr)
    check("furniture styles defined", set(styling.FURNITURE_STYLE) == {"busstops", "lights", "benches", "trashbins"})

    # 3D base height & prism height expressions + sun direction
    check("base height expr uses min_height", "min_height" in native3d._base_height_expression(1.0))
    check("base height expr scales", "* 2" in native3d._base_height_expression(2.0))
    check("prism height subtracts min_height", "min_height" in native3d._prism_extrusion_expression(1.0))
    dx, dy, dz = native3d._sun_direction_vector(315.0, 45.0)
    check("sun vector points downward", dz < 0 and abs(dx * dx + dy * dy + dz * dz - 1.0) < 1e-6)

    # Cancel check raises OsmCancelledError before anything is sent.
    sent = []
    saved = osm._post_once
    osm._post_once = lambda *args, **kwargs: sent.append(args)
    try:
        error = _safe(lambda: osm.fetch_overpass(41.0, 29.0, 41.01, 29.01, cancel_check=lambda: True))
    finally:
        osm._post_once = saved
    check("cancel_check raises OsmCancelledError and sends nothing",
          isinstance(error, osm.OsmCancelledError) and not sent)


def main():
    for test in (test_cache_isolated, test_parse_osm_number, test_building_levels, test_utm_epsg,
                 test_waterway_width, test_surface_ways, test_building_color_expression,
                 test_road_width_and_themes, test_building_color_modes, test_extrusion_expression,
                 test_cache_roundtrip, test_overpass_tiling_and_validation,
                 test_feature_batch, test_transport_rules, test_host_board, test_part_loop,
                 test_contract_c1, test_clear_cache,
                 test_shape_and_base_constants, test_themes,
                 test_geometry_transform_result_helper,
                 test_new_features,
                 test_v051_enhancements):
        try:
            test()
        except Exception as error:  # noqa: BLE001 - a crashed test is a failed test
            check(f"{test.__name__} ran to the end ({type(error).__name__}: {error})", False)
    tempfile.tempdir = None
    shutil.rmtree(_SANDBOX, ignore_errors=True)
    print()
    if _failures:
        print(f"FAILED ({len(_failures)}): " + ", ".join(_failures))
        return 1
    print("ALL PASSED")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
