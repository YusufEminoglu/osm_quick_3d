# -*- coding: utf-8 -*-
"""OpenStreetMap download + clip for the OSM Quick 3D plugin.

Fetches buildings, roads, greens and trees from the public Overpass servers
for the bounding box of a study boundary, reprojects to a metric UTM CRS, and
clips every feature to that boundary.

The download follows the 2026 Overpass rules (``net/``, ported from 3D OSM
Model 1.10.0 and 02AgentOSM 1.6.0).  A large area is split into parts; for
each part the HTTP status is read even when QGIS reports an error, and each
server is asked at most once (once more after a dropped connection, on Qt 6
only).  A server that failed rests for a while that depends on its answer
(429: a minute, 403/406: an hour, 5xx: 45 s, no answer: 2 minutes), doubling
while it keeps failing; the rests and the last server that worked are kept
in QgsSettings (``osm_quick_3d/hosts/``) across QGIS sessions.  A click stops
asking as soon as every server is resting, and sends at most 64 requests in
all.  Each request claims ``[timeout:N][maxsize:134217728]`` matched to
QGIS's own network timeout (transfer at most 55 s); an answer is cut off at
once when Cancel is pressed or when it passes 64 MiB; empty or incomplete
answers are never cached; a download that got nothing ends with one plain
message naming every server, and a result with missing parts says so in
``notes``.

Thread safety (contract C2): nothing here reads or changes the QGIS project.
The download and the parsing run on the calling (worker) thread; the memory
layers it builds are handed to the main thread, never added to a project.
"""
from __future__ import annotations

import contextlib
import glob
import hashlib
import json
import math
import os
import re
import tempfile
import threading
import time
from typing import Callable, Dict, List, Optional, Sequence, Tuple
from urllib.parse import urlencode, urlsplit

from qgis.PyQt.QtCore import QT_VERSION, QByteArray, QThread, Qt, QUrl, QVariant
from qgis.PyQt.QtNetwork import QNetworkRequest
from qgis.core import (
    QgsApplication,
    QgsBlockingNetworkRequest,
    QgsCoordinateReferenceSystem,
    QgsCoordinateTransform,
    QgsCoordinateTransformContext,
    QgsFeature,
    QgsFeedback,
    QgsField,
    QgsGeometry,
    QgsNetworkAccessManager,
    QgsNetworkRequestParameters,
    QgsPointXY,
    QgsProject,
    QgsRectangle,
    QgsSettings,
    QgsVectorFileWriter,
    QgsVectorLayer,
    QgsWkbTypes,
)

from .net import hosts as _hosts
from .net import transport as _transport

_NO_NETWORK_ERROR = getattr(
    getattr(QgsBlockingNetworkRequest, "ErrorCode", QgsBlockingNetworkRequest),
    "NoError",
)
_POLYGON_GEOMETRY = getattr(
    getattr(QgsWkbTypes, "GeometryType", QgsWkbTypes),
    "PolygonGeometry",
)


def _plugin_version() -> str:
    """The version in metadata.txt, for the User-Agent suffix."""
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "metadata.txt")
    with contextlib.suppress(OSError, UnicodeDecodeError):
        with open(path, encoding="utf-8") as handle:
            for line in handle:
                if line.strip().lower().startswith("version="):
                    return line.split("=", 1)[1].strip() or "0"
    return "0"


PLUGIN_VERSION = _plugin_version()

# The public Overpass servers, one per operator, asked in this order when no
# server has worked yet; after that the one that last worked goes first
# (net/hosts.py remembers it across QGIS sessions).  overpass.kumi.systems is
# not listed: it is an alias of private.coffee (same machine).  Read at call
# time: the test guard replaces this tuple.
OVERPASS_ENDPOINTS = (
    "https://overpass-api.de/api/interpreter",
    "https://overpass.private.coffee/api/interpreter",
)
#: Sent as the Referer on every request.  QGIS replaces a plugin's own
#: User-Agent with its stock one but keeps this header, so the operators can
#: tell this plugin's traffic apart and know whom to contact.
REFERER = "https://github.com/YusufEminoglu/osm_quick_3d"
#: Appended to QGIS's User-Agent where QGIS allows it (QGIS 4.2 and later).
USER_AGENT = f"OSM-Quick-3D-QGIS-Plugin/{PLUGIN_VERSION} (+{REFERER})"
#: Kept for callers of the 0.51 `fetch_overpass(timeout_s=...)`: an upper
#: bound on the transfer, which otherwise follows QGIS's own network timeout.
DEFAULT_TIMEOUT_S = 60
MAX_RESPONSE_BYTES = _transport.MAX_RESPONSE_BYTES
#: Memory a request asks the server to reserve: 128 MiB instead of the 512 MiB
#: Overpass assumes when a query declares nothing.  A nearly full server
#: admits the small claim and sheds the default one.
OVERPASS_MAXSIZE_BYTES = 134_217_728
MAX_TILE_AREA_KM2 = 100.0
MAX_JOB_AREA_KM2 = 2500.0
MAX_TILES = 40
MAX_ELEMENTS_PER_TILE = 150_000
MAX_TOTAL_ELEMENTS = 400_000
#: Requests one click may send, whatever the number of parts.
MAX_CLICK_REQUESTS = _hosts.MAX_CLICK_REQUESTS
#: Parts on which every server asked ran out of time before a click stops:
#: such servers are probably overloaded, and their 30 s rest is short enough
#: that the click would otherwise wait it out and ask again for every part.
MAX_TIMEOUT_PARTS = 1
_KM_PER_DEGREE = 111.32

# Disk cache for Overpass answers, keyed by the exact selection.  Re-running
# the same area (or nudging a layer toggle) sends nothing within the TTL.
CACHE_TTL_S = 7 * 24 * 3600  # one week
#: Folder of the answer cache; None means "<system temp>/osm_quick_3d_cache".
#: Read at call time, so a test can point it at a folder of its own.
CACHE_DIR: Optional[str] = None
_CACHE_KEY_VERSION = "osm_quick_3d/0.52"

#: QgsSettings group that keeps each server's rest and the last server that
#: worked across QGIS sessions (read at the start of a click, written after).
SETTINGS_GROUP = "osm_quick_3d/hosts"
#: The board every download in this QGIS session shares (read at call time).
BOARD = _hosts.HostBoard()

_ALL_DATASETS = ("buildings", "roads", "water", "greens", "trees", "furniture")


class OsmDownloadError(RuntimeError):
    """A download or build step failed; ``str(error)`` is the final user text."""


class OsmDownloadCancelled(OsmDownloadError):
    """The user cancelled the download."""

    def __init__(self, message: str = "Download cancelled.") -> None:
        super().__init__(message)


#: The 0.51 name, kept so older callers still catch a cancel.
OsmCancelledError = OsmDownloadCancelled


# --------------------------------------------------------------------------
# Overpass answer cache (on disk, keyed by the exact selection)
# --------------------------------------------------------------------------
def _cache_dir() -> str:
    path = CACHE_DIR or os.path.join(tempfile.gettempdir(), "osm_quick_3d_cache")
    with contextlib.suppress(OSError):
        os.makedirs(path, exist_ok=True)
    return path


def _cache_path(key: str) -> str:
    # SHA-256 purely as a filename-safe digest of the key (not security).
    digest = hashlib.sha256(key.encode("utf-8")).hexdigest()[:40]
    return os.path.join(_cache_dir(), f"{digest}.json")


def _discard_path(path: str) -> None:
    with contextlib.suppress(OSError):
        os.remove(path)


def _read_cache_entry(key: str) -> Optional[Tuple[dict, float]]:
    """(payload, saved_at) for a fresh, usable saved answer, else None.

    An answer saved by 0.51.1 or earlier may be empty: such a copy, an
    incomplete one, an unreadable one and one older than the TTL are deleted
    instead of being used.
    """
    path = _cache_path(key)
    try:
        if not os.path.isfile(path):
            return None
        saved_at = os.path.getmtime(path)
        if time.time() - saved_at > CACHE_TTL_S:
            _discard_path(path)
            return None
        with open(path, "r", encoding="utf-8") as handle:
            payload = json.load(handle)
    except (OSError, ValueError):
        _discard_path(path)
        return None
    if _transport.cache_refusal(payload):
        _discard_path(path)
        return None
    return payload, saved_at


def _read_cache(key: str):
    """The saved answer for `key` if it is fresh and usable, else None."""
    entry = _read_cache_entry(key)
    return entry[0] if entry is not None else None


def _write_cache(key: str, payload: dict) -> bool:
    """Atomically keep a good answer; an empty or incomplete one never."""
    if _transport.cache_refusal(payload):
        return False
    tmp_path = None
    written = False
    try:
        cache_path = _cache_path(key)
        with tempfile.NamedTemporaryFile(
            "w", encoding="utf-8", dir=os.path.dirname(cache_path),
            prefix=".oq3d-", suffix=".tmp", delete=False,
        ) as handle:
            tmp_path = handle.name
            json.dump(payload, handle)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_path, cache_path)
        tmp_path = None
        written = True
    except (OSError, TypeError, ValueError):
        written = False
    finally:
        if tmp_path:
            _discard_path(tmp_path)
    return written


def clear_cache():
    """Delete all cached Overpass answers. Returns (files_removed, bytes_freed)."""
    removed, freed = 0, 0
    with contextlib.suppress(OSError):
        for path in glob.glob(os.path.join(_cache_dir(), "*.json")):
            with contextlib.suppress(OSError):
                size = os.path.getsize(path)
                os.remove(path)
                removed += 1
                freed += size
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
# Overpass query
# --------------------------------------------------------------------------
_DATASET_SELECTORS = {
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


def _overpass_selection(min_lat: float, min_lon: float, max_lat: float, max_lon: float,
                        datasets=None) -> str:
    """The statements that pick the data, without the settings header."""
    bbox = f"{min_lat},{min_lon},{max_lat},{max_lon}"
    enabled = set(_ALL_DATASETS if datasets is None else datasets)
    selectors = [f"  {selector}({bbox});"
                 for name, rows in _DATASET_SELECTORS.items() if name in enabled
                 for selector in rows]
    if not selectors:
        raise OsmDownloadError("Select at least one OSM dataset.")
    return "(\n" + "\n".join(selectors) + "\n);\nout body geom;"


def _overpass_header(timeout_s: int) -> str:
    """`[timeout:N]` is what the server may spend; `[maxsize]` the memory it
    is asked to reserve.  Both are claims the server admits against."""
    return f"[out:json][timeout:{max(1, int(timeout_s))}][maxsize:{OVERPASS_MAXSIZE_BYTES}];"


def _overpass_query(min_lat: float, min_lon: float, max_lat: float, max_lon: float,
                    datasets=None, timeout_s: Optional[int] = None) -> str:
    """The whole request; `timeout_s` defaults to what QGIS's timeout allows."""
    if timeout_s is None:
        timeout_s = _transport.server_timeout_seconds(_transfer_seconds())
    return (_overpass_header(timeout_s) + "\n"
            + _overpass_selection(min_lat, min_lon, max_lat, max_lon, datasets))


def _cache_key(min_lat: float, min_lon: float, max_lat: float, max_lon: float,
               datasets=None) -> str:
    # The settings header is left out: a changed QGIS timeout must not orphan
    # a saved answer for the same selection.
    return (_CACHE_KEY_VERSION + "\n"
            + _overpass_selection(min_lat, min_lon, max_lat, max_lon, datasets))


def bbox_area_km2(min_lat, min_lon, max_lat, max_lon) -> float:
    """Conservative planar area estimate for a WGS84 bounding box."""
    try:
        values = tuple(float(value) for value in (min_lat, min_lon, max_lat, max_lon))
    except (TypeError, ValueError) as exc:
        raise OsmDownloadError("The selected extent is not a valid area.") from exc
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
            f"The selected extent is {area:,.0f} km²; the safe limit is "
            f"{MAX_JOB_AREA_KM2:,.0f} km². Reduce Max area or download it in parts."
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
            f"The selected extent needs {rows * columns} separate downloads; the safe "
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


def _validate_payload(payload: object) -> dict:
    """Reject malformed or explicitly incomplete Overpass answers."""
    if not isinstance(payload, dict):
        raise OsmDownloadError("The OpenStreetMap answer is not valid map data.")
    if str(payload.get("remark") or "").strip():
        raise OsmDownloadError("The OpenStreetMap server marked its answer as incomplete, "
                               "so nothing was built. Try again later, or choose a smaller area.")
    elements = payload.get("elements")
    if not isinstance(elements, list) or any(not isinstance(element, dict) for element in elements):
        raise OsmDownloadError("The OpenStreetMap answer is not valid map data.")
    if len(elements) > MAX_ELEMENTS_PER_TILE:
        raise OsmDownloadError("This area holds too much OpenStreetMap data for one download. "
                               "Choose a smaller Max area.")
    return payload


# --------------------------------------------------------------------------
# One request to one server, through QGIS's own network stack
# --------------------------------------------------------------------------
def _network_enum(group: str, name: str):
    """Qt 5 exposes these enum members on QNetworkRequest, Qt 6 nests them."""
    enum = getattr(QNetworkRequest, group, None)
    return getattr(enum, name) if enum is not None else getattr(QNetworkRequest, name)


def _reply_status(reply) -> int:
    try:
        return int(reply.attribute(_network_enum("Attribute", "HttpStatusCodeAttribute")))
    except (TypeError, ValueError):
        return 0


def _enum_int(value) -> int:
    """An enum's number on Qt 5 (int enums) and Qt 6 (Python enums)."""
    with contextlib.suppress(TypeError, ValueError):
        return int(getattr(value, "value", value))
    return 0


_UA_ATTRIBUTE_PROBED = False
_UA_ATTRIBUTE = None


def _user_agent_attribute():
    """The request attribute QGIS appends to its User-Agent (QGIS 4.2+).

    Older QGIS has no such attribute and overwrites any User-Agent a plugin
    sets, so there the Referer is the only identification that arrives.
    """
    global _UA_ATTRIBUTE_PROBED, _UA_ATTRIBUTE
    if not _UA_ATTRIBUTE_PROBED:
        attribute = None
        with contextlib.suppress(Exception):
            raw = getattr(QgsNetworkRequestParameters, "AttributeUserAgentSuffix", None)
            if raw is None:
                enum = getattr(QgsNetworkRequestParameters, "RequestAttributes", None)
                raw = getattr(enum, "AttributeUserAgentSuffix", None)
            if raw is not None:
                attribute = QNetworkRequest.Attribute(int(getattr(raw, "value", raw)))
        _UA_ATTRIBUTE = attribute
        _UA_ATTRIBUTE_PROBED = True
    return _UA_ATTRIBUTE


def _identify(request: QNetworkRequest) -> None:
    """Name the plugin on a request: Referer always, User-Agent suffix where QGIS allows."""
    request.setRawHeader(b"Referer", REFERER.encode("ascii"))
    attribute = _user_agent_attribute()
    if attribute is not None:
        with contextlib.suppress(Exception):
            request.setAttribute(attribute, USER_AGENT)


def _transfer_seconds() -> float:
    """T = min(55 s, QGIS network timeout - 2 s), read from the user's QGIS."""
    timeout_ms = _transport.DEFAULT_QGIS_TIMEOUT_MS
    with contextlib.suppress(Exception):
        timeout_ms = QgsNetworkAccessManager.timeout()
    return _transport.transfer_seconds(timeout_ms)


def _form_body(query: str) -> bytes:
    # Standard form encoding: a literal "+" would reach the server as a space.
    return urlencode({"data": query}).encode("ascii")


def _host_name(url: str) -> str:
    return urlsplit(url).netloc or url


_LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})


def _address_allowed(url: QUrl) -> bool:
    """HTTPS anywhere; plain HTTP only to this computer (local test servers)."""
    if not url.isValid() or not url.host():
        return False
    scheme = str(url.scheme()).lower()
    return scheme == "https" or (scheme == "http" and str(url.host()).lower() in _LOOPBACK_HOSTS)


def _cancelled(feedback) -> bool:
    if feedback is None:
        return False
    try:
        return bool(feedback.isCanceled())
    except (AttributeError, RuntimeError, TypeError):
        return False


def _raise_if_cancelled(feedback) -> None:
    if _cancelled(feedback):
        raise OsmDownloadCancelled()


def _post_once(url: str, body: bytes, transfer_s: float, feedback=None) -> _transport.Verdict:
    """Send one request to one server and classify what came back.

    The transfer runs under its own QgsFeedback, cancelled either by the
    caller's feedback (Cancel) or as soon as the answer passes 64 MiB, so an
    oversized answer is never downloaded in full.  The HTTP status is read
    even when QGIS reports the request as failed, which it does for every
    4xx and 5xx reply.  Raises OsmDownloadCancelled when the caller cancelled.
    """
    address = QUrl(url)
    if not _address_allowed(address):
        return _transport.Verdict(_transport.HOST_ERROR, "is not a valid secure address")
    request = QNetworkRequest(address)
    if hasattr(request, "setTransferTimeout"):
        request.setTransferTimeout(max(1000, int(round(transfer_s * 1000))))
    request.setHeader(
        _network_enum("KnownHeaders", "ContentTypeHeader"),
        "application/x-www-form-urlencoded",
    )
    request.setRawHeader(b"Accept", b"application/json")
    _identify(request)

    guard = QgsFeedback()
    oversize = []

    def watch(received, _total) -> None:
        if received > MAX_RESPONSE_BYTES and not oversize:
            oversize.append(int(received))
            guard.cancel()

    client = QgsBlockingNetworkRequest()
    # For a POST, QGIS reports the bytes received through uploadProgress;
    # both signals are watched, and a direct connection lets the check run on
    # the thread doing the transfer.
    for name in ("downloadProgress", "uploadProgress"):
        signal = getattr(client, name, None)
        if signal is not None:
            signal.connect(watch, Qt.ConnectionType.DirectConnection)
    canceled = getattr(feedback, "canceled", None) if feedback is not None else None
    linked = False
    if canceled is not None and hasattr(canceled, "connect"):
        canceled.connect(guard.cancel)
        linked = True
    try:
        # QgsBlockingNetworkRequest ignores a feedback that is already
        # cancelled and would still send the POST: a Cancel that came before
        # this point (the link above is now in place) stops it here.
        _raise_if_cancelled(feedback)
        code = client.post(request, QByteArray(body), True, guard)
    except OsmDownloadCancelled:
        raise
    except Exception as error:  # noqa: BLE001 - one server's fault, not the download's
        _raise_if_cancelled(feedback)
        return _transport.Verdict(
            _transport.NETWORK, f"could not be reached (the request failed: {type(error).__name__})"
        )
    finally:
        if linked:
            with contextlib.suppress(TypeError, RuntimeError):
                canceled.disconnect(guard.cancel)
    _raise_if_cancelled(feedback)
    reply = client.reply()
    error_text = ""
    if code != _NO_NETWORK_ERROR:
        error_text = str(client.errorMessage() or "").strip()
    return _transport.classify(
        status=_reply_status(reply),
        body=b"" if oversize else bytes(reply.content()),
        error_code=_enum_int(reply.error()),
        error_text=error_text,
        oversize=bool(oversize),
        transfer_s=transfer_s,
    )


# --------------------------------------------------------------------------
# The servers' rests, kept across QGIS sessions
# --------------------------------------------------------------------------
_SETTINGS_ERRORS = (ValueError, TypeError, RuntimeError, OSError, AttributeError)


def _restore_board(board: _hosts.HostBoard) -> None:
    """Merge the rests and successes kept by earlier sessions into `board`."""
    with contextlib.suppress(*_SETTINGS_ERRORS):
        settings = QgsSettings()
        raw = settings.value(f"{SETTINGS_GROUP}/board", "", type=str)
        last = settings.value(f"{SETTINGS_GROUP}/last_worked", "", type=str)
        stored = json.loads(raw) if isinstance(raw, str) and raw else {}
        board.restore({"hosts": stored if isinstance(stored, dict) else {},
                       "last_worked": last if isinstance(last, str) else ""})


def _save_board(board: _hosts.HostBoard) -> None:
    """Keep the board's rests and the last server that worked in QgsSettings."""
    with contextlib.suppress(*_SETTINGS_ERRORS):
        snapshot = board.snapshot()
        settings = QgsSettings()
        settings.setValue(f"{SETTINGS_GROUP}/board", json.dumps(snapshot.get("hosts") or {}, sort_keys=True))
        settings.setValue(f"{SETTINGS_GROUP}/last_worked", str(snapshot.get("last_worked") or ""))
        settings.sync()


def forget_hosts() -> None:
    """Forget every server's rest and success, in this session and in QgsSettings.

    For test suites; a person never needs it, and the rests protect the
    servers, so the plugin offers no button for it.
    """
    BOARD.reset()
    with contextlib.suppress(*_SETTINGS_ERRORS):
        settings = QgsSettings()
        settings.remove(SETTINGS_GROUP)
        settings.sync()


# --------------------------------------------------------------------------
# A click: saved copies, else the servers, part by part
# --------------------------------------------------------------------------
def _say(status, text: str) -> None:
    """Report one step; a fault in the status display never breaks the download."""
    if status is not None and text:
        with contextlib.suppress(Exception):
            status(text)


def _progress(feedback, percent: float) -> None:
    if feedback is not None and hasattr(feedback, "setProgress"):
        with contextlib.suppress(Exception):
            feedback.setProgress(max(0.0, min(100.0, float(percent))))


def _cancellable_wait(seconds: float, feedback) -> None:
    """Wait, checking for Cancel at short intervals."""
    deadline = time.monotonic() + max(0.0, float(seconds))
    while True:
        _raise_if_cancelled(feedback)
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return
        time.sleep(min(0.1, remaining))


def _reask_after_drop() -> bool:
    """Qt 5 already re-sends a POST three times after a dropped connection
    (measured on QGIS 3.44, Qt 5.15.13); Qt 6 sends once, so only there does
    the plugin ask the same server once more."""
    try:
        return int(QT_VERSION) >= 0x060000
    except (TypeError, ValueError):
        return False


def _host_answers(heard: Dict[str, _transport.Verdict], hosts: Sequence[str],
                  board: _hosts.HostBoard) -> List[_transport.HostAnswer]:
    """Each server and its answer: during this click, or else the last time."""
    answers = []
    for host in hosts:
        verdict = heard.get(host)
        if verdict is not None:
            answers.append(_transport.HostAnswer(host, verdict.kind, verdict.detail))
            continue
        last = board.last_answer(host)
        if last is not None and last[0] != _transport.OK:
            answers.append(_transport.HostAnswer(host, last[0], last[1], asked=False, age_s=last[2]))
        else:
            answers.append(_transport.HostAnswer(host, asked=False))
    return answers


def _failure_message(heard: Dict[str, _transport.Verdict], hosts: Sequence[str],
                     board: _hosts.HostBoard) -> str:
    """The plain message for a click that got nothing, naming every server."""
    wait_s = min((board.remaining(host) for host in hosts), default=0.0)
    return _transport.failure_report(_host_answers(heard, hosts, board), wait_s).message


def _all_resting(board: _hosts.HostBoard, hosts: Sequence[str]) -> bool:
    """True when no server can be asked within the longest wait for a rest."""
    return bool(hosts) and all(
        board.remaining(host) > _hosts.MAX_REST_WAIT_SECONDS for host in hosts)


def _fetch(min_lat, min_lon, max_lat, max_lon, *, use_cache: bool, status, feedback,
           datasets=None, transfer_cap: Optional[float] = None) -> dict:
    tiles = tile_bbox(min_lat, min_lon, max_lat, max_lon)
    # Validate the selection before anything is sent.
    _overpass_selection(*tiles[0], datasets)
    _raise_if_cancelled(feedback)
    transfer_s = _transfer_seconds()
    if transfer_cap is not None:
        with contextlib.suppress(TypeError, ValueError):
            cap = float(transfer_cap)
            if math.isfinite(cap) and cap > 0:
                transfer_s = max(1.0, min(transfer_s, cap))
    server_timeout = _transport.server_timeout_seconds(transfer_s)

    board = BOARD
    _restore_board(board)
    urls: Dict[str, str] = {}
    for endpoint in tuple(OVERPASS_ENDPOINTS):
        urls.setdefault(_host_name(str(endpoint)), str(endpoint))
    hosts = tuple(urls)
    if not hosts:
        raise OsmDownloadError("No OpenStreetMap server is configured, so nothing was downloaded.")

    total = len(tiles)
    multi = total > 1
    budget = _hosts.RequestBudget(MAX_CLICK_REQUESTS)
    payloads: List[dict] = []
    missing: List[int] = []
    cached = 0
    heard: Dict[str, _transport.Verdict] = {}
    stopped = ""
    timeout_parts = 0
    reask = _reask_after_drop()
    if multi:
        _say(status, f"This area is downloaded in {total} parts.")

    lead: List[str] = []
    for host in hosts:
        if board.remaining(host) > 0:
            last = board.last_answer(host)
            why = f" after it {last[1]}" if last is not None and last[1] else ""
            lead.append(f"{host} is resting{why}, so it is not asked. ")
    pending: List[str] = []
    announced: List[str] = []
    prefix = [""]

    def opening() -> str:
        text = "".join(lead) + "".join(pending)
        lead.clear()
        pending.clear()
        return text

    def pause(seconds: float, host: str, why: str) -> None:
        if why == _hosts.RESTING:
            _say(status, f"{prefix[0]}{opening()}Every server is resting after an earlier "
                         f"answer; waiting until {host} accepts requests again.")
        elif why == _hosts.RESET:
            _say(status, f"{prefix[0]}{host} closed the connection; asking it once more in "
                         f"{_transport.duration_text(seconds)}.")
        elif why == _hosts.IN_USE and host not in announced:
            announced.append(host)
            _say(status, f"Waiting for {host}: another download is using it.")
        _cancellable_wait(seconds, feedback)

    def notify(host: str, verdict: _transport.Verdict) -> None:
        rest = board.remaining(host)
        if rest >= 1:
            pending.append(f"{host} {verdict.detail}, so it rests for "
                           f"{_transport.duration_text(rest)}. ")
        else:
            pending.append(f"{host} {verdict.detail}. ")

    try:
        for index, tile in enumerate(tiles):
            _raise_if_cancelled(feedback)
            _progress(feedback, 80.0 * index / total)
            prefix[0] = f"Part {index + 1} of {total}: " if multi else ""
            key = _cache_key(*tile, datasets)
            if use_cache:
                hit = _read_cache_entry(key)
                if hit is not None:
                    payload, saved_at = hit
                    age = _transport.age_text(time.time() - saved_at)
                    text = f"using the copy downloaded {age} ago; no request sent."
                    _say(status, prefix[0] + (text if multi else text[0].upper() + text[1:]))
                    payloads.append(payload)
                    cached += 1
                    continue
            if stopped:
                missing.append(index + 1)
                continue
            if _all_resting(board, hosts):
                stopped = _transport.STOP_RESTING
                missing.append(index + 1)
                continue
            body = _form_body(_overpass_query(*tile, datasets=datasets, timeout_s=server_timeout))

            def attempt(host: str, body=body) -> _transport.Verdict:
                _say(status, f"{prefix[0]}{opening()}Asking {host}…")
                verdict = _post_once(urls[host], body, transfer_s, feedback)
                if verdict.ok and len((verdict.payload or {}).get("elements") or ()) > MAX_ELEMENTS_PER_TILE:
                    return _transport.Verdict(
                        _transport.TOO_LARGE, f"sent more than {MAX_ELEMENTS_PER_TILE:,} objects")
                return verdict

            result = _hosts.ask_hosts(hosts, attempt, board, pause, notify,
                                      reset_retry=reask, budget=budget)
            for host, verdict in result.answers:
                heard[host] = verdict
            if result.kind == _transport.OK and isinstance(result.payload, dict):
                _write_cache(key, result.payload)
                payloads.append(result.payload)
                _say(status, f"{prefix[0]}{opening()}{result.host} delivered the data.")
                continue
            missing.append(index + 1)
            pending.clear()
            if result.kind == _hosts.NO_BUDGET:
                stopped = _transport.STOP_BUDGET
            elif result.kind == _transport.BAD_QUERY:
                stopped = _transport.STOP_INVALID
            elif result.answers and all(verdict.kind == _transport.SERVER_TIMEOUT
                                        for _host, verdict in result.answers):
                timeout_parts += 1
                if timeout_parts >= MAX_TIMEOUT_PARTS:
                    stopped = _transport.STOP_TIMEOUTS
    finally:
        _save_board(board)

    if not payloads:
        message = _failure_message(heard, hosts, board)
        if stopped == _transport.STOP_BUDGET and not heard:
            message = (f"This run used all {budget.limit} of its requests, so nothing was "
                       "downloaded. Choose a smaller area.")
        raise OsmDownloadError(message)

    merged: Dict[Tuple[str, str], dict] = {}
    for payload in payloads:
        for element in payload.get("elements") or ():
            merged.setdefault((str(element.get("type", "")), str(element.get("id", ""))), element)
        if len(merged) > MAX_TOTAL_ELEMENTS:
            raise OsmDownloadError(
                f"This area holds more than {MAX_TOTAL_ELEMENTS:,} OpenStreetMap objects, "
                "so it was not built. Choose a smaller Max area."
            )
    notes: List[str] = []
    if missing:
        notes.append(_transport.missing_parts_note(
            len(missing), total, _host_answers(heard, hosts, board), stopped, budget.limit))
        _say(status, notes[-1])
    _progress(feedback, 80.0)
    return {
        "elements": list(merged.values()),
        "_download": {
            "tiles_total": total,
            "tiles_succeeded": len(payloads),
            "tiles_failed": tuple(missing),
            "tiles_cached": cached,
            "requests": budget.spent,
            "notes": notes,
        },
    }


def fetch_overpass_ex(min_lat, min_lon, max_lat, max_lon, *, use_cache=True,
                      status=None, feedback=None, datasets=None) -> dict:
    """Download the OSM data for a bounding box, politely.

    `use_cache` lets a saved copy younger than a week answer a part without
    a request; with False the servers are asked and a good answer replaces
    the saved copy.  `status(text)` is called on the calling thread with one
    short plain sentence per step.  `feedback` is a QgsFeedback: cancelling
    it aborts the transfer at once and raises OsmDownloadCancelled.  Any
    other failure raises OsmDownloadError whose text is the message for the
    user.  The payload's ``_download`` entry says how many parts there were,
    which are missing (``tiles_failed``) and carries ``notes``.
    """
    return _fetch(min_lat, min_lon, max_lat, max_lon, use_cache=bool(use_cache),
                  status=status, feedback=feedback, datasets=datasets)


class _CancelBridge:
    """A QgsFeedback that is cancelled as soon as `cancel_check()` turns true
    or the caller's own QgsFeedback is cancelled.

    The 0.51 API takes a polled `cancel_check`; a small watcher thread polls
    it every 0.1 s so a hung transfer is aborted at once, not when it ends.
    """

    def __init__(self, cancel_check: Optional[Callable[[], bool]], outer=None) -> None:
        self.feedback = QgsFeedback()
        self._check = cancel_check
        self._outer = outer
        self._stop = threading.Event()
        self._thread = None

    def _polled(self) -> bool:
        if _cancelled(self._outer):
            return True
        if self._check is None:
            return False
        try:
            return bool(self._check())
        except (RuntimeError, TypeError, ValueError, AttributeError):
            return False

    def _watch(self) -> None:
        while not self._stop.wait(0.1):
            if self._polled():
                self.feedback.cancel()
                return

    def __enter__(self) -> QgsFeedback:
        if self._polled():
            self.feedback.cancel()
        else:
            self._thread = threading.Thread(target=self._watch, name="oq3d-cancel", daemon=True)
            self._thread.start()
        return self.feedback

    def __exit__(self, *_exc) -> bool:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(1.0)
        return False


def _split_feedback(feedback, status):
    """(status callable, QgsFeedback or None): the 0.51 API passed the status
    callable as `feedback`; contract C1 passes a QgsFeedback there."""
    if feedback is not None and not isinstance(feedback, QgsFeedback) and callable(feedback):
        return (status if status is not None else feedback), None
    return status, feedback


def fetch_overpass(min_lat: float, min_lon: float, max_lat: float, max_lon: float,
                   timeout_s: int = DEFAULT_TIMEOUT_S, feedback=None,
                   use_cache: bool = True, datasets=None, cancel_check=None) -> dict:
    """The 0.51 API: `feedback` is a status callable (or a QgsFeedback),
    `cancel_check` a callable that returns True once the user cancelled,
    `timeout_s` caps the transfer."""
    status, outer = _split_feedback(feedback, None)
    if cancel_check is None:
        return _fetch(min_lat, min_lon, max_lat, max_lon, use_cache=bool(use_cache),
                      status=status, feedback=outer, datasets=datasets, transfer_cap=timeout_s)
    with _CancelBridge(cancel_check, outer) as bridged:
        return _fetch(min_lat, min_lon, max_lat, max_lon, use_cache=bool(use_cache),
                      status=status, feedback=bridged, datasets=datasets, transfer_cap=timeout_s)


EMPTY_AREA_MESSAGE = (
    "OpenStreetMap has none of the selected map data in this area: the server "
    "sent an empty answer. Choose another or a larger area, or try again later "
    "if you expected map data here."
)


# --------------------------------------------------------------------------
# OSM element -> attributes (finite, capped)
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

#: Metres per level when heights come from level counts.
FLOOR_HEIGHT_M = 3.0
#: Level counts are clamped to 1-200, so one absurd tag never sinks the
#: download (levels=inf raised OverflowError in 0.51.1; levels=1e10
#: overflowed the Int field and the provider dropped every later building).
MAX_LEVELS = 200
_MAX_ROOF_LEVELS = 20
#: Building heights are clamped to 1-1000 m; bases to 0-1000 m.
HEIGHT_RANGE_M = (1.0, 1000.0)
_BASE_RANGE_M = (0.0, 1000.0)

_NUMBER = r"([-+]?(?:\d+(?:[.,]\d*)?|[.,]\d+))"
_FEET_RE = re.compile(
    _NUMBER + r"\s*(?:ft|feet|foot|'|′)"
    r"(?:\s*" + _NUMBER + r"\s*(?:in|inches?|\"|″))?",
    re.IGNORECASE,
)
_METRIC_RE = re.compile(
    r"([-+]?(?:\d+(?:[.,]\d*)?|[.,]\d+)(?:[eE][-+]?\d+)?)\s*"
    r"(m|met(?:er|re)s?|cm|km|ft|feet|foot|in|inches?)?",
    re.IGNORECASE,
)


def _parse_osm_number(value):
    """Parse an OSM numeric tag ('12', '12.5', '12 m', '3;4', '45 ft') -> float or None.

    A value that does not parse to a finite number ("inf", "nan", or
    ``"9" * 400``, which is infinite as a float) is treated as absent.
    """
    if value is None:
        return None
    text = str(value).split(";", 1)[0].strip().lstrip("~").strip()
    if not text or len(text) > 64:
        return None
    feet_match = _FEET_RE.fullmatch(text)
    if feet_match:
        feet = float(feet_match.group(1).replace(",", "."))
        inches = float((feet_match.group(2) or "0").replace(",", "."))
        result = feet * 0.3048 + inches * 0.0254
        return result if math.isfinite(result) else None
    match = _METRIC_RE.fullmatch(text)
    if not match:
        return None
    try:
        number = float(match.group(1).replace(",", "."))
    except (OverflowError, ValueError):
        return None
    unit = (match.group(2) or "").lower()
    if unit == "cm":
        number /= 100.0
    elif unit == "km":
        number *= 1000.0
    elif unit in {"ft", "feet", "foot"}:
        number *= 0.3048
    elif unit.startswith("in"):
        number *= 0.0254
    return number if math.isfinite(number) else None


def _round_half_up(value: float) -> int:
    """2.5 -> 3 (Python's round() would give 2)."""
    return int(math.floor(float(value) + 0.5))


def _clamp_levels(number: float, low: int, high: int) -> int:
    """A finite level count, rounded half up and clamped (never overflows)."""
    if number >= high:
        return high
    if number <= low:
        return low
    return max(low, min(high, _round_half_up(number)))


def _building_height(tags: dict):
    """OSM ``height`` in metres clamped to 1-1000, or None when absent or not positive."""
    height = _parse_osm_number(tags.get("height"))
    if height is None or height <= 0:
        return None
    return round(max(HEIGHT_RANGE_M[0], min(HEIGHT_RANGE_M[1], height)), 1)


def _building_levels(tags: dict) -> int:
    """Floor count for the export's OSM-native ``building_levels`` column.

    Priority: building:levels (+ roof:levels) -> height / 3 m -> a default by
    the OSM ``building`` type -> 3.  Always an integer in 1-200.
    """
    base = None
    for key in ("building:levels", "levels"):
        number = _parse_osm_number(tags.get(key))
        if number is not None:
            base = _clamp_levels(number, 1, MAX_LEVELS)
            break
    if base is None:
        height = _building_height(tags)
        if height is not None:
            base = _clamp_levels(height / FLOOR_HEIGHT_M, 1, MAX_LEVELS)
    if base is None:
        btype = str(tags.get("building") or tags.get("building:part") or "").lower()
        return _DEFAULT_FLOORS_BY_OSM.get(btype, 3)
    roof = _parse_osm_number(tags.get("roof:levels"))
    if roof is not None and roof > 0:
        base += _clamp_levels(roof, 0, _MAX_ROOF_LEVELS)
    return max(1, min(MAX_LEVELS, base))


def _building_min_height(tags: dict) -> float:
    """Base elevation offset (m above ground) from ``min_height`` or
    ``building:min_level``, clamped to 0-1000 m."""
    for key in ("min_height", "building:min_height"):
        val = _parse_osm_number(tags.get(key))
        if val is not None and val > 0:
            return round(min(_BASE_RANGE_M[1], float(val)), 1)
    for key in ("building:min_level", "min_level"):
        lvl = _parse_osm_number(tags.get(key))
        if lvl is not None and lvl > 0:
            return round(_clamp_levels(lvl, 0, MAX_LEVELS - 1) * FLOOR_HEIGHT_M, 1)
    return 0.0


# Highways that are not there to drive on: not built yet, or gone (P14).
_NOT_BUILT_HIGHWAYS = frozenset({
    "proposed", "construction", "abandoned", "razed", "demolished", "disused",
})
# Ways underground: drawn on the surface they would be wrong.
_UNDERGROUND_TUNNELS = frozenset({"yes", "culvert"})


def _is_underground(tags: dict) -> bool:
    return _tag(tags, "tunnel") in _UNDERGROUND_TUNNELS


def _is_drawn_highway(tags: dict) -> bool:
    """True for a highway that exists on the surface today."""
    return _tag(tags, "highway") not in _NOT_BUILT_HIGHWAYS and not _is_underground(tags)


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
    return str(tags.get("waterway") or "").lower() or "stream"


def _waterway_width(tags: dict) -> float:
    for key in ("width", "est_width"):
        n = _parse_osm_number(tags.get(key))
        if n is not None and n > 0:
            return max(0.5, min(1000.0, n))
    return _WATERWAY_WIDTH.get(_waterway_class(tags), 3.0)


def _road_width(tags: dict):
    """A tagged carriageway width in metres (0-100], else None."""
    width = _parse_osm_number(tags.get("width"))
    if width is None or width <= 0:
        return None
    return round(min(100.0, width), 1)


def _tree_height(tags: dict) -> float:
    """A tagged tree height in metres (0.5-120), else 6 m."""
    height = _parse_osm_number(tags.get("height"))
    if height is None or height <= 0:
        return 6.0
    return round(max(0.5, min(120.0, height)), 1)


def _tag(tags: dict, key: str) -> str:
    """Lower-cased OSM tag value, or '' when absent — emitted verbatim as a column."""
    return str(tags.get(key) or "").strip().lower()


def _is_water_area(tags: dict) -> bool:
    """True for an OSM polygon that should render as open water (a filled surface)."""
    if tags.get("natural") == "water":
        return True
    if tags.get("waterway") == "riverbank":
        return True
    if tags.get("landuse") in ("reservoir", "basin"):
        return True
    return bool(str(tags.get("water") or "").strip())


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


# --------------------------------------------------------------------------
# OSM geometry: rings stitched from member ways, holes by nesting (P8)
# --------------------------------------------------------------------------
def _point_key(point) -> tuple:
    # Overpass repeats shared node coordinates, but JSON float round-tripping can
    # introduce insignificant noise. Sub-millimetre precision is ample here.
    return round(point.x(), 11), round(point.y(), 11)


def _member_points(geometry) -> list:
    """Finite coordinates of an Overpass way or member ``geometry`` list,
    consecutive duplicates removed."""
    points = []
    for item in geometry or []:
        if not isinstance(item, dict) or "lon" not in item or "lat" not in item:
            continue
        try:
            lon, lat = float(item["lon"]), float(item["lat"])
        except (TypeError, ValueError):
            continue
        if not math.isfinite(lon) or not math.isfinite(lat):
            continue
        point = QgsPointXY(lon, lat)
        if not points or _point_key(points[-1]) != _point_key(point):
            points.append(point)
    return points


def _stitch_member_rings(paths: list) -> list:
    """Join split or reversed member ways (two points or more) into closed rings.

    0.51.1 closed every open member on its own, which drew a split concave
    outline as a set of wrong shapes and dropped two-point ways.
    """
    pending = [list(path) for path in paths if len(path) >= 2]
    rings = []
    while pending:
        chain = pending.pop(0)
        while _point_key(chain[0]) != _point_key(chain[-1]):
            start, end = _point_key(chain[0]), _point_key(chain[-1])
            match_index = None
            joined = None
            for index, path in enumerate(pending):
                path_start, path_end = _point_key(path[0]), _point_key(path[-1])
                if end == path_start:
                    joined = chain + path[1:]
                elif end == path_end:
                    joined = chain + list(reversed(path[:-1]))
                elif start == path_end:
                    joined = path[:-1] + chain
                elif start == path_start:
                    joined = list(reversed(path[1:])) + chain
                else:
                    continue
                match_index = index
                break
            if match_index is None:
                break
            chain = joined
            pending.pop(match_index)
        if _point_key(chain[0]) == _point_key(chain[-1]):
            if len({_point_key(point) for point in chain[:-1]}) >= 3:
                rings.append(chain)
    return rings


def _polygon_from_ring(points: list) -> Optional[QgsGeometry]:
    polygon = QgsGeometry.fromPolygonXY([points])
    if polygon is None or polygon.isEmpty() or polygon.area() <= 0:
        return None
    if not polygon.isGeosValid():
        polygon = polygon.makeValid()
    if polygon is None or polygon.isEmpty():
        return None
    if QgsWkbTypes.geometryType(polygon.wkbType()) != _POLYGON_GEOMETRY:
        converted = QgsGeometry(polygon)
        if not converted.convertGeometryCollectionToSubclass(_POLYGON_GEOMETRY):
            return None
        polygon = converted
    return polygon if not polygon.isEmpty() and polygon.area() > 0 else None


def _nested_area(rings: list) -> Optional[QgsGeometry]:
    """One area from closed rings, holes decided by nesting (even/odd depth).

    A ring inside an even number of other rings is land, inside an odd number
    a hole -- what libosmium, osm2pgsql and OGR do -- so an island in a pond
    inside a park survives (subtracting every inner ring from the union of
    the outer rings erased it in 0.51.1).
    """
    polygons = [polygon for polygon in (_polygon_from_ring(ring) for ring in rings)
                if polygon is not None]
    if not polygons:
        return None
    polygons.sort(key=lambda item: -item.area())
    boxes = [polygon.boundingBox() for polygon in polygons]
    depths = []
    for index, polygon in enumerate(polygons):
        probe = polygon.pointOnSurface()
        depths.append(sum(
            1 for outer in range(index)
            if boxes[outer].contains(boxes[index]) and polygons[outer].contains(probe)
        ))
    result = None
    for level in sorted({depth - depth % 2 for depth in depths}):
        shells = [item for item, depth in zip(polygons, depths) if depth == level]
        holes = [item for item, depth in zip(polygons, depths) if depth == level + 1]
        part = QgsGeometry.unaryUnion(shells)
        if holes and part is not None and not part.isEmpty():
            part = part.difference(QgsGeometry.unaryUnion(holes))
        if part is None or part.isEmpty():
            continue
        result = part if result is None else QgsGeometry.unaryUnion([result, part])
    return result


def _relation_multipolygon(element) -> Optional[QgsGeometry]:
    """Assemble a (multi)polygon from a relation's member ways.

    Split or reversed member ways are stitched into rings (outer and inner
    roles separately), and which ring is a hole is decided by nesting, so an
    island inside a hole is kept.  Returns None when no usable ring exists.
    """
    outer_paths, inner_paths = [], []
    for member in (element.get("members") or []):
        if not isinstance(member, dict) or member.get("type") != "way":
            continue
        role = str(member.get("role") or "").lower()
        if role not in ("", "outer", "inner"):
            continue
        points = _member_points(member.get("geometry"))
        if len(points) < 2:
            continue
        (inner_paths if role == "inner" else outer_paths).append(points)
    poly = _nested_area(_stitch_member_rings(outer_paths) + _stitch_member_rings(inner_paths))
    if poly is None or poly.isEmpty() or poly.area() <= 0:
        return None
    if not poly.isGeosValid():
        poly = poly.makeValid()
    if poly is None or poly.isEmpty():
        return None
    if QgsWkbTypes.geometryType(poly.wkbType()) != _POLYGON_GEOMETRY:
        if not poly.convertGeometryCollectionToSubclass(_POLYGON_GEOMETRY):
            return None
    return poly


def _ring_from_geometry(geometry) -> Optional[QgsGeometry]:
    """A polygon from a closed Overpass ``geometry`` list; None when it is open.

    An open way is never closed here: joining its ends would invent an
    outline that OSM does not have.
    """
    points = _member_points(geometry)
    if len(points) < 4 or _point_key(points[0]) != _point_key(points[-1]):
        return None
    return _polygon_from_ring(points)


def _way_polygon(element) -> Optional[QgsGeometry]:
    return _ring_from_geometry(element.get("geometry"))


def _element_polygon(element) -> Optional[QgsGeometry]:
    """Polygon for a building/green element: relation -> multipolygon, else ring."""
    if element.get("type") == "relation":
        return _relation_multipolygon(element)
    return _way_polygon(element)


def _way_polyline(element) -> Optional[QgsGeometry]:
    points = _member_points(element.get("geometry"))
    if len(points) < 2:
        return None
    return QgsGeometry.fromPolylineXY(points)


def _node_point(element) -> Optional[QgsGeometry]:
    if "lon" not in element or "lat" not in element:
        return None
    try:
        lon, lat = float(element["lon"]), float(element["lat"])
    except (TypeError, ValueError):
        return None
    if not math.isfinite(lon) or not math.isfinite(lat):
        return None
    return QgsGeometry.fromPointXY(QgsPointXY(lon, lat))


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
    """Main thread only: reads the project's transform context."""
    options = QgsVectorFileWriter.SaveVectorOptions()
    options.driverName = "GeoJSON"
    options.fileEncoding = "UTF-8"
    QgsVectorFileWriter.writeAsVectorFormatV3(
        layer, str(path), QgsProject.instance().transformContext(), options
    )


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


def _hand_to_main_thread(layers) -> None:
    """Memory layers built on a worker thread belong to it; move them to the
    main thread so the caller can add them to the project there (C2)."""
    with contextlib.suppress(Exception):
        app = QgsApplication.instance()
        main = app.thread() if app is not None else None
        if main is None or QThread.currentThread() == main:
            return
        for layer in layers:
            layer.moveToThread(main)


# --------------------------------------------------------------------------
# Main download + clip (contract C1; worker-safe, contract C2)
# --------------------------------------------------------------------------
def download_osm_for_area(area_utm: QgsGeometry, epsg_dest: int, feedback=None,
                          use_cache: bool = True, datasets=None, cancel_check=None,
                          *, status=None) -> dict:
    """Fetch OSM for the boundary's bbox, reproject to UTM, clip to the boundary.

    ``area_utm`` is the study-boundary polygon (circle, rounded rectangle,
    rectangle or exact polygon) in the EPSG:``epsg_dest`` metric CRS.

    Contract C1: ``status(text)`` is called on the calling (worker) thread
    with plain sentences; ``feedback`` is a QgsFeedback whose Cancel aborts
    at once and raises :class:`OsmDownloadCancelled` ("Download cancelled.").
    The 0.51 call style -- a status callable as ``feedback`` and a polled
    ``cancel_check`` -- still works.  Contract C2: the project is never read
    or changed; the memory layers are moved to the main thread for the
    caller to add there.

    Returns a dict of memory layers keyed by role plus ``counts``,
    ``download`` (parts total/succeeded/missing) and ``notes``: plain
    sentences the caller must show, for example that parts of the area are
    missing.
    """
    status, outer = _split_feedback(feedback, status)
    if cancel_check is None:
        return _download_osm_for_area(area_utm, epsg_dest, outer, bool(use_cache), datasets, status)
    with _CancelBridge(cancel_check, outer) as bridged:
        return _download_osm_for_area(area_utm, epsg_dest, bridged, bool(use_cache), datasets, status)


def _download_osm_for_area(area_utm, epsg_dest, feedback, use_cache, datasets, status) -> dict:
    _raise_if_cancelled(feedback)
    dst_crs = QgsCoordinateReferenceSystem.fromEpsgId(epsg_dest)
    wgs_crs = QgsCoordinateReferenceSystem.fromEpsgId(4326)
    # A default context, never the project's: this runs on a worker thread,
    # and WGS84 <-> WGS84/UTM needs no datum transformation.
    context = QgsCoordinateTransformContext()
    to_wgs = QgsCoordinateTransform(dst_crs, wgs_crs, context)
    to_utm = QgsCoordinateTransform(wgs_crs, dst_crs, context)

    wgs_rect = to_wgs.transformBoundingBox(area_utm.boundingBox())
    min_lon, min_lat = wgs_rect.xMinimum(), wgs_rect.yMinimum()
    max_lon, max_lat = wgs_rect.xMaximum(), wgs_rect.yMaximum()
    clip_boundary = _valid_clip_boundary(area_utm)
    enabled_datasets = set(_ALL_DATASETS if datasets is None else datasets)

    payload = _fetch(min_lat, min_lon, max_lat, max_lon, use_cache=use_cache,
                     status=status, feedback=feedback, datasets=datasets)
    elements = payload.get("elements") or []
    download = dict(payload.get("_download") or {})
    notes = list(download.get("notes") or [])
    if not elements:
        if download.get("tiles_failed"):
            raise OsmDownloadError(" ".join(notes) or EMPTY_AREA_MESSAGE)
        raise OsmDownloadError(EMPTY_AREA_MESSAGE)
    _say(status, f"Building the layers from {len(elements):,} OpenStreetMap objects…")

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
    # Street furniture as points.
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
    layers = (buildings_layer, roads_layer, bikelanes_layer, greens_layer, trees_layer,
              waterlines_layer, waterareas_layer, busstops_layer, benches_layer, lights_layer,
              trashbins_layer)

    batches = tuple(_FeatureBatch(provider) for provider in (
        b_pr, r_pr, bl_pr, g_pr, t_pr, w_pr, wa_pr, bs_pr, be_pr, li_pr, tb_pr,
    ))
    (b_pr, r_pr, bl_pr, g_pr, t_pr, w_pr, wa_pr,
     bs_pr, be_pr, li_pr, tb_pr) = batches

    counts = {
        "buildings": 0, "roads": 0, "bikelanes": 0, "greens": 0, "trees": 0,
        "trees_scattered": 0, "parking": 0, "plazas": 0, "waterlines": 0,
        "waterareas": 0, "busstops": 0, "benches": 0, "lights": 0,
        "trashbins": 0, "skipped": 0, "not_on_surface": 0,
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

    def point_in_area(element):
        geom = _node_point(element)
        if not geom:
            return None
        g = QgsGeometry(geom)
        if not _geometry_transform_ok(g.transform(to_utm)) or not clip_boundary.contains(g):
            return None
        return g

    def polygon_feature(element, attributes):
        base = _element_polygon(element)
        if not base:
            counts["skipped"] += 1
            return None
        clipped = clip_to_area(base)
        if clipped is None:
            counts["skipped"] += 1
            return None
        feat = QgsFeature()
        feat.setGeometry(clipped)
        feat.setAttributes(attributes(clipped) if callable(attributes) else attributes)
        return feat

    scattered_trees = 0
    total_elements = len(elements)
    for elem_idx, element in enumerate(elements):
        if elem_idx % 250 == 0:
            _raise_if_cancelled(feedback)
            _progress(feedback, 80.0 + 20.0 * elem_idx / max(1, total_elements))
        if not isinstance(element, dict):
            counts["skipped"] += 1
            continue
        etype = element.get("type")
        tags = element.get("tags") or {}
        if not isinstance(tags, dict):
            tags = {}
        osm_id = str(element.get("id", ""))

        if etype == "node" and tags.get("natural") == "tree":
            g = point_in_area(element)
            if g is None:
                counts["skipped"] += 1
                continue
            feat = QgsFeature()
            feat.setGeometry(g)
            feat.setAttributes([osm_id, "tree", _tree_height(tags)])
            t_pr.addFeatures([feat])
            counts["trees"] += 1
            continue

        if etype == "node":
            # Street furniture points: bus stops, benches, street lamps, bins.
            g = point_in_area(element)
            if g is None:
                counts["skipped"] += 1
                continue
            feat = QgsFeature()
            feat.setGeometry(g)
            if tags.get("highway") == "bus_stop" or tags.get("public_transport") == "platform":
                feat.setAttributes([osm_id, _tag(tags, "highway") or "bus_stop", tags.get("name", "")])
                bs_pr.addFeatures([feat])
                counts["busstops"] += 1
            elif tags.get("amenity") == "bench":
                feat.setAttributes([osm_id, "bench"])
                be_pr.addFeatures([feat])
                counts["benches"] += 1
            elif tags.get("highway") == "street_lamp":
                feat.setAttributes([osm_id, "street_lamp"])
                li_pr.addFeatures([feat])
                counts["lights"] += 1
            elif tags.get("amenity") == "waste_basket":
                feat.setAttributes([osm_id, "waste_basket"])
                tb_pr.addFeatures([feat])
                counts["trashbins"] += 1
            else:
                counts["skipped"] += 1
            continue

        if etype not in ("way", "relation"):
            counts["skipped"] += 1
            continue

        if tags.get("building") or tags.get("building:part"):
            levels = _building_levels(tags)
            is_part_only = bool(tags.get("building:part")) and not bool(tags.get("building"))
            b_tag = _tag(tags, "building") or _tag(tags, "building:part")
            if b_tag == "yes" and is_part_only:
                b_tag = "part"

            def building_attributes(clipped, levels=levels, is_part_only=is_part_only, b_tag=b_tag):
                try:
                    footprint = 0.0 if is_part_only else round(clipped.area(), 1)
                except Exception:
                    footprint = None
                if footprint is not None and not math.isfinite(footprint):
                    footprint = None
                gfa = round(footprint * levels, 1) if footprint else (0.0 if is_part_only else None)
                return [osm_id, b_tag, levels, _building_height(tags), _building_min_height(tags),
                        footprint, gfa, tags.get("name", "")]

            feat = polygon_feature(element, building_attributes)
            if feat is not None:
                b_pr.addFeatures([feat])
                counts["buildings"] += 1
            continue

        # Water areas (lakes, ponds, riverbanks, reservoirs, bays).
        if _is_water_area(tags):
            feat = polygon_feature(element, [
                osm_id, _tag(tags, "natural"), _tag(tags, "waterway"), _tag(tags, "landuse"),
                tags.get("name", ""),
            ])
            if feat is not None:
                wa_pr.addFeatures([feat])
                counts["waterareas"] += 1
            continue

        # Parking lots
        if tags.get("amenity") == "parking":
            feat = polygon_feature(element, [osm_id, "", "", "", "parking", "", "", tags.get("name", "")])
            if feat is not None:
                g_pr.addFeatures([feat])
                counts["greens"] += 1
                counts["parking"] += 1
            continue

        # Paved public squares/plazas
        if _is_paved_area(tags):
            feat = polygon_feature(element, [
                osm_id, "", "", "", _tag(tags, "amenity"), _tag(tags, "highway"),
                _tag(tags, "place"), tags.get("name", ""),
            ])
            if feat is not None:
                g_pr.addFeatures([feat])
                counts["greens"] += 1
                counts["plazas"] += 1
            continue

        if etype == "way" and tags.get("highway"):
            # Tunnels and roads that are proposed, under construction or gone
            # are not drawn as ordinary surface roads (P14).
            if not _is_drawn_highway(tags):
                counts["not_on_surface"] += 1
                continue
            base = _way_polyline(element)
            if not base:
                counts["skipped"] += 1
                continue
            clipped = clip_to_area(base)
            if clipped is None:
                counts["skipped"] += 1
                continue
            highway = _tag(tags, "highway")
            feat = QgsFeature()
            feat.setGeometry(clipped)
            feat.setAttributes([osm_id, highway, _road_width(tags), tags.get("name", "")])
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
            # A stream in a culvert or tunnel runs underground.
            if _is_underground(tags):
                counts["not_on_surface"] += 1
                continue
            base = _way_polyline(element)
            if not base:
                counts["skipped"] += 1
                continue
            clipped = clip_to_area(base)
            if clipped is None:
                counts["skipped"] += 1
                continue
            feat = QgsFeature()
            feat.setGeometry(clipped)
            feat.setAttributes([
                osm_id, _tag(tags, "waterway"), round(_waterway_width(tags), 1), tags.get("name", ""),
            ])
            w_pr.addFeatures([feat])
            counts["waterlines"] += 1
            continue

        if tags.get("leisure") or tags.get("landuse") or tags.get("natural"):
            feat = polygon_feature(element, [
                osm_id, _tag(tags, "leisure"), _tag(tags, "landuse"), _tag(tags, "natural"),
                "", "", "", tags.get("name", ""),
            ])
            if feat is None:
                continue
            g_pr.addFeatures([feat])
            counts["greens"] += 1
            kind = _green_scatter_kind(tags)
            if kind and "trees" in enabled_datasets:
                placed = _scatter_trees(feat.geometry(), kind, t_pr, MAX_SCATTER_TREES - scattered_trees)
                scattered_trees += placed
                counts["trees"] += placed
                counts["trees_scattered"] += placed
            continue

        counts["skipped"] += 1

    # A Cancel after the last element no longer discards the finished result.
    for batch in batches:
        batch.flush()
    for layer in layers:
        layer.updateExtents()
    _hand_to_main_thread(layers)
    _progress(feedback, 100.0)

    return {
        "epsg": epsg_dest,
        "download": download,
        "notes": notes,
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
