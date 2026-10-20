# -*- coding: utf-8 -*-
"""OpenStreetMap download + clip for the OSM Quick 3D plugin.

Fetches buildings, roads, greens and trees from the public Overpass API for the
bounding box of a study boundary, reprojects to a metric UTM CRS, and clips every
feature to that boundary. Building floor count defaults to 3 when OSM has no level
data, matching the viewer's expectation.
"""
from __future__ import annotations

import contextlib
import hashlib
import json
import math
import os
import tempfile
import time
import urllib.parse

from qgis.PyQt.QtCore import QByteArray, QUrl, QUrlQuery, QVariant
from qgis.PyQt.QtNetwork import QNetworkRequest
from qgis.core import (
    QgsBlockingNetworkRequest,
    QgsCoordinateReferenceSystem,
    QgsCoordinateTransform,
    QgsFeature,
    QgsField,
    QgsGeometry,
    QgsPointXY,
    QgsProject,
    QgsRectangle,
    QgsVectorFileWriter,
    QgsVectorLayer,
)

# Public Overpass mirrors, tried in order. The main instance is frequently rate
# limited (HTTP 429) or slow; falling back to mirrors makes the one-button flow
# far more reliable. The first endpoint that answers with valid JSON wins.
OVERPASS_ENDPOINTS = (
    "https://overpass-api.de/api/interpreter",
    "https://overpass.kumi.systems/api/interpreter",
    "https://overpass.private.coffee/api/interpreter",
)
USER_AGENT = "OSM-Quick-3D-QGIS-Plugin/0.50.0 (https://github.com/YusufEminoglu/osm_quick_3d)"
DEFAULT_TIMEOUT_S = 60
MAX_TILE_AREA_KM2 = 100.0
MAX_JOB_AREA_KM2 = 2500.0
MAX_TILES = 40
MAX_RESPONSE_BYTES = 64 * 1024 * 1024
MAX_ELEMENTS_PER_TILE = 150_000
MAX_TOTAL_ELEMENTS = 400_000
TILE_PAUSE_S = 1.0
MIRROR_RETRY_DELAY_S = 1.2
_KM_PER_DEGREE = 111.32

# Disk cache for Overpass responses. The public API is frequently rate-limited
# (HTTP 429); caching the exact-query JSON means re-running on the same area (or
# nudging a layer toggle) doesn't hit the network again within the TTL.
CACHE_TTL_S = 7 * 24 * 3600  # one week


class OsmDownloadError(RuntimeError):
    pass


class OsmCancelledError(OsmDownloadError):
    pass


def _cache_dir() -> str:
    path = os.path.join(tempfile.gettempdir(), "osm_quick_3d_cache")
    with contextlib.suppress(OSError):
        os.makedirs(path, exist_ok=True)
    return path


def _cache_path(query: str) -> str:
    # SHA-256 purely as a filename-safe digest of the query (not security).
    digest = hashlib.sha256(query.encode("utf-8")).hexdigest()[:40]
    return os.path.join(_cache_dir(), f"{digest}.json")


def _read_cache(query: str):
    """Return cached JSON for this exact query if present and fresh, else None."""
    path = _cache_path(query)
    try:
        if not os.path.isfile(path):
            return None
        if time.time() - os.path.getmtime(path) > CACHE_TTL_S:
            return None
        with open(path, "r", encoding="utf-8") as handle:
            return json.load(handle)
    except (OSError, ValueError):
        return None


def _write_cache(query: str, payload: dict) -> None:
    with contextlib.suppress(OSError, TypeError, ValueError):
        with open(_cache_path(query), "w", encoding="utf-8") as handle:
            json.dump(payload, handle)


def clear_cache():
    """Delete all cached Overpass responses. Returns (files_removed, bytes_freed)."""
    import glob

    removed, freed = 0, 0
    with contextlib.suppress(OSError):
        for path in glob.glob(os.path.join(_cache_dir(), "*.json")):
            with contextlib.suppress(OSError):
                freed += os.path.getsize(path)
                os.remove(path)
                removed += 1
    return removed, freed


# --------------------------------------------------------------------------
# Geo helpers
# --------------------------------------------------------------------------
def utm_epsg_for(lon: float, lat: float) -> int:
    zone = int(math.floor((lon + 180.0) / 6.0) + 1)
    zone = max(1, min(60, zone))
    return (32600 if lat >= 0 else 32700) + zone


# Ground-base geometry, in metres. The base is the study area buffered outward by
# BASE_BUFFER_M and rendered as a slab whose top sits at ground level (0) and whose
# bottom is BASE_DEPTH_M below it, i.e. a recessed plinth the city stands on.
BASE_BUFFER_M = 5.0
BASE_DEPTH_M = 2.0

# Accepted study-area shapes (dialog values).
SHAPE_RECTANGLE = "rectangle"
SHAPE_ROUNDED = "rounded"
SHAPE_CIRCLE = "circle"
SHAPE_HEXAGON = "hexagon"
# "polygon" means: use the user's own selected polygon feature(s) as the exact
# study area. Its geometry comes from the active layer's selection (resolved in
# the plugin class), not from the rectangle, so it is not inscribed in an extent.
SHAPE_POLYGON = "polygon"
AREA_SHAPES = (SHAPE_RECTANGLE, SHAPE_ROUNDED, SHAPE_CIRCLE, SHAPE_HEXAGON, SHAPE_POLYGON)


def shape_study_area(rect: QgsRectangle, shape: str) -> QgsGeometry:
    """Build the study-area polygon for ``shape`` inscribed in ``rect`` (metric CRS).

    ``rectangle`` is the raw extent; ``rounded`` rounds its corners; ``circle`` and
    ``hexagon`` are inscribed in the shorter side so they stay within the extent.
    Everything downstream clips OSM features to this polygon, so the chosen shape
    is what actually gets exported.

    ``polygon`` is not built here — it is the user's own selected feature, resolved
    by the plugin class before clipping — so it falls through to the rectangle as a
    safe fallback if this function is ever called with it directly.
    """
    shape = (shape or SHAPE_RECTANGLE).lower()
    cx = (rect.xMinimum() + rect.xMaximum()) / 2.0
    cy = (rect.yMinimum() + rect.yMaximum()) / 2.0
    radius = min(rect.width(), rect.height()) / 2.0

    if shape == SHAPE_CIRCLE and radius > 0:
        return QgsGeometry.fromPointXY(QgsPointXY(cx, cy)).buffer(radius, 64)

    if shape == SHAPE_HEXAGON and radius > 0:
        points = []
        for i in range(6):
            angle = math.radians(60 * i - 30)  # pointy-top hexagon
            points.append(QgsPointXY(cx + radius * math.cos(angle),
                                     cy + radius * math.sin(angle)))
        points.append(points[0])
        return QgsGeometry.fromPolygonXY([points])

    if shape == SHAPE_ROUNDED:
        corner = min(rect.width(), rect.height()) * 0.18
        rounded = QgsGeometry.fromRect(rect)
        if corner > 0:
            # Erode then dilate by the same radius to round the corners.
            shrunk = rounded.buffer(-corner, 12)
            if not shrunk.isEmpty():
                grown = shrunk.buffer(corner, 12)
                if not grown.isEmpty():
                    return grown
        return rounded

    return QgsGeometry.fromRect(rect)


def base_geometry(area_utm: QgsGeometry, buffer_m: float = BASE_BUFFER_M) -> QgsGeometry:
    """The study area buffered outward by ``buffer_m`` — the ground-base footprint."""
    with contextlib.suppress(Exception):
        buffered = area_utm.buffer(buffer_m, 16)
        if buffered is not None and not buffered.isEmpty():
            return buffered
    return QgsGeometry(area_utm)


# --------------------------------------------------------------------------
# Overpass query + fetch
# --------------------------------------------------------------------------
def _overpass_query(min_lat: float, min_lon: float, max_lat: float, max_lon: float,
                    datasets=None) -> str:
    bbox = f"{min_lat},{min_lon},{max_lat},{max_lon}"
    enabled = set(
        ("buildings", "roads", "water", "greens", "trees", "furniture")
        if datasets is None else datasets
    )
    groups = {
        "buildings": (
            'way["building"]', 'way["building:part"]',
            'relation["building"]', 'relation["building:part"]',
        ),
        "roads": ('way["highway"]',),
        "water": (
            'way["waterway"~"river|stream|canal|drain|ditch|riverbank"]',
            'way["natural"="water"]', 'relation["natural"="water"]',
            'way["landuse"~"reservoir|basin"]', 'relation["landuse"~"reservoir|basin"]',
        ),
        "greens": (
            'way["leisure"~"park|garden|playground|pitch|nature_reserve|common|dog_park|golf_course"]',
            'relation["leisure"~"park|garden|playground|pitch|nature_reserve|common|dog_park|golf_course"]',
            'way["landuse"~"forest|grass|meadow|recreation_ground|cemetery|village_green|orchard|vineyard|farmland|allotments|greenfield"]',
            'relation["landuse"~"forest|grass|meadow|recreation_ground|cemetery|village_green|orchard|vineyard|farmland|allotments|greenfield"]',
            'way["natural"~"wood|scrub|grassland|heath"]',
            'relation["natural"~"wood|scrub|grassland|heath"]',
            'way["amenity"~"parking|marketplace"]', 'relation["amenity"~"parking|marketplace"]',
            'way["place"="square"]', 'relation["place"="square"]',
        ),
        "trees": ('node["natural"="tree"]',),
        "furniture": (
            'node["highway"="bus_stop"]', 'node["amenity"="bench"]',
            'node["highway"="street_lamp"]', 'node["amenity"="waste_basket"]',
        ),
    }
    selectors = [f"  {selector}({bbox});"
                 for name, rows in groups.items() if name in enabled
                 for selector in rows]
    if not selectors:
        raise OsmDownloadError("Select at least one OSM dataset.")
    return (f"[out:json][timeout:{DEFAULT_TIMEOUT_S}];\n(\n"
            + "\n".join(selectors) + "\n);\nout body geom;")


def bbox_area_km2(min_lat, min_lon, max_lat, max_lon) -> float:
    """Conservative planar area estimate for a WGS84 bounding box."""
    values = tuple(float(value) for value in (min_lat, min_lon, max_lat, max_lon))
    south, west, north, east = values
    if not all(math.isfinite(value) for value in values):
        raise OsmDownloadError("The selected extent is not finite.")
    if not (-90 <= south < north <= 90 and -180 <= west < east <= 180):
        raise OsmDownloadError("The selected extent is outside WGS84 bounds.")
    nearest_equator = 0.0 if south <= 0 <= north else min(abs(south), abs(north))
    longitude_scale = max(0.01, math.cos(math.radians(nearest_equator)))
    return ((north - south) * _KM_PER_DEGREE
            * (east - west) * _KM_PER_DEGREE * longitude_scale)


def tile_bbox(min_lat, min_lon, max_lat, max_lon,
              max_tile_km2: float = MAX_TILE_AREA_KM2):
    """Split a large WGS84 extent into compact, bounded Overpass requests."""
    area = bbox_area_km2(min_lat, min_lon, max_lat, max_lon)
    if area > MAX_JOB_AREA_KM2:
        raise OsmDownloadError(
            f"The selected extent is {area:,.0f} km2; the safe tiled limit is "
            f"{MAX_JOB_AREA_KM2:,.0f} km2. Reduce Max area or download it in parts."
        )
    if area <= max_tile_km2:
        return ((float(min_lat), float(min_lon), float(max_lat), float(max_lon)),)
    south, west, north, east = map(float, (min_lat, min_lon, max_lat, max_lon))
    nearest_equator = 0.0 if south <= 0 <= north else min(abs(south), abs(north))
    longitude_scale = max(0.01, math.cos(math.radians(nearest_equator)))
    height_km = (north - south) * _KM_PER_DEGREE
    width_km = (east - west) * _KM_PER_DEGREE * longitude_scale
    side_km = math.sqrt(max(1.0, float(max_tile_km2)))
    rows = max(1, math.ceil(height_km / side_km))
    columns = max(1, math.ceil(width_km / side_km))
    if rows * columns > MAX_TILES:
        raise OsmDownloadError(
            f"The selected extent needs {rows * columns} OSM requests; the safe "
            f"limit is {MAX_TILES}. Reduce Max area or download it in parts."
        )
    lat_step = (north - south) / rows
    lon_step = (east - west) / columns
    tiles = []
    for row in range(rows):
        tile_south = south + row * lat_step
        tile_north = north if row == rows - 1 else tile_south + lat_step
        for column in range(columns):
            tile_west = west + column * lon_step
            tile_east = east if column == columns - 1 else tile_west + lon_step
            tiles.append((tile_south, tile_west, tile_north, tile_east))
    return tuple(tiles)


def _network_enum(group: str, name: str):
    enum = getattr(QNetworkRequest, group, None)
    return getattr(enum, name) if enum is not None else getattr(QNetworkRequest, name)


def _validate_payload(payload: object) -> dict:
    if not isinstance(payload, dict):
        raise OsmDownloadError("The OSM server returned an invalid response.")
    remark = str(payload.get("remark") or "").strip()
    if remark:
        raise OsmDownloadError(f"The OSM server could not complete the query: {remark[:240]}")
    elements = payload.get("elements")
    if not isinstance(elements, list):
        raise OsmDownloadError("The OSM response has no elements list.")
    if len(elements) > MAX_ELEMENTS_PER_TILE:
        raise OsmDownloadError("The OSM tile returned too many objects; use a smaller Max area.")
    return payload


def _fetch_one(endpoint: str, query: str, timeout_s: int):
    """Return (payload, detail, transient) using QGIS proxy/TLS settings."""
    url = QUrl(endpoint)
    if not url.isValid() or url.scheme() != "https" or not url.host():
        return None, "invalid HTTPS endpoint", False
    encoded = QUrlQuery()
    encoded.addQueryItem("data", query)
    body = QByteArray(
        encoded.query(
            QUrl.ComponentFormattingOption.FullyEncoded
        ).encode("ascii")
    )
    request = QNetworkRequest(url)
    if hasattr(request, "setTransferTimeout"):
        request.setTransferTimeout((timeout_s + 10) * 1000)
    request.setHeader(
        _network_enum("KnownHeaders", "ContentTypeHeader"),
        "application/x-www-form-urlencoded",
    )
    request.setRawHeader(b"Accept", b"application/json")
    request.setRawHeader(b"User-Agent", USER_AGENT.encode("ascii"))
    client = QgsBlockingNetworkRequest()
    try:
        code = client.post(request, body, False, None)
    except Exception as exc:
        return None, str(exc).strip() or "network request exception", True
    no_error = getattr(
        getattr(QgsBlockingNetworkRequest, "ErrorCode", QgsBlockingNetworkRequest),
        "NoError",
    )
    if code != no_error:
        detail = "network request failed"
        error_message = getattr(client, "errorMessage", None)
        if callable(error_message):
            detail = str(error_message()).strip() or detail
        return None, detail, True
    reply = client.reply()
    try:
        status = int(reply.attribute(_network_enum("Attribute", "HttpStatusCodeAttribute")))
    except (TypeError, ValueError):
        status = 0
    if status and not 200 <= status < 300:
        return None, f"HTTP {status}", status == 429 or 500 <= status < 600
    content = bytes(reply.content())
    if len(content) > MAX_RESPONSE_BYTES:
        return None, "response exceeded 64 MB", False
    try:
        return _validate_payload(json.loads(content.decode("utf-8"))), "", False
    except (UnicodeDecodeError, json.JSONDecodeError, OsmDownloadError) as exc:
        return None, str(exc), False


def _fetch_query(query: str, timeout_s: int, feedback, use_cache: bool) -> dict:
    if use_cache:
        cached = _read_cache(query)
        if cached is not None:
            if feedback:
                feedback("Using cached OSM tile (no download) ...")
            return _validate_payload(cached)
    failures = []
    for index, endpoint in enumerate(OVERPASS_ENDPOINTS):
        host = urllib.parse.urlparse(endpoint).netloc or endpoint
        for attempt in range(2):
            if feedback:
                if attempt:
                    feedback(f"{host} transient failure; retrying once ...")
                else:
                    prefix = "Querying" if index == 0 else "Trying mirror"
                    feedback(f"{prefix} {host} ...")
            if attempt:
                time.sleep(MIRROR_RETRY_DELAY_S)
            payload, detail, transient = _fetch_one(endpoint, query, timeout_s)
            if payload is not None:
                if use_cache:
                    _write_cache(query, payload)
                return payload
            failures.append(f"{host}: {detail}")
            if not (transient and attempt == 0):
                break
    raise OsmDownloadError("All pinned OSM mirrors failed. " + "; ".join(failures[-3:]))


def fetch_overpass(min_lat: float, min_lon: float, max_lat: float, max_lon: float,
                   timeout_s: int = DEFAULT_TIMEOUT_S, feedback=None,
                   use_cache: bool = True, datasets=None, cancel_check=None) -> dict:
    """Fetch one or more bounded tiles, cache them, and merge by OSM identity."""
    tiles = tile_bbox(min_lat, min_lon, max_lat, max_lon)
    queries = tuple(_overpass_query(*tile, datasets=datasets) for tile in tiles)
    if feedback and len(queries) > 1:
        feedback(f"Large area: downloading {len(queries)} safe OSM tiles in sequence ...")
    payloads = []
    failed_tiles = []
    failure_details = []
    for index, query in enumerate(queries):
        if cancel_check is not None and cancel_check():
            raise OsmCancelledError("Download cancelled by user.")
        if feedback and len(queries) > 1:
            feedback(f"Downloading OSM tile {index + 1}/{len(queries)} ...")
        try:
            payloads.append(_fetch_query(query, timeout_s, feedback, use_cache))
        except OsmCancelledError:
            raise
        except OsmDownloadError as exc:
            if len(queries) == 1:
                raise
            failed_tiles.append(index + 1)
            failure_details.append(str(exc))
        if (index + 1 < len(queries)
                and (not use_cache or _read_cache(queries[index + 1]) is None)):
            if cancel_check is not None and cancel_check():
                raise OsmCancelledError("Download cancelled by user.")
            time.sleep(TILE_PAUSE_S)
    if not payloads:
        raise OsmDownloadError(
            f"All {len(queries)} OSM tiles failed. Check QGIS proxy/network settings "
            "or retry shortly; successful retries are cached per tile. Last error: "
            + (failure_details[-1] if failure_details else "no server answered")
        )
    merged = {}
    for payload in payloads:
        for element in payload["elements"]:
            key = (str(element.get("type", "")), str(element.get("id", "")))
            merged.setdefault(key, element)
        if len(merged) > MAX_TOTAL_ELEMENTS:
            raise OsmDownloadError(
                f"The job returned more than {MAX_TOTAL_ELEMENTS:,} OSM objects. "
                "Reduce Max area."
            )
    if feedback:
        message = (
            f"Merged {len(payloads)}/{len(queries)} tiles into "
            f"{len(merged):,} OSM objects."
        )
        if failed_tiles:
            missing = ", ".join(map(str, failed_tiles))
            message += f" Missing tiles: {missing}. Retry to fill them from cache."
        feedback(message)
    return {
        "elements": list(merged.values()),
        "_download": {
            "tiles_total": len(queries),
            "tiles_succeeded": len(payloads),
            "tiles_failed": tuple(failed_tiles),
        },
    }


# --------------------------------------------------------------------------
# OSM element -> attributes
# --------------------------------------------------------------------------
# Default floor counts by OSM ``building`` type, used only when OSM has no
# building:levels / height. Housing reads taller than retail; light industry and
# worship halls read low. Unknown buildings keep the historical default of 3.
_DEFAULT_FLOORS_BY_OSM = {
    "apartments": 4, "residential": 4, "dormitory": 5,
    "house": 2, "detached": 2, "terrace": 2, "semidetached_house": 2, "bungalow": 1,
    "commercial": 3, "office": 5, "retail": 2, "supermarket": 1, "kiosk": 1,
    "industrial": 1, "warehouse": 1, "manufacture": 1, "hangar": 1,
    "school": 3, "university": 4, "college": 4, "kindergarten": 2,
    "hospital": 5, "clinic": 3,
    "church": 1, "mosque": 1, "temple": 1, "synagogue": 1, "cathedral": 2, "chapel": 1,
    "public": 3, "civic": 3, "government": 4, "townhall": 3,
    "garage": 1, "garages": 1, "shed": 1, "hut": 1, "roof": 1, "carport": 1,
}


def _parse_osm_number(value):
    """Parse an OSM numeric tag ('12', '12.5', '12 m', '3;4') -> float or None."""
    if value is None:
        return None
    try:
        return float(str(value).split(";")[0].strip().rstrip(" m").strip())
    except (ValueError, TypeError):
        return None


def _building_levels(tags: dict) -> int:
    """Floor count for the export's OSM-native ``building_levels`` column.

    Priority: building:levels (+ roof:levels) -> height / 3 m -> a default by the
    OSM ``building`` type -> 3.
    """
    base = None
    for key in ("building:levels", "levels"):
        n = _parse_osm_number(tags.get(key))
        if n is not None:
            base = max(1, int(round(n)))
            break
    if base is None:
        h = _parse_osm_number(tags.get("height"))
        if h is not None and h > 0:
            base = max(1, int(round(h / 3.0)))
    if base is None:
        btype = (tags.get("building") or tags.get("building:part") or "").lower()
        return _DEFAULT_FLOORS_BY_OSM.get(btype, 3)
    roof = _parse_osm_number(tags.get("roof:levels"))
    if roof:
        base += max(0, int(round(roof)))
    return max(1, base)


def _building_min_height(tags: dict) -> float:
    """Base elevation offset (m above ground) from ``min_height`` or ``building:min_level``."""
    for key in ("min_height", "building:min_height"):
        val = _parse_osm_number(tags.get(key))
        if val is not None and val > 0:
            return round(float(val), 1)
    for key in ("building:min_level", "min_level"):
        lvl = _parse_osm_number(tags.get(key))
        if lvl is not None and lvl > 0:
            return round(float(lvl) * 3.0, 1)
    return 0.0


# Default carriageway/channel width (m) per OSM waterway class, used when the way
# has no explicit ``width``. The viewer draws each waterline as a ribbon of this
# width via the manifest's ``waterline_width_field`` mapping.
_WATERWAY_WIDTH = {
    "river": 8.0,
    "canal": 6.0,
    "stream": 2.5,
    "drain": 1.5,
    "ditch": 1.2,
}


def _waterway_class(tags: dict) -> str:
    return (tags.get("waterway") or "").lower() or "stream"


def _waterway_width(tags: dict) -> float:
    for key in ("width", "est_width"):
        n = _parse_osm_number(tags.get(key))
        if n is not None and n > 0:
            return max(0.5, n)
    return _WATERWAY_WIDTH.get(_waterway_class(tags), 3.0)


def _tag(tags: dict, key: str) -> str:
    """Lower-cased OSM tag value, or '' when absent — emitted verbatim as a column."""
    return (tags.get(key) or "").strip().lower()


def _is_water_area(tags: dict) -> bool:
    """True for an OSM polygon that should render as open water (a filled surface)."""
    if tags.get("natural") == "water":
        return True
    if tags.get("waterway") == "riverbank":
        return True
    if tags.get("landuse") in ("reservoir", "basin"):
        return True
    return bool((tags.get("water") or "").strip())


def _is_paved_area(tags: dict) -> bool:
    """True for an OSM polygon that should render as a paved public square/plaza."""
    if tags.get("highway") in ("pedestrian", "footway", "living_street") and tags.get("area") == "yes":
        return True
    if tags.get("place") == "square":
        return True
    return tags.get("amenity") == "marketplace"


# Tree scatter config
_TREE_SCATTER = {
    "forest": (75.0, 7.0, 13.0), "wood": (75.0, 7.0, 13.0),
    "orchard": (110.0, 4.0, 7.0),
    "park": (170.0, 5.0, 9.0), "garden": (210.0, 4.0, 7.0), "nature_reserve": (180.0, 5.0, 10.0),
    "village_green": (220.0, 4.0, 8.0), "recreation_ground": (230.0, 4.0, 8.0),
    "cemetery": (260.0, 5.0, 9.0), "scrub": (280.0, 2.5, 4.5), "allotments": (320.0, 3.0, 5.0),
    "grass": (400.0, 3.0, 6.0), "meadow": (440.0, 3.0, 6.0),
}
MAX_SCATTER_TREES = 500
MAX_SCATTER_PER_POLY = 130


def _green_scatter_kind(tags: dict) -> str:
    """Return the tree-bearing green type for ``tags``, or '' if none should scatter."""
    for value in (_tag(tags, "landuse"), _tag(tags, "leisure"), _tag(tags, "natural")):
        if value in _TREE_SCATTER:
            return value
    return ""


class _LCG:
    """Self-contained deterministic PRNG (Knuth MMIX linear congruential), so the
    module needs no ``random`` import. Reproducible per seed: the same study area
    scatters the same trees on every run.
    """

    __slots__ = ("_state",)
    _MASK = (1 << 64) - 1

    def __init__(self, seed):
        self._state = (int(seed) & self._MASK) or 0x2545F4914F6CDD1D

    def _next53(self):
        # MMIX constants; take the top 53 bits for a [0, 1) fraction.
        self._state = (self._state * 6364136223846793005 + 1442695040888963407) & self._MASK
        return (self._state >> 11) / float(1 << 53)

    def uniform(self, lo, hi):
        return lo + (hi - lo) * self._next53()


def _scatter_trees(geom_utm: QgsGeometry, kind: str, provider, remaining: int) -> int:
    """Scatter up to ``remaining`` trees inside ``geom_utm``."""
    cfg = _TREE_SCATTER.get(kind)
    if not cfg or remaining <= 0:
        return 0
    spacing_area, h_min, h_max = cfg
    area = geom_utm.area()
    if area < spacing_area * 2.0:
        return 0
    target = int(min(MAX_SCATTER_PER_POLY, remaining, area / spacing_area))
    if target <= 0:
        return 0
    bbox = geom_utm.boundingBox()
    kind_code = sum((i + 1) * ord(c) for i, c in enumerate(kind))
    seed = ((int(round(bbox.xMinimum())) * 73856093)
            ^ (int(round(bbox.yMinimum())) * 19349663)
            ^ (int(round(area)) * 83492791)
            ^ (kind_code * 2654435761)) & 0xFFFFFFFF
    rng = _LCG(seed)
    feats = []
    attempts = 0
    max_attempts = target * 8
    while len(feats) < target and attempts < max_attempts:
        attempts += 1
        px = rng.uniform(bbox.xMinimum(), bbox.xMaximum())
        py = rng.uniform(bbox.yMinimum(), bbox.yMaximum())
        pt = QgsGeometry.fromPointXY(QgsPointXY(px, py))
        if not geom_utm.contains(pt):
            continue
        feat = QgsFeature()
        feat.setGeometry(pt)
        feat.setAttributes([f"scatter_{int(px)}_{int(py)}", "tree", round(rng.uniform(h_min, h_max), 1)])
        feats.append(feat)
    if feats:
        provider.addFeatures(feats)
    return len(feats)


def _ring_from_geometry(geometry) -> QgsGeometry | None:
    """Build a closed-ring polygon from an Overpass member/way ``geometry`` list."""
    pts = [QgsPointXY(pt["lon"], pt["lat"])
           for pt in (geometry or []) if "lon" in pt and "lat" in pt]
    if len(pts) < 3:
        return None
    if pts[0] != pts[-1]:
        pts.append(pts[0])
    ring = QgsGeometry.fromPolygonXY([pts])
    return ring if ring and not ring.isEmpty() else None


def _relation_multipolygon(element) -> QgsGeometry | None:
    """Assemble a (multi)polygon from a relation's outer/inner member ways."""
    outers, inners = [], []
    for member in (element.get("members") or []):
        if member.get("type") != "way":
            continue
        ring = _ring_from_geometry(member.get("geometry"))
        if ring is None:
            continue
        (inners if (member.get("role") or "").lower() == "inner" else outers).append(ring)
    if not outers:
        return None
    poly = outers[0]
    for extra in outers[1:]:
        merged = poly.combine(extra)
        if merged and not merged.isEmpty():
            poly = merged
    for hole in inners:
        cut = poly.difference(hole)
        if cut and not cut.isEmpty():
            poly = cut
    if poly is None or poly.isEmpty():
        return None
    return poly


def _element_polygon(element) -> QgsGeometry | None:
    """Polygon for a building/green element: relation -> multipolygon, else ring."""
    if element.get("type") == "relation":
        return _relation_multipolygon(element)
    return _way_polygon(element)


def _way_polygon(element) -> QgsGeometry | None:
    geometry = element.get("geometry") or []
    if len(geometry) < 3:
        return None
    points = [QgsPointXY(pt["lon"], pt["lat"]) for pt in geometry]
    if points[0] != points[-1]:
        points.append(points[0])
    return QgsGeometry.fromPolygonXY([points])


def _way_polyline(element) -> QgsGeometry | None:
    geometry = element.get("geometry") or []
    if len(geometry) < 2:
        return None
    points = [QgsPointXY(pt["lon"], pt["lat"]) for pt in geometry]
    return QgsGeometry.fromPolylineXY(points)


def _node_point(element) -> QgsGeometry | None:
    if "lon" not in element or "lat" not in element:
        return None
    return QgsGeometry.fromPointXY(QgsPointXY(element["lon"], element["lat"]))


def _make_layer(name: str, wkb_type: str, epsg_dest: int, fields_def):
    layer = QgsVectorLayer(f"{wkb_type}?crs=EPSG:{epsg_dest}", name, "memory")
    provider = layer.dataProvider()
    provider.addAttributes([QgsField(field_name, qvariant) for field_name, qvariant in fields_def])
    layer.updateFields()
    return layer, provider


class _FeatureBatch:
    """Collect provider writes and commit them in one C++ boundary crossing."""

    def __init__(self, provider):
        self.provider = provider
        self.features = []

    def addFeatures(self, features):
        self.features.extend(features)

    def flush(self):
        if self.features:
            self.provider.addFeatures(self.features)
            self.features = []


def build_base_layer(area_utm: QgsGeometry, epsg_dest: int,
                     buffer_m: float = BASE_BUFFER_M) -> QgsVectorLayer:
    """A one-feature ground-base layer (study area buffered by ``buffer_m``)."""
    layer, provider = _make_layer(
        "OSM Base", "MultiPolygon", epsg_dest, [("kind", QVariant.String)]
    )
    feat = QgsFeature()
    feat.setGeometry(base_geometry(area_utm, buffer_m))
    feat.setAttributes(["base"])
    provider.addFeatures([feat])
    layer.updateExtents()
    return layer


def save_layer_to_geojson(layer: QgsVectorLayer, path) -> None:
    options = QgsVectorFileWriter.SaveVectorOptions()
    options.driverName = "GeoJSON"
    options.fileEncoding = "UTF-8"
    QgsVectorFileWriter.writeAsVectorFormatV3(
        layer, str(path), QgsProject.instance().transformContext(), options
    )


def write_layer_to_gpkg(layer: QgsVectorLayer, gpkg_path: str, layer_name: str,
                        first: bool) -> str | None:
    """Write one (styled) memory layer into a GeoPackage as ``layer_name``.

    ``first`` overwrites/creates the file; later layers add a new table to it.
    Returns ``None`` on success or a short error string. Used to make the
    otherwise-ephemeral memory layers durable so they survive project reload.
    """
    options = QgsVectorFileWriter.SaveVectorOptions()
    options.driverName = "GPKG"
    options.layerName = layer_name
    options.fileEncoding = "UTF-8"
    _action = getattr(QgsVectorFileWriter, "ActionOnExistingFile", QgsVectorFileWriter)
    options.actionOnExistingFile = (
        getattr(_action, "CreateOrOverwriteFile") if first
        else getattr(_action, "CreateOrOverwriteLayer")
    )
    try:
        result = QgsVectorFileWriter.writeAsVectorFormatV3(
            layer, str(gpkg_path), QgsProject.instance().transformContext(), options
        )
    except Exception as exc:  # pragma: no cover - depends on GDAL build
        return str(exc)
    # writeAsVectorFormatV3 returns (errorCode, errorMessage); NoError == 0.
    try:
        code, message = result[0], result[1]
    except (TypeError, IndexError):
        return None
    _no_error = getattr(getattr(QgsVectorFileWriter, "WriterError", QgsVectorFileWriter), "NoError")
    if code != _no_error:
        return message or f"write error {code}"
    return None


def _geometry_transform_ok(result) -> bool:
    """Return True when QgsGeometry.transform succeeded across QGIS versions."""
    if result is None:
        return True
    with contextlib.suppress(TypeError, ValueError):
        if int(result) == 0:
            return True
    text = str(result).lower()
    if text.endswith("success") or text.endswith(".success"):
        return True
    try:
        from qgis.core import Qgis
        return result == Qgis.GeometryOperationResult.Success
    except Exception:
        return False


def _valid_clip_boundary(area_utm: QgsGeometry) -> QgsGeometry:
    """Use a valid copy of the study area for repeatable clipping."""
    boundary = QgsGeometry(area_utm)
    with contextlib.suppress(Exception):
        if not boundary.isGeosValid():
            fixed = boundary.makeValid()
            if fixed is not None and not fixed.isEmpty():
                boundary = fixed
    return boundary


# --------------------------------------------------------------------------
# Main download + clip
# --------------------------------------------------------------------------
def download_osm_for_area(area_utm: QgsGeometry, epsg_dest: int, feedback=None,
                          use_cache: bool = True, datasets=None, cancel_check=None) -> dict:
    """Fetch OSM for the boundary's bbox, reproject to UTM, clip to the boundary.

    ``area_utm`` is the study-boundary polygon (circle, rounded rectangle,
    rectangle or exact polygon) in the EPSG:``epsg_dest`` metric CRS. Returns a
    dict of memory layers keyed by role plus feature counts.
    """
    project = QgsProject.instance()
    dst_crs = QgsCoordinateReferenceSystem.fromEpsgId(epsg_dest)
    wgs_crs = QgsCoordinateReferenceSystem.fromEpsgId(4326)
    to_wgs = QgsCoordinateTransform(dst_crs, wgs_crs, project)
    to_utm = QgsCoordinateTransform(wgs_crs, dst_crs, project)

    wgs_rect = to_wgs.transformBoundingBox(area_utm.boundingBox())
    min_lon, min_lat = wgs_rect.xMinimum(), wgs_rect.yMinimum()
    max_lon, max_lat = wgs_rect.xMaximum(), wgs_rect.yMaximum()
    clip_boundary = _valid_clip_boundary(area_utm)
    enabled_datasets = set(
        ("buildings", "roads", "water", "greens", "trees", "furniture")
        if datasets is None else datasets
    )

    payload = fetch_overpass(
        min_lat, min_lon, max_lat, max_lon, feedback=feedback,
        use_cache=use_cache, datasets=datasets, cancel_check=cancel_check,
    )
    elements = payload.get("elements") or []
    if not elements:
        raise OsmDownloadError("Overpass returned 0 elements for this area. Try a different or larger area.")

    # Columns mirror raw OSM tags (no PlanX schema): the viewer maps building_levels
    # -> floors, building -> colour, highway -> hierarchy, etc. via the manifest.
    # footprint_m2 (polygon area in the metric UTM CRS) and gfa_m2 (estimated gross
    # floor area = footprint x floors) are computed here so both the memory and the
    # GeoPackage outputs carry them, ready to label, sum or analyse in QGIS.
    buildings_layer, b_pr = _make_layer(
        "OSM Buildings", "MultiPolygon", epsg_dest,
        [("osm_id", QVariant.String), ("building", QVariant.String),
         ("building_levels", QVariant.Int), ("height", QVariant.Double),
         ("min_height", QVariant.Double),
         ("footprint_m2", QVariant.Double), ("gfa_m2", QVariant.Double),
         ("name", QVariant.String)],
    )
    roads_layer, r_pr = _make_layer(
        "OSM Roads", "MultiLineString", epsg_dest,
        [("osm_id", QVariant.String), ("highway", QVariant.String),
         ("width", QVariant.Double), ("name", QVariant.String)],
    )
    # Dedicated cycleways (highway=cycleway) split off into their own bike-lane layer.
    bikelanes_layer, bl_pr = _make_layer(
        "OSM Bike lanes", "MultiLineString", epsg_dest,
        [("osm_id", QVariant.String), ("highway", QVariant.String),
         ("width", QVariant.Double), ("name", QVariant.String)],
    )
    greens_layer, g_pr = _make_layer(
        "OSM Greens", "MultiPolygon", epsg_dest,
        [("osm_id", QVariant.String), ("leisure", QVariant.String),
         ("landuse", QVariant.String), ("natural", QVariant.String),
         ("amenity", QVariant.String), ("highway", QVariant.String),
         ("place", QVariant.String), ("name", QVariant.String)],
    )
    trees_layer, t_pr = _make_layer(
        "OSM Trees", "Point", epsg_dest,
        [("osm_id", QVariant.String), ("natural", QVariant.String), ("height", QVariant.Double)],
    )
    # Polygon/line layers are Multi* so that boundary-clipping a feature into
    # several parts never drops it (a single part is promoted automatically).
    waterlines_layer, w_pr = _make_layer(
        "OSM Waterlines", "MultiLineString", epsg_dest,
        [("osm_id", QVariant.String), ("waterway", QVariant.String),
         ("width", QVariant.Double), ("name", QVariant.String)],
    )
    waterareas_layer, wa_pr = _make_layer(
        "OSM Water areas", "MultiPolygon", epsg_dest,
        [("osm_id", QVariant.String), ("natural", QVariant.String),
         ("waterway", QVariant.String), ("landuse", QVariant.String), ("name", QVariant.String)],
    )
    # Street furniture as points — one layer per viewer input (mybusstops,
    # mybenches, mylights, mytrashbins).
    busstops_layer, bs_pr = _make_layer(
        "OSM Bus stops", "Point", epsg_dest,
        [("osm_id", QVariant.String), ("highway", QVariant.String), ("name", QVariant.String)],
    )
    benches_layer, be_pr = _make_layer(
        "OSM Benches", "Point", epsg_dest, [("osm_id", QVariant.String), ("amenity", QVariant.String)],
    )
    lights_layer, li_pr = _make_layer(
        "OSM Street lights", "Point", epsg_dest, [("osm_id", QVariant.String), ("highway", QVariant.String)],
    )
    trashbins_layer, tb_pr = _make_layer(
        "OSM Trash bins", "Point", epsg_dest, [("osm_id", QVariant.String), ("amenity", QVariant.String)],
    )

    batches = tuple(_FeatureBatch(provider) for provider in (
        b_pr, r_pr, bl_pr, g_pr, t_pr, w_pr, wa_pr, bs_pr, be_pr, li_pr, tb_pr,
    ))
    (b_pr, r_pr, bl_pr, g_pr, t_pr, w_pr, wa_pr,
     bs_pr, be_pr, li_pr, tb_pr) = batches

    counts = {
        "buildings": 0, "roads": 0, "bikelanes": 0, "greens": 0, "trees": 0,
        "trees_scattered": 0, "parking": 0, "plazas": 0, "waterlines": 0,
        "waterareas": 0, "busstops": 0, "benches": 0, "lights": 0,
        "trashbins": 0, "skipped": 0,
    }

    def clip_to_area(geom_wgs: QgsGeometry):
        g = QgsGeometry(geom_wgs)
        if not _geometry_transform_ok(g.transform(to_utm)):
            return None
        with contextlib.suppress(Exception):
            if not g.isGeosValid():
                fixed = g.makeValid()
                if fixed is not None and not fixed.isEmpty():
                    g = fixed
        if not g.intersects(clip_boundary):
            return None
        clipped = g.intersection(clip_boundary)
        with contextlib.suppress(Exception):
            if clipped is not None and not clipped.isEmpty() and not clipped.isGeosValid():
                fixed = clipped.makeValid()
                if fixed is not None and not fixed.isEmpty():
                    clipped = fixed
        if clipped is None or clipped.isEmpty():
            return g if g.within(clip_boundary) else None
        return clipped

    scattered_trees = 0
    for elem_idx, element in enumerate(elements):
        if cancel_check is not None and elem_idx % 250 == 0 and cancel_check():
            raise OsmCancelledError("Download cancelled by user.")
        etype = element.get("type")
        tags = element.get("tags") or {}

        if etype == "node" and tags.get("natural") == "tree":
            geom = _node_point(element)
            if not geom:
                continue
            g = QgsGeometry(geom)
            if not _geometry_transform_ok(g.transform(to_utm)) or not clip_boundary.contains(g):
                counts["skipped"] += 1
                continue
            height_val = _parse_osm_number(tags.get("height")) or 6.0
            feat = QgsFeature()
            feat.setGeometry(g)
            feat.setAttributes([str(element.get("id", "")), "tree", round(height_val, 1)])
            t_pr.addFeatures([feat])
            counts["trees"] += 1
            continue

        if etype == "node":
            # Street furniture points: bus stops, benches, street lamps, bins.
            geom = _node_point(element)
            if not geom:
                counts["skipped"] += 1
                continue
            g = QgsGeometry(geom)
            if not _geometry_transform_ok(g.transform(to_utm)) or not clip_boundary.contains(g):
                counts["skipped"] += 1
                continue
            osm_id = str(element.get("id", ""))
            if tags.get("highway") == "bus_stop" or tags.get("public_transport") == "platform":
                feat = QgsFeature()
                feat.setGeometry(g)
                feat.setAttributes([osm_id, _tag(tags, "highway") or "bus_stop", tags.get("name", "")])
                bs_pr.addFeatures([feat])
                counts["busstops"] += 1
            elif tags.get("amenity") == "bench":
                feat = QgsFeature()
                feat.setGeometry(g)
                feat.setAttributes([osm_id, "bench"])
                be_pr.addFeatures([feat])
                counts["benches"] += 1
            elif tags.get("highway") == "street_lamp":
                feat = QgsFeature()
                feat.setGeometry(g)
                feat.setAttributes([osm_id, "street_lamp"])
                li_pr.addFeatures([feat])
                counts["lights"] += 1
            elif tags.get("amenity") == "waste_basket":
                feat = QgsFeature()
                feat.setGeometry(g)
                feat.setAttributes([osm_id, "waste_basket"])
                tb_pr.addFeatures([feat])
                counts["trashbins"] += 1
            else:
                counts["skipped"] += 1
            continue

        if etype in ("way", "relation") and (tags.get("building") or tags.get("building:part")):
            base = _element_polygon(element)
            if not base:
                continue
            clipped = clip_to_area(base)
            if clipped is None:
                counts["skipped"] += 1
                continue
            height_val = _parse_osm_number(tags.get("height"))
            min_height_val = _building_min_height(tags)
            levels = _building_levels(tags)
            is_part_only = bool(tags.get("building:part")) and not bool(tags.get("building"))
            try:
                footprint = 0.0 if is_part_only else round(clipped.area(), 1)
            except Exception:
                footprint = None
            gfa = round(footprint * levels, 1) if footprint else (0.0 if is_part_only else None)
            b_tag = _tag(tags, "building") or _tag(tags, "building:part")
            if b_tag == "yes" and is_part_only:
                b_tag = "part"
            feat = QgsFeature()
            feat.setGeometry(clipped)
            feat.setAttributes([
                str(element.get("id", "")),
                b_tag,
                levels,
                round(height_val, 1) if height_val else None,
                min_height_val,
                footprint,
                gfa,
                tags.get("name", ""),
            ])
            b_pr.addFeatures([feat])
            counts["buildings"] += 1
            continue

        # Water areas (lakes, ponds, riverbanks, reservoirs, bays).
        if etype in ("way", "relation") and _is_water_area(tags):
            base = _element_polygon(element)
            if not base:
                continue
            clipped = clip_to_area(base)
            if clipped is None:
                counts["skipped"] += 1
                continue
            feat = QgsFeature()
            feat.setGeometry(clipped)
            feat.setAttributes([
                str(element.get("id", "")),
                _tag(tags, "natural"), _tag(tags, "waterway"), _tag(tags, "landuse"),
                tags.get("name", ""),
            ])
            wa_pr.addFeatures([feat])
            counts["waterareas"] += 1
            continue

        # Parking lots
        if etype in ("way", "relation") and tags.get("amenity") == "parking":
            base = _element_polygon(element)
            if not base:
                continue
            clipped = clip_to_area(base)
            if clipped is None:
                counts["skipped"] += 1
                continue
            feat = QgsFeature()
            feat.setGeometry(clipped)
            feat.setAttributes([
                str(element.get("id", "")), "", "", "",
                "parking", "", "", tags.get("name", "")
            ])
            g_pr.addFeatures([feat])
            counts["greens"] += 1
            counts["parking"] += 1
            continue

        # Paved public squares/plazas
        if etype in ("way", "relation") and _is_paved_area(tags):
            base = _element_polygon(element)
            if not base:
                continue
            clipped = clip_to_area(base)
            if clipped is None:
                counts["skipped"] += 1
                continue
            feat = QgsFeature()
            feat.setGeometry(clipped)
            feat.setAttributes([
                str(element.get("id", "")), "", "", "",
                _tag(tags, "amenity"), _tag(tags, "highway"), _tag(tags, "place"),
                tags.get("name", "")
            ])
            g_pr.addFeatures([feat])
            counts["greens"] += 1
            counts["plazas"] += 1
            continue

        if etype == "way" and tags.get("highway"):
            base = _way_polyline(element)
            if not base:
                continue
            clipped = clip_to_area(base)
            if clipped is None:
                counts["skipped"] += 1
                continue
            highway = _tag(tags, "highway")
            width = _parse_osm_number(tags.get("width"))
            feat = QgsFeature()
            feat.setGeometry(clipped)
            feat.setAttributes([
                str(element.get("id", "")), highway,
                round(width, 1) if width else None, tags.get("name", ""),
            ])
            # Dedicated cycle tracks go to the bike-lane layer; everything else
            # (incl. footways/paths, which the viewer keeps cars off) stays a road.
            if highway == "cycleway":
                bl_pr.addFeatures([feat])
                counts["bikelanes"] += 1
            else:
                r_pr.addFeatures([feat])
                counts["roads"] += 1
            continue

        if etype == "way" and tags.get("waterway"):
            base = _way_polyline(element)
            if not base:
                continue
            clipped = clip_to_area(base)
            if clipped is None:
                counts["skipped"] += 1
                continue
            feat = QgsFeature()
            feat.setGeometry(clipped)
            feat.setAttributes([
                str(element.get("id", "")),
                _tag(tags, "waterway"),
                round(_waterway_width(tags), 1),
                tags.get("name", ""),
            ])
            w_pr.addFeatures([feat])
            counts["waterlines"] += 1
            continue

        if etype in ("way", "relation") and (tags.get("leisure") or tags.get("landuse") or tags.get("natural")):
            base = _element_polygon(element)
            if not base:
                continue
            clipped = clip_to_area(base)
            if clipped is None:
                counts["skipped"] += 1
                continue
            feat = QgsFeature()
            feat.setGeometry(clipped)
            feat.setAttributes([
                str(element.get("id", "")),
                _tag(tags, "leisure"), _tag(tags, "landuse"), _tag(tags, "natural"),
                "", "", "", tags.get("name", ""),
            ])
            g_pr.addFeatures([feat])
            counts["greens"] += 1
            kind = _green_scatter_kind(tags)
            if kind and "trees" in enabled_datasets:
                placed = _scatter_trees(clipped, kind, t_pr, MAX_SCATTER_TREES - scattered_trees)
                scattered_trees += placed
                counts["trees"] += placed
                counts["trees_scattered"] += placed
            continue

        counts["skipped"] += 1

    for batch in batches:
        batch.flush()

    for layer in (buildings_layer, roads_layer, bikelanes_layer, greens_layer, trees_layer,
                  waterlines_layer, waterareas_layer, busstops_layer, benches_layer, lights_layer, trashbins_layer):
        layer.updateExtents()

    return {
        "epsg": epsg_dest,
        "download": payload.get("_download", {}),
        "counts": counts,
        "buildings": buildings_layer,
        "roads": roads_layer,
        "bikelanes": bikelanes_layer,
        "greens": greens_layer,
        "trees": trees_layer,
        "waterlines": waterlines_layer,
        "waterareas": waterareas_layer,
        "busstops": busstops_layer,
        "benches": benches_layer,
        "lights": lights_layer,
        "trashbins": trashbins_layer,
    }
