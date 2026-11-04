"""QGIS suite: is the model faithful to the OSM data? (P7, P8, P14)

A synthetic Overpass answer around Izmir is served by a fake server on
127.0.0.1 and goes through the real ``download_osm_for_area``; every check
compares the layers with what OSM says.  Areas of the multipolygons are
checked against an independent UTM projection (Krueger series, not PROJ)
and the shoelace formula.  No public host is contacted and the answer cache
is a new empty folder.

Each check names the 0.51.1 behaviour it rules out; the few marked "kept"
guard behaviour 0.51.1 already had.  Run from the folder that contains the
plugin, with QGIS_CUSTOM_CONFIG_PATH pointing at a throwaway folder:

    python-qgis-ltr.bat -m osm_quick_3d.tests.test_osm_fidelity_qgis   # QGIS 3.44
    python-qgis.bat     -m osm_quick_3d.tests.test_osm_fidelity_qgis   # QGIS 4.2
"""
from __future__ import annotations

import json
import math
import os
import sys
import tempfile
import time
import traceback
from typing import Dict, List

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("NO_PROXY", "127.0.0.1,localhost")

PLUGIN_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if os.path.dirname(PLUGIN_DIR) not in sys.path:
    sys.path.insert(0, os.path.dirname(PLUGIN_DIR))

from qgis.core import (  # noqa: E402
    QgsApplication,
    QgsCoordinateReferenceSystem,
    QgsCoordinateTransform,
    QgsGeometry,
    QgsNetworkAccessManager,
    QgsPointXY,
    QgsProject,
)

LAT0, LON0 = 38.4245, 27.1457      # model centre (Izmir, UTM zone 35N)
EPSG = 32635
RADIUS_M = 250.0
_M_PER_DEG_LAT = 111_000.0          # only to PLACE points; areas are re-projected exactly
_M_PER_DEG_LON = 111_000.0 * math.cos(math.radians(LAT0))


# ------------------------------------------------- independent geometry
def utm_forward(lat: float, lon: float, zone: int = 35):
    """WGS84 -> UTM (Krueger series, n^3 terms): independent of PROJ."""
    a, f, k0 = 6378137.0, 1 / 298.257223563, 0.9996
    n = f / (2 - f)
    big_a = a / (1 + n) * (1 + n ** 2 / 4 + n ** 4 / 64)
    alpha = (0.0, n / 2 - 2 * n ** 2 / 3 + 5 * n ** 3 / 16,
             13 * n ** 2 / 48 - 3 * n ** 3 / 5, 61 * n ** 3 / 240)
    e = math.sqrt(f * (2 - f))
    lon0 = math.radians((zone - 1) * 6 - 180 + 3)
    phi, lam = math.radians(lat), math.radians(lon) - lon0
    t = math.sinh(math.atanh(math.sin(phi)) - e * math.atanh(e * math.sin(phi)))
    xi_p = math.atan2(t, math.cos(lam))
    eta_p = math.atanh(math.sin(lam) / math.sqrt(1 + t * t))
    xi = xi_p + sum(alpha[j] * math.sin(2 * j * xi_p) * math.cosh(2 * j * eta_p) for j in (1, 2, 3))
    eta = eta_p + sum(alpha[j] * math.cos(2 * j * xi_p) * math.sinh(2 * j * eta_p) for j in (1, 2, 3))
    return 500000.0 + k0 * big_a * eta, k0 * big_a * xi


def ring_area(ring) -> float:
    """Shoelace area in m² of a closed lat/lon ring, projected independently."""
    points = [utm_forward(p["lat"], p["lon"]) for p in ring[:-1]]
    total = 0.0
    for (x1, y1), (x2, y2) in zip(points, points[1:] + points[:1]):
        total += x1 * y2 - x2 * y1
    return abs(total) / 2.0


def ll(dx: float, dy: float) -> Dict[str, float]:
    return {"lat": round(LAT0 + dy / _M_PER_DEG_LAT, 9), "lon": round(LON0 + dx / _M_PER_DEG_LON, 9)}


def rect(cx: float, cy: float, w: float, h: float) -> List[Dict[str, float]]:
    """A closed counter-clockwise ring, w x h metres centred cx, cy metres from the centre."""
    ring = [ll(cx - w / 2, cy - h / 2), ll(cx + w / 2, cy - h / 2),
            ll(cx + w / 2, cy + h / 2), ll(cx - w / 2, cy + h / 2)]
    return ring + [ring[0]]


def way(wid: int, ring, tags: dict) -> dict:
    return {"type": "way", "id": wid, "geometry": ring, "tags": tags}


def line(wid: int, x0: float, x1: float, y: float, tags: dict) -> dict:
    return {"type": "way", "id": wid, "geometry": [ll(x0, y), ll(x1, y)], "tags": tags}


def node(nid: int, dx: float, dy: float, tags: dict) -> dict:
    point = ll(dx, dy)
    return {"type": "node", "id": nid, "lat": point["lat"], "lon": point["lon"], "tags": tags}


def member(ref: int, role: str, geometry) -> dict:
    return {"type": "way", "ref": ref, "role": role, "geometry": geometry}


# ------------------------------------------------------------- fixture
COURT_OUTER = rect(60, -60, 40, 40)
COURT_INNER = rect(60, -60, 16, 16)
PARK_OUTER = rect(-80, -90, 120, 120)
PARK_HOLE = rect(-80, -90, 60, 60)
PARK_ISLAND = rect(-80, -90, 20, 20)
SQUARE = rect(120, 60, 30, 30)
# An L-shaped outline (60 x 60 with a 40 x 40 notch), offset to (100, -160) metres.
_L = [(0, 0), (60, 0), (60, 20), (20, 20), (20, 60), (0, 60)]
L_RING = [ll(100 + x, -160 + y) for x, y in _L] + [ll(100, -160)]

# osm id -> (tags, expected building_levels, height, min_height)
BUILDINGS = {
    101: ({"building": "apartments", "building:levels": "5", "roof:levels": "1"}, (6, None, 0.0)),
    102: ({"building": "yes", "height": "45 ft"}, (5, 13.7, 0.0)),
    103: ({"building": "house", "building:levels": "2;3"}, (2, None, 0.0)),
    104: ({"building": "roof", "min_height": "3", "height": "5"}, (2, 5.0, 3.0)),
    105: ({"building": "yes", "building:levels": "2.5"}, (3, None, 0.0)),
    106: ({"building": "house"}, (2, None, 0.0)),
    107: ({"building": "yes", "height": "12,5 m"}, (4, 12.5, 0.0)),
}
_SPOTS = [(-180 + 40 * (i % 9), 40 + 30 * (i // 9)) for i in range(len(BUILDINGS))]

# osm id -> (tags, expected building_levels, height, min_height)
ABSURD = {
    150: ({"building": "yes", "building:levels": "1e10"}, (200, None, 0.0)),
    151: ({"building": "yes", "building:levels": "inf"}, (3, None, 0.0)),
    152: ({"building": "yes", "height": "9" * 400}, (3, None, 0.0)),
    153: ({"building": "yes", "building:levels": "3000000000"}, (200, None, 0.0)),
    154: ({"building": "yes", "height": "99999"}, (200, 1000.0, 0.0)),
    155: ({"building": "yes", "height": "-5"}, (3, None, 0.0)),
    156: ({"building": "yes", "height": "nan"}, (3, None, 0.0)),
    157: ({"building": "yes", "building:levels": "5", "roof:levels": "1e9"}, (25, None, 0.0)),
    158: ({"building": "yes", "min_height": "1e12", "height": "20"}, (7, 20.0, 1000.0)),
}


def main_elements() -> list:
    elements = []
    for (wid, (tags, _expected)), (cx, cy) in zip(BUILDINGS.items(), _SPOTS):
        elements.append(way(wid, rect(cx, cy, 12, 10), tags))
    elements.append(way(190, rect(150, 150, 10, 10)[:-2], {"building": "yes"}))    # open way
    outer = COURT_OUTER[:-1]
    elements.append({"type": "relation", "id": 201, "tags": {"type": "multipolygon", "building": "yes"},
                     "members": [member(9001, "outer", outer[0:3]),
                                 member(9002, "outer", [outer[2], outer[3], outer[0]][::-1]),
                                 member(9003, "inner", COURT_INNER)]})
    elements.append({"type": "relation", "id": 202,
                     "tags": {"type": "multipolygon", "leisure": "park", "name": "Kültürpark"},
                     "members": [member(9101, "outer", PARK_OUTER),
                                 member(9102, "inner", PARK_HOLE),
                                 member(9103, "outer", PARK_ISLAND)]})
    # A square outline delivered as four two-point ways, one of them reversed.
    sq = SQUARE[:-1]
    elements.append({"type": "relation", "id": 203, "tags": {"type": "multipolygon", "building": "yes"},
                     "members": [member(9201, "outer", [sq[0], sq[1]]),
                                 member(9202, "outer", [sq[2], sq[1]]),
                                 member(9203, "outer", [sq[2], sq[3]]),
                                 member(9204, "outer", [sq[3], sq[0]])]})
    # The L outline split where the chord between the pieces runs outside it.
    elements.append({"type": "relation", "id": 204, "tags": {"type": "multipolygon", "landuse": "grass"},
                     "members": [member(9301, "outer", [L_RING[4], L_RING[5], L_RING[0], L_RING[1], L_RING[2]]),
                                 member(9302, "outer", [L_RING[2], L_RING[3], L_RING[4]])]})
    elements += [
        node(301, 10, 10, {"natural": "tree", "height": "8"}),
        node(302, 20, 12, {"natural": "tree", "height": "9" * 400}),
        line(401, -200, 200, 5, {"highway": "residential", "name": "Atatürk Caddesi", "width": "7"}),
        line(402, -100, 100, -170, {"highway": "proposed", "proposed": "secondary"}),
        line(403, -100, 100, -180, {"highway": "residential", "tunnel": "yes", "layer": "-1"}),
        line(404, -100, 100, -190, {"highway": "abandoned"}),
        line(405, -100, 100, -200, {"highway": "construction", "construction": "primary"}),
        line(407, -100, 100, -150, {"highway": "service", "tunnel": "culvert"}),
        line(408, 100, 200, -20, {"highway": "service", "tunnel": "building_passage"}),
        line(409, 100, 200, -40, {"highway": "residential", "bridge": "yes", "layer": "1"}),
        line(410, 100, 200, -50, {"highway": "cycleway", "tunnel": "yes"}),
        line(411, 100, 200, -80, {"highway": "service", "width": "9" * 400}),
        line(501, -200, 200, 120, {"waterway": "stream"}),
        line(502, -200, 200, 130, {"waterway": "stream", "tunnel": "culvert"}),
    ]
    return elements


def absurd_elements() -> list:
    # The 1e10 building comes first: in 0.51.1 the provider dropped every
    # building after it.
    elements = []
    for index, (wid, (tags, _expected)) in enumerate(ABSURD.items()):
        elements.append(way(wid, rect(-160 + 40 * (index % 8), 160 - 30 * (index // 8), 12, 10), tags))
    for (wid, (tags, _expected)), (cx, cy) in zip(BUILDINGS.items(), _SPOTS):
        if wid in (101, 102):
            elements.append(way(wid, rect(cx, cy, 12, 10), tags))
    return elements


# --------------------------------------------------------------- checks
class Checks:
    def __init__(self) -> None:
        self.results: List[bool] = []

    def ok(self, name: str, condition, detail: str = "") -> bool:
        tag = "PASS" if condition else "FAIL"
        print(f"  [{tag}] {name}" + (f" - {detail}" if detail else ""), flush=True)
        self.results.append(bool(condition))
        return bool(condition)


def study_area() -> QgsGeometry:
    to_utm = QgsCoordinateTransform(QgsCoordinateReferenceSystem.fromEpsgId(4326),
                                    QgsCoordinateReferenceSystem.fromEpsgId(EPSG), QgsProject.instance())
    centre = to_utm.transform(QgsPointXY(LON0, LAT0))
    return QgsGeometry.fromPointXY(centre).buffer(RADIUS_M, 64)


def features(layer) -> Dict[str, object]:
    return {str(feature["osm_id"]): feature for feature in layer.getFeatures()}


def nullable(value):
    """A QGIS NULL (QVariant on Qt 5, None on Qt 6) as None."""
    if value is None:
        return None
    if hasattr(value, "isNull") and value.isNull():
        return None
    return value


def build(module, servers, elements):
    from osm_quick_3d.tests._offline import forget_servers

    forget_servers()
    servers.elements = list(elements)
    servers.set("a", "ok")
    return module.download_osm_for_area(study_area(), EPSG, use_cache=False)


def check_heights(c: Checks, osm: dict) -> None:
    buildings = features(osm["buildings"])
    reasons = {101: "levels + roof:levels (kept)", 102: "a height in feet (0.51.1: absent)",
               103: "'2;3' takes the first value (kept)", 104: "min_height is the base (kept)",
               105: "2.5 levels round half up to 3 (0.51.1: 2)", 106: "the default for house (kept)",
               107: "comma decimal (0.51.1: absent)"}
    for wid, (tags, expected) in BUILDINGS.items():
        feature = buildings.get(str(wid))
        got = None if feature is None else (
            feature["building_levels"], nullable(feature["height"]), feature["min_height"])
        c.ok(f"building {wid}: levels/height/base = {expected} ({reasons[wid]})", got == expected, f"got {got}")


def check_geometry(c: Checks, osm: dict) -> None:
    buildings = features(osm["buildings"])
    greens = features(osm["greens"])
    c.ok("an open building way is skipped, not closed into an invented polygon "
         "(0.51.1 closed it)", "190" not in buildings, "present" if "190" in buildings else "absent")
    want = ring_area(COURT_OUTER) - ring_area(COURT_INNER)
    got = buildings["201"].geometry().area() if "201" in buildings else None
    c.ok("courtyard relation: split and reversed outer ways joined, the inner ring is a hole (kept)",
         got is not None and abs(got - want) < 0.01 * want, f"{got} vs independent {want:.1f} m²")
    want = ring_area(PARK_OUTER) - ring_area(PARK_HOLE) + ring_area(PARK_ISLAND)
    lost = ring_area(PARK_OUTER) - ring_area(PARK_HOLE)
    got = greens["202"].geometry().area() if "202" in greens else None
    c.ok("a multipolygon island inside a hole is kept (0.51.1 subtracted it with the hole)",
         got is not None and abs(got - want) < 0.01 * want,
         f"{got if got is None else round(got, 1)} vs independent {want:.1f} m² (island lost: {lost:.1f})")
    want = ring_area(SQUARE)
    got = buildings["203"].geometry().area() if "203" in buildings else None
    c.ok("an outline made of two-point ways is joined into one ring (0.51.1 dropped the building)",
         got is not None and abs(got - want) < 0.01 * want,
         f"{got if got is None else round(got, 1)} vs independent {want:.1f} m²")
    want = ring_area(L_RING)
    notch = ring_area([L_RING[2], L_RING[3], L_RING[4], L_RING[2]])
    got = greens["204"].geometry().area() if "204" in greens else None
    c.ok("a concave outline split into open ways keeps its notch (0.51.1 closed each piece and "
         "filled the notch)", got is not None and abs(got - want) < 0.01 * want,
         f"{got if got is None else round(got, 1)} vs independent {want:.1f} m² "
         f"(notch filled: {want + notch:.1f})")
    if "201" in buildings:
        footprint = buildings["201"]["footprint_m2"]
        c.ok("footprint_m2 is the assembled area (kept)",
             abs(footprint - buildings["201"].geometry().area()) < 0.2, str(footprint))


def check_ways(c: Checks, osm: dict) -> None:
    roads = features(osm["roads"])
    bikes = features(osm["bikelanes"])
    water = features(osm["waterlines"])
    gone = [wid for wid in ("402", "404", "405") if wid in roads]
    c.ok("highway=proposed/construction/abandoned is not drawn as a road (0.51.1 drew them)",
         not gone, f"drawn: {gone}")
    tunnels = [wid for wid in ("403", "407") if wid in roads] + (["410"] if "410" in bikes else [])
    c.ok("tunnel=yes/culvert is not drawn as a surface road or bike lane (0.51.1 drew them)",
         not tunnels, f"drawn: {tunnels}")
    c.ok("a building passage, a bridge and an ordinary street stay roads (kept)",
         all(wid in roads for wid in ("401", "408", "409")), str(sorted(roads)))
    c.ok("a stream in a culvert is not drawn on the surface; an open stream is (0.51.1 drew both)",
         "501" in water and "502" not in water, str(sorted(water)))
    width = nullable(roads["411"]["width"]) if "411" in roads else "missing"
    c.ok("an absurd width tag is dropped, not written as infinity (0.51.1 wrote inf)",
         width is None, str(width))
    trees = {str(f["osm_id"]): f["height"] for f in osm["trees"].getFeatures()
             if not str(f["osm_id"]).startswith("scatter_")}
    c.ok("a tree height that is not a finite number falls back to 6 m (0.51.1: whole download failed)",
         trees.get("301") == 8.0 and trees.get("302") == 6.0, str(trees))


def check_absurd(c: Checks, module, servers) -> None:
    try:
        osm = build(module, servers, absurd_elements())
        error = ""
    except Exception as exc:  # noqa: BLE001 - the check reports it
        osm, error = None, f"{type(exc).__name__}: {exc}"
    expected_count = len(ABSURD) + 2
    c.ok("absurd level and height tags never sink the download (0.51.1 raised OverflowError "
         "for the whole area)", osm is not None, error[:200])
    if osm is None:
        return
    layer = osm["buildings"]
    c.ok("every building after a levels=1e10 one is kept (0.51.1's provider dropped them)",
         layer.featureCount() == expected_count and osm["counts"]["buildings"] == expected_count,
         f"{layer.featureCount()} in the layer, {osm['counts']['buildings']} counted, {expected_count} expected")
    buildings = features(layer)
    for wid, (tags, expected) in ABSURD.items():
        feature = buildings.get(str(wid))
        got = None if feature is None else (
            feature["building_levels"], nullable(feature["height"]), feature["min_height"])
        shown = {key: (text if len(text) < 14 else text[:6] + "…") for key, text in tags.items()}
        c.ok(f"building {wid} {shown}: {expected} (levels 1-200, heights 1-1000 m)",
             got == expected, f"got {got}")
    with tempfile.TemporaryDirectory(prefix="oq3d-fidelity-") as folder:
        path = os.path.join(folder, "buildings.geojson")
        module.save_layer_to_geojson(layer, path)
        with open(path, encoding="utf-8") as handle:
            text = handle.read()

        def refuse(constant):
            raise ValueError(constant)

        try:
            document = json.loads(text, parse_constant=refuse)
            props = [feature["properties"] for feature in document["features"]]
            exported = all(isinstance(p.get("building_levels"), int) and 1 <= p["building_levels"] <= 200
                           and (p.get("gfa_m2") is None or math.isfinite(p["gfa_m2"])) for p in props)
        except (ValueError, KeyError, TypeError) as exc:
            exported, props = False, [str(exc)]
    c.ok("the exported GeoJSON is strict JSON with finite, capped values on every building",
         exported, f"{len(props)} buildings")


def check_empty(c: Checks, module, servers) -> None:
    from osm_quick_3d.tests._offline import forget_servers

    forget_servers()
    module.clear_cache()            # the isolated cache holds the earlier good answers
    servers.set("a", "empty")
    servers.take_hits()
    message = ""
    for _attempt in range(2):
        try:
            module.download_osm_for_area(study_area(), EPSG, use_cache=True)
        except module.OsmDownloadError as error:
            message = str(error)
    hits = servers.take_hits()
    c.ok("an empty answer stops the build in plain words (0.51.1: 'Overpass returned 0 elements')",
         message.startswith("OpenStreetMap has none of the selected map data in this area")
         and "Overpass" not in message, message[:120])
    c.ok("an empty answer is never cached: the second run asks again (0.51.1 replayed it for a week)",
         len(hits) == 2, f"{len(hits)} requests for two runs")


# ------------------------------------------------------------------ main
def run_all() -> bool:
    from osm_quick_3d.tests._offline import (FakeOverpass, allow_loopback_http, cleanup,
                                             isolate_network, refused)

    isolate_network()
    from osm_quick_3d import osm_download

    print("=" * 62)
    print(" osm_quick_3d - model fidelity (P7, P8, P14)")
    print("=" * 62, flush=True)
    if allow_loopback_http(osm_download):
        print("  (a build without loopback support: plain HTTP to 127.0.0.1 is let through)")
    QgsNetworkAccessManager.setTimeout(8000)
    servers = FakeOverpass(names=("a",))
    osm_download.OVERPASS_ENDPOINTS = (servers.url("a"),)
    c = Checks()
    started = time.monotonic()
    osm = None
    try:
        osm = build(osm_download, servers, main_elements())
    except Exception as error:  # noqa: BLE001 - reported as a failure
        traceback.print_exc()
        c.ok("the fixture builds", False, f"{type(error).__name__}: {error}")
    if osm is not None:
        for step in (check_heights, check_geometry, check_ways):
            try:
                step(c, osm)
            except Exception as error:  # noqa: BLE001 - a crashed step is a failed step
                traceback.print_exc()
                c.ok(f"{step.__name__} ran to the end", False, f"{type(error).__name__}: {error}")
    for step in (check_absurd, check_empty):
        try:
            step(c, osm_download, servers)
        except Exception as error:  # noqa: BLE001 - a crashed step is a failed step
            traceback.print_exc()
            c.ok(f"{step.__name__} ran to the end", False, f"{type(error).__name__}: {error}")
    c.ok("no request left this computer (guard)", not refused(), str(refused()[:3]))
    cleanup()
    passed = sum(1 for result in c.results if result)
    print("-" * 62)
    print(f"  {passed}/{len(c.results)} passed in {time.monotonic() - started:.1f} s")
    return passed == len(c.results)


def main() -> None:
    profile = tempfile.TemporaryDirectory(prefix="osm_quick_3d-fidelity-")
    app = QgsApplication([], False, profile.name, "external")
    app.initQgis()
    ok = False
    try:
        ok = run_all()
    except Exception as error:  # noqa: BLE001 - a crash is a failed suite
        traceback.print_exc()
        print(f"  [FAIL] {type(error).__name__}: {error}")
    sys.stdout.flush()
    sys.stderr.flush()
    import shutil

    shutil.rmtree(profile.name, ignore_errors=True)     # os._exit skips its cleanup
    os._exit(0 if ok else 1)


if __name__ == "__main__":
    main()
