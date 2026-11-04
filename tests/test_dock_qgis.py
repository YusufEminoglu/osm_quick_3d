"""The real OSM Quick 3D dock and plugin against a fake Overpass server on 127.0.0.1.

Run under a QGIS Python, from the directory that contains the plugin folder,
with QGIS_CUSTOM_CONFIG_PATH set to a throwaway directory (TRAPS 4.6):

    python-qgis-ltr.bat -m osm_quick_3d.tests.test_dock_qgis   # QGIS 3.44
    python-qgis.bat     -m osm_quick_3d.tests.test_dock_qgis   # QGIS 4.2

Offscreen: no window reaches the desktop. No request leaves this computer:
tests._offline guards every host before anything else runs, the Overpass
host is a fake server on 127.0.0.1, browser opens are only recorded.

Covers items 2 and 5 of DEVELOPMENT_SET_v0.52 (P1, P6, P11, P12, P13, P15).
Each check's docstring names what 0.51.1 did instead; every check fails on
0.51.1 for that reason.

Where contract C1 (``download_osm_for_area(..., status=, feedback=)``) is
missing from the code under test, the suite wraps the 0.51 download in a
stand-in and says so on an [INFO] line; the dock side is then still tested.
"""
from __future__ import annotations

import contextlib
import faulthandler
import inspect
import json
import os
import re
import sys
import tempfile
import threading
import time
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
_SYSTEM_FONTS = os.path.join(os.environ.get("WINDIR", r"C:\Windows"), "Fonts")
if os.path.isdir(_SYSTEM_FONTS):
    # Offscreen Qt has no fonts of its own (TRAPS 4.3); with the system's the
    # layout measures as it does on screen.
    os.environ.setdefault("QT_QPA_FONTDIR", _SYSTEM_FONTS)
if not os.environ.get("QGIS_CUSTOM_CONFIG_PATH"):
    os.environ["QGIS_CUSTOM_CONFIG_PATH"] = tempfile.mkdtemp(prefix="oq3d-dock-profile-")

PLUGIN_DIR = Path(__file__).resolve().parent.parent
if str(PLUGIN_DIR.parent) not in sys.path:
    sys.path.insert(0, str(PLUGIN_DIR.parent))

from qgis.core import (  # noqa: E402
    QgsApplication,
    QgsCoordinateReferenceSystem,
    QgsCoordinateTransform,
    QgsPointXY,
    QgsProject,
    QgsRectangle,
    QgsTask,
)

KONAK = (27.1428, 38.4361)        # lon, lat: Alsancak, Izmir
CANCELLED = "Download cancelled."
ERR_COLOR = "#a32525"
FORBIDDEN = (r"\bcanvas\b", r"\bepsg\b", r"\boverpass\b", r"\bmirror", r"\bjson\b", r"\bmanaged\b", r"\bkm2\b")
STANDINS = []
AFTER_RUN = []        # clean-up hooks a suite registers (temp folders)


class CheckFailed(AssertionError):
    pass


def check(condition, message):
    if not condition:
        raise CheckFailed(message)


def fail_if(problems):
    if problems:
        raise CheckFailed("; ".join(problems))


def pump(seconds=0.05):
    app = QgsApplication.instance()
    end = time.monotonic() + seconds
    while True:
        app.processEvents()
        if time.monotonic() >= end:
            return
        time.sleep(0.005)


def wait_for(predicate, timeout=5.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if predicate():
            return True
        pump(0.02)
    return bool(predicate())


def _enum(owner, scope, name):
    holder = getattr(owner, scope, None)
    return getattr(holder, name) if holder is not None and hasattr(holder, name) else getattr(owner, name)


def task_done(task) -> bool:
    if task is None:
        return True
    try:
        return task.status() in (_enum(QgsTask, "TaskStatus", "Complete"),
                                 _enum(QgsTask, "TaskStatus", "Terminated"))
    except RuntimeError:
        return True


# ---------------------------------------------------------------------------
# The offline guard (tests/_offline.py, written by OQ-NET; a stand-in until then)
# ---------------------------------------------------------------------------
def offline_module():
    try:
        from osm_quick_3d.tests import _offline
        return _offline
    except ImportError:
        STANDINS.append("tests._offline")
        return _OfflineStandIn()


class _OfflineStandIn:
    """The same guard as osm_3d_model's tests/_offline.py: a closed local port
    for every plugin host, a QGIS request preprocessor that refuses any
    non-local host, recorded (not performed) browser opens."""

    CLOSED = "http://127.0.0.1:9"

    def __init__(self):
        self._refused, self._opened, self._lock = [], [], threading.Lock()

    def refused(self):
        with self._lock:
            return list(self._refused)

    def opened(self):
        with self._lock:
            return list(self._opened)

    def _guard(self, request):
        from qgis.PyQt.QtCore import QUrl

        url = request.url()
        host = str(url.host()).lower()
        if str(url.scheme()).lower() not in ("http", "https", "ftp", "ws", "wss") \
                or host in ("127.0.0.1", "localhost", "::1"):
            return
        text = str(url.toString())
        with self._lock:
            self._refused.append(text)
        request.setUrl(QUrl(f"{self.CLOSED}/refused-by-the-offline-guard"))
        print(f"  [FAIL] offline guard refused a request for {host}: {text[:160]}", flush=True)

    def _record(self, target, *_a, **_k):
        with self._lock:
            self._opened.append(target.toString() if hasattr(target, "toString") else str(target))
        return True

    def isolate_network(self, guard_qgis=True):
        import webbrowser

        from qgis.core import QgsNetworkAccessManager
        from qgis.PyQt.QtGui import QDesktopServices

        from osm_quick_3d import main_plugin, osm_download

        osm_download.OVERPASS_ENDPOINTS = (f"{self.CLOSED}/api/interpreter",)
        main_plugin.OsmQuick3DPlugin.OSM_BASEMAP_URI = (
            "type=xyz&url=http://127.0.0.1:9/%7Bz%7D/%7Bx%7D/%7By%7D.png&zmax=19&zmin=0&crs=EPSG3857")
        self.isolate_download_cache()
        webbrowser.open = webbrowser.open_new = webbrowser.open_new_tab = self._record
        QDesktopServices.openUrl = staticmethod(self._record)
        if guard_qgis:
            QgsNetworkAccessManager.setRequestPreprocessor(self._guard)

    def isolate_download_cache(self):
        from osm_quick_3d import osm_download

        folder = tempfile.mkdtemp(prefix="oq3d-cache-")
        if hasattr(osm_download, "CACHE_DIR"):
            osm_download.CACHE_DIR = folder
        osm_download._cache_dir = lambda: folder
        return folder

    def forget_servers(self):
        from osm_quick_3d import osm_download

        forget = getattr(osm_download, "forget_hosts", None)
        if callable(forget):
            forget()


# ---------------------------------------------------------------------------
# Contract C1 stand-in (only where the code under test lacks it)
# ---------------------------------------------------------------------------
OLD_DOWNLOAD = []     # True when the 0.51 download (https only, no status=) is under test


def install_contract_standins():
    from osm_quick_3d import osm_download

    function = osm_download.download_osm_for_area
    if "status" in inspect.signature(function).parameters:
        return
    OLD_DOWNLOAD.append(True)
    error = osm_download.OsmDownloadError
    if not hasattr(osm_download, "OsmDownloadCancelled"):
        class OsmDownloadCancelled(error):
            def __init__(self, message=CANCELLED):
                super().__init__(message)
        osm_download.OsmDownloadCancelled = OsmDownloadCancelled
    cancelled = osm_download.OsmDownloadCancelled
    old_cancelled = getattr(osm_download, "OsmCancelledError", cancelled)
    old = function

    def download_osm_for_area(area_utm, epsg_dest, feedback=None, use_cache=True, datasets=None,
                              cancel_check=None, status=None):
        from qgis.core import QgsFeedback

        if isinstance(feedback, QgsFeedback) or feedback is None:
            qfb, say = feedback, status
        else:  # the 0.51 caller passes its status callable as feedback
            qfb, say = None, feedback
        check_cancel = cancel_check or (qfb.isCanceled if qfb is not None else None)
        try:
            result = old(area_utm, epsg_dest, feedback=say, use_cache=use_cache,
                         datasets=datasets, cancel_check=check_cancel)
        except old_cancelled as exc:
            raise cancelled(CANCELLED) from exc
        result.setdefault("notes", [])
        return result

    osm_download.download_osm_for_area = download_osm_for_area
    STANDINS.append("C1 download_osm_for_area(status=, feedback=)")


def _https_to_http_for_local(request):
    """The 0.51 download accepts https only; the fake server speaks http."""
    from qgis.PyQt.QtCore import QUrl

    url = request.url()
    if str(url.scheme()) == "https" and str(url.host()) in ("127.0.0.1", "localhost"):
        plain = QUrl(url)
        plain.setScheme("http")
        request.setUrl(plain)


# ---------------------------------------------------------------------------
# Fake Overpass (127.0.0.1 only)
# ---------------------------------------------------------------------------
def synth_town(min_lat, min_lon, max_lat, max_lon):
    """Buildings, streets and a park in the middle of the bbox."""
    els, nid = [], [1000]

    def nxt():
        nid[0] += 1
        return nid[0]

    dlat, dlon = max_lat - min_lat, max_lon - min_lon

    def pt(fy, fx):
        return {"lat": min_lat + dlat * fy, "lon": min_lon + dlon * fx}

    def rect(fy0, fx0, fy1, fx1):
        return [pt(fy0, fx0), pt(fy0, fx1), pt(fy1, fx1), pt(fy1, fx0), pt(fy0, fx0)]

    types = ["apartments", "house", "commercial", "retail", "school", "yes"]
    n = 6
    for i in range(n):
        for j in range(n):
            fy0, fx0 = 0.25 + 0.5 * i / n, 0.25 + 0.5 * j / n
            size = 0.5 / n * 0.6
            tags = {"building": types[(i * n + j) % len(types)]}
            if (i + j) % 3 == 0:
                tags["building:levels"] = str(2 + (i * j) % 7)
            els.append({"type": "way", "id": nxt(), "tags": tags,
                        "geometry": rect(fy0, fx0, fy0 + size, fx0 + size)})
    for k in range(n + 1):
        f = 0.25 + 0.5 * k / n - 0.5 / n * 0.2
        els.append({"type": "way", "id": nxt(), "tags": {"highway": "residential", "name": f"Test Street {k}"},
                    "geometry": [pt(f, 0.2), pt(f, 0.8)]})
    els.append({"type": "way", "id": nxt(), "tags": {"leisure": "park", "name": "Test Park"},
                "geometry": rect(0.45, 0.45, 0.55, 0.56)})
    return els


class _OverpassHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *args):
        return None

    def _send(self, status, body, ctype="application/json"):
        if isinstance(body, str):
            body = body.encode("utf-8")
        with contextlib.suppress(OSError):
            self.send_response(status)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Connection", "close")
            self.end_headers()
            self.wfile.write(body)

    def do_POST(self):  # noqa: N802 - http.server naming
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length) if length else b""
        query = parse_qs(body.decode("utf-8", "replace")).get("data", [""])[0]
        srv = self.server
        with srv.lock:
            srv.requests.append({"t": time.monotonic(), "query": query})
            mode, release, delay = srv.mode, srv.release, srv.delay
        if mode == "hang":
            release.wait(srv.hang_s)
        elif delay:
            time.sleep(delay)
        if mode == "504":
            self._send(504, "<html><body>Gateway Timeout</body></html>", "text/html")
            return
        match = re.search(r"\(\s*([-\d.]+)\s*,\s*([-\d.]+)\s*,\s*([-\d.]+)\s*,\s*([-\d.]+)\s*\)", query)
        elements = synth_town(*(float(v) for v in match.groups())) if match else []
        self._send(200, json.dumps({
            "version": 0.6, "generator": "osm_quick_3d test fake",
            "osm3s": {"timestamp_osm_base": "2026-11-04T08:00:00Z"}, "elements": elements}))


class FakeOverpass:
    def __init__(self):
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), _OverpassHandler)
        self.httpd.daemon_threads = True
        self.httpd.lock = threading.Lock()
        self.httpd.requests = []
        self.httpd.mode = "ok"
        self.httpd.delay = 0.0
        self.httpd.hang_s = 8.0
        self.httpd.release = threading.Event()
        self.port = self.httpd.server_address[1]
        threading.Thread(target=self.httpd.serve_forever, daemon=True, name="fake-overpass").start()

    @property
    def url(self):
        scheme = "https" if OLD_DOWNLOAD else "http"
        return f"{scheme}://127.0.0.1:{self.port}/api/interpreter"

    @property
    def requests(self):
        with self.httpd.lock:
            return list(self.httpd.requests)

    def reset(self, mode, delay=0.0):
        self.release_all()
        with self.httpd.lock:
            self.httpd.requests = []
            self.httpd.mode = mode
            self.httpd.delay = float(delay)
            self.httpd.release = threading.Event()

    def release_all(self):
        with self.httpd.lock:
            self.httpd.release.set()


# ---------------------------------------------------------------------------
# A QGIS-shaped window and recorders
# ---------------------------------------------------------------------------
class MessageBar:
    def __init__(self):
        self.notes = []

    def _push(self, level, *args):
        title, text = (str(args[0]), str(args[1])) if len(args) > 1 else ("", str(args[0]) if args else "")
        self.notes.append((level, title, text))

    def pushSuccess(self, *args, **_kw):  # noqa: N802 - QGIS naming
        self._push("success", *args)

    def pushInfo(self, *args, **_kw):  # noqa: N802
        self._push("info", *args)

    def pushWarning(self, *args, **_kw):  # noqa: N802
        self._push("warning", *args)

    def pushCritical(self, *args, **_kw):  # noqa: N802
        self._push("critical", *args)

    def pushMessage(self, *args, **_kw):  # noqa: N802
        self._push("message", *args)


class Iface:
    def __init__(self, window, canvas, bar):
        self._window, self._canvas, self._bar = window, canvas, bar
        self.active_layer = None
        self.docks = []

    def mainWindow(self):  # noqa: N802
        return self._window

    def mapCanvas(self):  # noqa: N802
        return self._canvas

    def messageBar(self):  # noqa: N802
        return self._bar

    def activeLayer(self):  # noqa: N802
        return self.active_layer

    def addToolBarIcon(self, action):  # noqa: N802
        pass

    def removeToolBarIcon(self, action):  # noqa: N802
        pass

    def addPluginToMenu(self, menu, action):  # noqa: N802
        pass

    def removePluginMenu(self, menu, action):  # noqa: N802
        pass

    def addDockWidget(self, area, dock):  # noqa: N802
        self._window.addDockWidget(area, dock)
        self.docks.append(dock)

    def removeDockWidget(self, dock):  # noqa: N802
        self._window.removeDockWidget(dock)
        if dock in self.docks:
            self.docks.remove(dock)

    def createNewMapCanvas3D(self, name):  # noqa: N802
        return None

    def mapCanvases3D(self):  # noqa: N802
        return []

    def closeMapCanvas3D(self, name):  # noqa: N802
        pass


class BoxRecorder:
    """Every QMessageBox the plugin raises is recorded and answered, never shown."""

    def __init__(self):
        self.shown = []

    def install(self):
        from qgis.PyQt.QtWidgets import QMessageBox

        recorder = self

        def exec_(box, *_args, **_kwargs):
            recorder.shown.append(("box", box.text()))
            return 0

        def static(kind):
            def show(_parent, _title, text, *_args, **_kwargs):
                recorder.shown.append((kind, str(text)))
                return QMessageBox.StandardButton.Ok
            return staticmethod(show)

        QMessageBox.exec = exec_
        QMessageBox.exec_ = exec_
        for kind in ("critical", "warning", "information", "question"):
            setattr(QMessageBox, kind, static(kind))


class LogRecorder:
    """Records what the plugin writes to the QGIS log (its own tab), then logs it.

    Wraps QgsMessageLog.logMessage: QGIS 4.2 offscreen does not deliver
    messageReceived to Python, so the signal alone would see nothing.
    """

    def __init__(self):
        from qgis.core import QgsMessageLog

        self.lines = []
        original = QgsMessageLog.logMessage
        recorder = self

        def log_message(message, tag="", *args, **kwargs):
            if tag == "OSM Quick 3D":
                recorder.lines.append(str(message))
            return original(message, tag, *args, **kwargs)

        QgsMessageLog.logMessage = staticmethod(log_message)


def unclipped(widget, top) -> bool:
    """True when no scroll area between ``widget`` and ``top`` cuts it off."""
    from qgis.PyQt.QtCore import QPoint
    from qgis.PyQt.QtWidgets import QAbstractScrollArea

    parent = widget.parentWidget()
    while parent is not None and parent is not top:
        area = parent.parentWidget()
        if isinstance(area, QAbstractScrollArea) and area.viewport() is parent:
            corner = widget.mapTo(parent, QPoint(0, 0))
            if not (0 <= corner.y() and corner.y() + widget.height() <= parent.height()):
                return False
        parent = parent.parentWidget()
    return True


class Rig:
    def __init__(self):
        from qgis.gui import QgsMapCanvas
        from qgis.PyQt.QtWidgets import QMainWindow

        self.window = QMainWindow()
        self.window.resize(1366, 728)        # a 1366x768 laptop minus the taskbar
        self.canvas = QgsMapCanvas(self.window)
        self.window.setCentralWidget(self.canvas)
        self.canvas.setDestinationCrs(QgsCoordinateReferenceSystem("EPSG:3857"))
        self.window.show()
        self.bar = MessageBar()
        self.iface = Iface(self.window, self.canvas, self.bar)
        self.boxes = BoxRecorder()
        self.boxes.install()
        self.log = LogRecorder()
        self.overpass_fake = FakeOverpass()
        self.plugins = []
        pump(0.2)

    def new_plugin(self):
        from osm_quick_3d import classFactory
        from qgis.core import QgsSettings

        settings = QgsSettings()
        for key, value in (("open3d", False), ("use_cache", False), ("save_gpkg", False),
                           ("area_source", "canvas"), ("shape", "rectangle"), ("max_km2", 6.0)):
            settings.setValue(f"osm_quick_3d/{key}", value)
        plugin = classFactory(self.iface)
        plugin.initGui()
        self.plugins.append(plugin)
        return plugin

    def open_dock(self):
        plugin = self.new_plugin()
        plugin.show_dock()
        pump(0.3)
        return plugin, plugin.dock

    def set_view(self, lon=KONAK[0], lat=KONAK[1], width_m=900, height_m=700):
        to_map = QgsCoordinateTransform(QgsCoordinateReferenceSystem("EPSG:4326"),
                                        self.canvas.mapSettings().destinationCrs(), QgsProject.instance())
        centre = to_map.transform(QgsPointXY(lon, lat))
        self.canvas.setExtent(QgsRectangle(centre.x() - width_m / 2, centre.y() - height_m / 2,
                                           centre.x() + width_m / 2, centre.y() + height_m / 2))
        self.canvas.refresh()
        pump(0.1)

    def clear_view(self):
        """An empty project: the canvas has no extent at all."""
        from qgis.gui import QgsMapCanvas

        old = self.canvas
        self.canvas = QgsMapCanvas(self.window)
        self.canvas.setDestinationCrs(QgsCoordinateReferenceSystem("EPSG:3857"))
        self.window.setCentralWidget(self.canvas)
        self.iface._canvas = self.canvas
        old.deleteLater()
        pump(0.1)

    def overpass(self, mode="ok", delay=0.0):
        from osm_quick_3d import osm_download

        self.overpass_fake.reset(mode, delay)
        osm_download.OVERPASS_ENDPOINTS = (self.overpass_fake.url,)
        for name in ("TILE_PAUSE_S", "MIRROR_RETRY_DELAY_S"):
            if hasattr(osm_download, name):
                setattr(osm_download, name, 0.0)
        if hasattr(OFFLINE, "forget_servers"):
            OFFLINE.forget_servers()
        return self.overpass_fake

    def run(self, dock):
        dock.cb_use_cache.setChecked(False)
        dock.cb_open3d.setChecked(False)
        dock.run_btn.click()

    def status(self, dock) -> str:
        return dock.status.text()

    def summary(self, dock) -> str:
        label = getattr(dock, "summary", None)
        text = label.text() if label is not None and hasattr(label, "text") and label.isVisible() else ""
        return text

    def osm_layers(self):
        return [lyr for lyr in QgsProject.instance().mapLayers().values() if lyr.name().startswith("OSM ")]

    # -- between checks ------------------------------------------------------
    def stop_runs(self):
        for plugin in self.plugins:
            task = getattr(plugin, "_task", None)
            if task is not None:
                with contextlib.suppress(RuntimeError):
                    task.cancel()
        self.overpass_fake.release_all()
        wait_for(lambda: all(getattr(p, "_task", None) is None for p in self.plugins), 20)

    def finish(self):
        from osm_quick_3d import osm_download

        self.stop_runs()
        for plugin in self.plugins:
            with contextlib.suppress(Exception):
                plugin.unload()
        self.plugins = []
        wait_for(lambda: QgsApplication.taskManager().countActiveTasks() == 0, 20)
        QgsProject.instance().clear()
        self.iface.active_layer = None
        self.boxes.shown.clear()
        self.bar.notes.clear()
        self.log.lines.clear()
        osm_download.OVERPASS_ENDPOINTS = ("http://127.0.0.1:9/api/interpreter",)
        if self.canvas.extent().isEmpty():
            self.set_view()
        pump(0.2)


def is_idle(plugin, dock) -> bool:
    task = getattr(plugin, "_task", None)
    return task is None and not dock.cancel_btn.isVisible()


def start_run(rig, mode="ok", delay=0.0, wait_request=True):
    plugin, dock = rig.open_dock()
    rig.set_view()
    overpass = rig.overpass(mode, delay)
    pump(0.2)
    rig.run(dock)
    if wait_request:
        check(wait_for(lambda: overpass.requests, 10),
              f"the download never reached the fake server (status {dock.status.text()!r})")
    return plugin, dock, overpass


# ---------------------------------------------------------------------------
# Checks
# ---------------------------------------------------------------------------
def check_empty_project_hint(rig):
    """P12: an empty project turns Run off and points at "Add OSM basemap".

    0.51.1 left Run on over a Null extent and answered a click with a modal
    "Empty area" box that never mentioned the basemap button.
    """
    rig.clear_view()
    plugin, dock = rig.open_dock()
    problems = []
    if dock.run_btn.isEnabled():
        problems.append("Run is enabled although the map is empty")
    texts = " ".join([dock.status.text(), dock.canvas_area_label.text(), dock.run_btn.toolTip()])
    if "Add OSM basemap" not in texts:
        problems.append(f"nothing points at “Add OSM basemap”: {texts[:160]!r}")
    dock.run_btn.click()
    pump(0.2)
    if rig.boxes.shown:
        problems.append(f"a box appeared: {rig.boxes.shown[0]!r}")
    rig.set_view()
    if not dock.run_btn.isEnabled():
        problems.append("Run stayed off after the map got an area")
    fail_if(problems)
    return f"Run off with {dock.canvas_area_label.text()!r}; on again once the map has an area"


def check_run_visible_at_1366x728(rig):
    """P11: Run, Cancel's place and the status are on screen at 1366x728.

    0.51.1 put Run at y = 1066 inside a 611 px scroll viewport: below the fold.
    """
    plugin, dock = rig.open_dock()
    rig.set_view()
    rig.window.resize(1366, 728)
    pump(0.4)
    problems = []
    window_rect = rig.window.rect()
    for name, widget in (("Run", dock.run_btn), ("status", dock.status)):
        top_left = widget.mapTo(rig.window, widget.rect().topLeft())
        bottom = top_left.y() + widget.height()
        if not widget.isVisible():
            problems.append(f"{name} is hidden")
        elif not unclipped(widget, dock):
            problems.append(f"{name} is scrolled out of view")
        elif bottom > window_rect.height() or top_left.y() < 0:
            problems.append(f"{name} ends at y={bottom}, outside the {window_rect.height()} px window")
    fail_if(problems)
    p = dock.run_btn.mapTo(rig.window, dock.run_btn.rect().topLeft())
    return f"Run at y={p.y()}..{p.y() + dock.run_btn.height()} of {window_rect.height()} px, not in the scroll area"


def check_status_moves_and_ui_stays_responsive(rig):
    """See _status_moves; the QGIS network timeout is pinned so the 6 s answer
    always fits the transfer limit, whatever the runner's profile sets."""
    from qgis.core import QgsNetworkAccessManager

    previous = QgsNetworkAccessManager.timeout()
    QgsNetworkAccessManager.setTimeout(30000)
    try:
        return _status_moves(rig)
    finally:
        QgsNetworkAccessManager.setTimeout(previous)


def _status_moves(rig):
    """P1: during a slow answer QGIS keeps working and the status counts seconds.

    0.51.1 downloaded on the GUI thread: a timer due after 0.3 s fired only
    when the answer came (6 s later) and the status read "Querying …" all along.
    """
    plugin, dock = rig.open_dock()
    rig.set_view()
    overpass = rig.overpass("ok", delay=6.0)
    pump(0.2)
    fired, params = [], []
    dock.runRequested.connect(params.append)
    armed = time.monotonic()
    from qgis.PyQt.QtCore import QTimer
    QTimer.singleShot(300, lambda: fired.append(time.monotonic() - armed))
    rig.run(dock)
    seen = [dock.status.text()]
    end = time.monotonic() + 5.0
    second_run_requests = None
    while time.monotonic() < end:
        pump(0.1)
        if dock.status.text() != seen[-1]:
            seen.append(dock.status.text())
        if second_run_requests is None and time.monotonic() > end - 3.0:
            dock.run_btn.click()                        # a second run while one runs
            if params:
                dock.runRequested.emit(dict(params[0]))
            second_run_requests = True
    problems = []
    if not fired or fired[0] > 0.8:
        problems.append(f"a 0.3 s timer fired after {fired[0] if fired else 'never'} s: QGIS was frozen")
    if len(seen) < 3:
        problems.append(f"the status changed {len(seen) - 1} times in 5 s: {seen[-2:]}")
    if not any(re.search(r"\b\d+ s\b|\(\d+ s\)", t) for t in seen):
        problems.append(f"no status counted the seconds: {seen[-2:]}")
    if dock.run_btn.isEnabled():
        problems.append("Run stayed enabled during the download")
    ended = wait_for(lambda: is_idle(plugin, dock), 20)
    if not ended:
        problems.append("the run did not end")
    if len(overpass.requests) != 1:
        problems.append(f"{len(overpass.requests)} requests reached the server; a second run was started")
    summary = rig.summary(dock)
    if not re.search(r"\d+ buildings", summary):
        problems.append(f"no summary with counts after the run: {summary!r} / {dock.status.text()!r}")
    if not rig.osm_layers():
        problems.append("no layers were added")
    fail_if(problems)
    return (f"timer late by {fired[0] - 0.3:.2f} s, {len(seen)} status lines in 5 s "
            f"(last {seen[-1]!r}); summary {summary[:70]!r}")


def check_cancel_within_two_seconds(rig):
    """P1/P15: Cancel ends a hanging download within 2 s, neutrally, adding nothing.

    0.51.1 checked Cancel only between tiles on the frozen GUI thread: the
    click was handled after the answer came, and then threw that answer away.
    """
    plugin, dock, overpass = start_run(rig, mode="hang")
    pump(0.3)
    has_button = dock.cancel_btn.isVisible() and dock.cancel_btn.isEnabled()
    clicked = time.monotonic()
    if has_button:
        dock.cancel_btn.click()
    ended = wait_for(lambda: is_idle(plugin, dock), 2.0)
    elapsed = time.monotonic() - clicked
    status = dock.status.text()
    rig.overpass_fake.release_all()
    pump(1.0)
    problems = []
    if not has_button:
        problems.append("no Cancel button during the download")
    if not ended:
        problems.append(f"the dock was still busy {elapsed:.1f} s after Cancel")
    if status != CANCELLED:
        problems.append(f"the status reads {status!r}, not {CANCELLED!r}")
    if ERR_COLOR in dock.status.styleSheet():
        problems.append("the cancelled run is shown in the error colour")
    if rig.boxes.shown:
        problems.append(f"a box appeared: {rig.boxes.shown[0]!r}")
    if rig.osm_layers():
        problems.append("layers were added after Cancel")
    if not dock.run_btn.isEnabled():
        problems.append("Run stayed off after Cancel")
    fail_if(problems)
    return f"idle {elapsed:.2f} s after Cancel with {status!r}, nothing added"


def check_abandoned_download_is_forgotten(rig):
    """A download let go after Cancel is released once it really ends.

    The first 0.52 build cleared its callback when abandoning it, so it was
    never taken out of the plugin's list: every abandoned run stayed held.
    The download here ignores Cancel until the check lets it go, so it is
    always abandoned first.
    """
    from osm_quick_3d import osm_download

    real = osm_download.download_osm_for_area
    let_go, entered = threading.Event(), threading.Event()

    def stuck_download(*_args, **_kwargs):
        entered.set()
        let_go.wait(20)
        raise osm_download.OsmDownloadCancelled()

    osm_download.download_osm_for_area = stuck_download
    try:
        plugin, dock, overpass = start_run(rig, mode="ok", wait_request=False)
        task = getattr(plugin, "_task", None)
        check(wait_for(entered.is_set, 10), "the download task never started")
        check(dock.cancel_btn.isVisible(), "no Cancel button during the download")
        dock.cancel_btn.click()
        idle = wait_for(lambda: is_idle(plugin, dock), 3.0)
        retired = getattr(plugin, "_retired_tasks", [])
        held_while_running = task in retired
        let_go.set()
        ended = wait_for(lambda: task_done(task), 15)
    finally:
        let_go.set()
        osm_download.download_osm_for_area = real
    forgotten = wait_for(lambda: task not in retired, 5)
    problems = []
    if not idle:
        problems.append("the dock was not idle after Cancel")
    if not held_while_running:
        problems.append("the abandoned download was not kept referenced while it ran")
    if not ended:
        problems.append("the abandoned download did not end")
    if not forgotten:
        problems.append(f"{len(retired)} abandoned download(s) still held after they ended")
    fail_if(problems)
    return "held while running, released once it ended"


def check_late_cancel_keeps_the_result(rig):
    """P1: Cancel pressed after the answer arrived keeps the answer.

    0.51.1 checked Cancel after merging and threw the finished download away.
    """
    plugin, dock, overpass = start_run(rig, mode="ok", wait_request=False)
    task = getattr(plugin, "_task", None)       # no events processed since the click
    check(task is not None and hasattr(task, "result_data"),
          "the plugin runs no download task (the download blocks the GUI thread)")
    end = time.monotonic() + 15
    while getattr(task, "result_data", None) is None and time.monotonic() < end:
        time.sleep(0.01)                     # no event processing: finished() stays queued
    check(task.result_data is not None, "the download did not finish within 15 s")
    if dock.cancel_btn.isVisible():
        dock.cancel_btn.click()
    wait_for(lambda: is_idle(plugin, dock), 15)
    problems = []
    if not rig.osm_layers():
        problems.append(f"the arrived result was discarded: {dock.status.text()!r}")
    if dock.status.text() == CANCELLED:
        problems.append("the status says cancelled although the result was kept")
    fail_if(problems)
    return f"{len(rig.osm_layers())} layers kept; status {dock.status.text()!r}"


def check_failure_in_dock_not_a_box(rig):
    """P6: a failed download is a short plain sentence in the dock, details in the log.

    0.51.1 raised a modal Critical box and put 300 characters of raw Qt text
    ("Error transferring http://… - server replied: Gateway Timeout") in the status.
    """
    plugin, dock, overpass = start_run(rig, mode="504")
    wait_for(lambda: is_idle(plugin, dock), 30)
    status = dock.status.text()
    problems = []
    if rig.boxes.shown:
        problems.append(f"a modal box appeared: {rig.boxes.shown[0][1][:90]!r}")
    if ERR_COLOR not in dock.status.styleSheet():
        problems.append("the failure is not shown in the error colour")
    if len(status) > 240:
        problems.append(f"the status is {len(status)} characters long")
    for raw in ("Error transferring", "http://", "https://", "server replied", "JSON"):
        if raw in status:
            problems.append(f"raw text {raw!r} in the status: {status[:120]!r}")
    if not rig.log.lines:
        problems.append("nothing was written to the QGIS log")
    # A missing selection is a dock message too, not a box.
    rig.boxes.shown.clear()
    dock.area_source.setCurrentIndex(dock.area_source.findData("selection"))
    dock.run_btn.click()
    pump(0.3)
    if rig.boxes.shown:
        problems.append(f"no selection raised a box: {rig.boxes.shown[0][1][:80]!r}")
    if ERR_COLOR not in dock.status.styleSheet():
        problems.append(f"no selection is not reported in the dock: {dock.status.text()!r}")
    dock.area_source.setCurrentIndex(dock.area_source.findData("canvas"))
    fail_if(problems)
    return f"status {status!r}; {len(rig.log.lines)} log lines"


def check_partial_result_says_so(rig):
    """C1 notes: a result with missing parts says so in the dock, and the
    summary is not overwritten by later status lines.

    0.51.1 had no notes and wrote every later message ("No managed 3D scene
    could be created.") over the summary.
    """
    from osm_quick_3d import osm_download

    note = "2 of 4 parts of the area are missing (127.0.0.1 did not answer); run again to fill them."
    real = osm_download.download_osm_for_area

    def with_notes(*args, **kwargs):
        result = real(*args, **kwargs)
        result["notes"] = [note]
        return result

    osm_download.download_osm_for_area = with_notes
    try:
        plugin, dock, overpass = start_run(rig, mode="ok")
        wait_for(lambda: is_idle(plugin, dock), 20)
    finally:
        osm_download.download_osm_for_area = real
    summary = rig.summary(dock)
    shown = f"{dock.status.text()} {summary}"
    problems = []
    if note not in shown:
        problems.append(f"the note is not in the dock: {shown[:200]!r}")
    if not re.search(r"\d+ buildings", summary):
        problems.append(f"no counts in the summary: {summary!r}")
    if "partial" not in shown.lower() and "missing" not in dock.status.text().lower():
        problems.append("the dock does not say the result is partial")
    # A later message must leave the summary alone.
    dock.set_status("3D view updated.")
    if rig.summary(dock) != summary:
        problems.append("a later status line replaced the summary")
    fail_if(problems)
    return f"status {dock.status.text()!r}; summary keeps the note"


def check_no_dead_3d_toggles(rig):
    """P13: no 3D shadow or ambient-occlusion switch that does nothing.

    0.51.1 offered "3D sun shadows" and "Ambient occlusion (SSAO)"; the calls
    behind them do not exist on Qgs3DMapSettings in 3.44 or 4.2.
    """
    from qgis.PyQt.QtWidgets import QCheckBox

    import qgis._3d as q3

    plugin, dock = rig.open_dock()
    settings_cls = q3.Qgs3DMapSettings
    shadows_api = hasattr(settings_cls, "setShadowSettings") or hasattr(settings_cls, "setShadowRenderingSettings")
    ao_api = hasattr(settings_cls, "setAmbientOcclusionSettings")
    dead = []
    for box in dock.findChildren(QCheckBox):
        text = box.text().lower()
        if "3d" in text and "shadow" in text and not shadows_api:
            dead.append(box.text())
        if ("ambient occlusion" in text or "ssao" in text) and not ao_api:
            dead.append(box.text())
    check(not dead, f"switches without a QGIS API behind them: {dead}")
    return f"no dead switches (shadow API {shadows_api}, ambient-occlusion API {ao_api})"


def _visible_texts(widget):
    from qgis.PyQt.QtWidgets import QAbstractButton, QComboBox, QGroupBox, QLabel

    texts = []
    for child in widget.findChildren(QLabel) + widget.findChildren(QAbstractButton):
        texts.append(child.text())
        texts.append(child.toolTip())
    for box in widget.findChildren(QGroupBox):
        texts.append(box.title())
    for combo in widget.findChildren(QComboBox):
        texts.extend(combo.itemText(i) for i in range(combo.count()))
        texts.append(combo.toolTip())
    return [t for t in texts if t]


def check_plain_words(rig):
    """P6: the Build tab and the status speak plainly (no canvas, EPSG, Overpass…).

    0.51.1 said "Visible map extent (canvas)", "Canvas extent", "Overpass
    tiles", "km2", "managed 3D Map View" and put EPSG codes in the summary.
    """
    plugin, dock, overpass = start_run(rig, mode="ok")
    wait_for(lambda: is_idle(plugin, dock), 20)
    texts = _visible_texts(dock.download_tab) + [dock.status.text(), rig.summary(dock)]
    found = sorted({f"{m.group(0)} in {t[:60]!r}" for t in texts for pattern in FORBIDDEN
                    for m in [re.search(pattern, t, re.IGNORECASE)] if m})
    check(not found, f"jargon in the dock: {found}")
    return f"{len(texts)} texts checked"


def check_unload_removes_dock_and_disconnects(rig):
    """P15: unload cancels the run, disconnects from the canvas and deletes the dock.

    0.51.1 only closed the dock: it stayed alive, connected to extentsChanged.
    The first 0.52 build cancelled before tearing the dock down, which could
    deadlock the GUI thread against the worker's transfer teardown.
    """
    from qgis.PyQt import sip
    from qgis.PyQt.QtCore import QCoreApplication, QEvent

    rig.set_view()
    canvas = rig.canvas
    project = QgsProject.instance()
    QCoreApplication.sendPostedEvents(None, _enum(QEvent, "Type", "DeferredDelete"))
    pump(0.1)
    before = (canvas.receivers(canvas.extentsChanged), project.receivers(project.layersAdded))
    plugin, dock, overpass = start_run(rig, mode="hang")
    task = getattr(plugin, "_task", None)
    unload_returned, cancelled_during_unload = [], []
    feedback = getattr(task, "feedback", None)
    if feedback is not None:
        feedback.canceled.connect(lambda: cancelled_during_unload.append(not unload_returned))
    plugin.unload()
    unload_returned.append(True)
    rig.plugins.remove(plugin)
    QCoreApplication.sendPostedEvents(None, _enum(QEvent, "Type", "DeferredDelete"))
    pump(0.3)
    ended = wait_for(lambda: task_done(task), 3.0)
    rig.overpass_fake.release_all()
    pump(1.0)
    after = (canvas.receivers(canvas.extentsChanged), project.receivers(project.layersAdded))
    problems = []
    if not sip.isdeleted(dock):
        problems.append("the dock still exists after unload")
    if after[0] > before[0]:
        problems.append(f"extentsChanged has {after[0]} receivers, {before[0]} before the plugin")
    if after[1] > before[1]:
        problems.append(f"layersAdded has {after[1]} receivers, {before[1]} before the plugin")
    if task is not None and not ended:
        problems.append("the download kept running after unload")
    if any(cancelled_during_unload):
        # The worker then tears the transfer down (needing the GIL under a Qt
        # signal/slot lock) while unload tears the dock down with the GIL held:
        # a rare deadlock of the GUI thread in dock.setParent(None).
        problems.append("unload cancelled the download while still tearing the dock down; "
                        "the cancel must reach the task from the event loop, after unload")
    if rig.osm_layers():
        problems.append("layers were added after unload")
    canvas.setExtent(QgsRectangle(canvas.extent()))   # must not reach a deleted dock
    pump(0.2)
    fail_if(problems)
    return "dock deleted, canvas and project receivers back to their count before the plugin"


CHECKS = (
    check_empty_project_hint,
    check_run_visible_at_1366x728,
    check_status_moves_and_ui_stays_responsive,
    check_cancel_within_two_seconds,
    check_abandoned_download_is_forgotten,
    check_late_cancel_keeps_the_result,
    check_failure_in_dock_not_a_box,
    check_partial_result_says_so,
    check_no_dead_3d_toggles,
    check_plain_words,
    check_unload_removes_dock_and_disconnects,
)

OFFLINE = None


def prepare():
    """Guard the network, install stand-ins; return the rig."""
    global OFFLINE
    from qgis.core import QgsNetworkAccessManager

    OFFLINE = offline_module()
    OFFLINE.isolate_network()
    install_contract_standins()
    if OLD_DOWNLOAD:
        QgsNetworkAccessManager.setRequestPreprocessor(_https_to_http_for_local)
    if STANDINS:
        print(f"  [INFO] contract stand-ins in use: {', '.join(STANDINS)}")
    return Rig()


def run_checks(title, checks) -> bool:
    print("=" * 70)
    print(f" osm_quick_3d - {title} (fake server, offscreen)")
    print("=" * 70)
    started = time.monotonic()
    rig = prepare()
    passed = 0
    for function in checks:
        name = function.__name__[len("check_"):].replace("_", " ")
        try:
            detail = function(rig)
            passed += 1
            print(f"  [PASS] {name} - {detail}", flush=True)
        except CheckFailed as exc:
            print(f"  [FAIL] {name} - {exc}", flush=True)
        except Exception as exc:
            print(f"  [FAIL] {name} - {type(exc).__name__}: {exc}", flush=True)
            traceback.print_exc(limit=4)
        finally:
            with contextlib.suppress(Exception):
                rig.finish()
    for hook in AFTER_RUN:
        with contextlib.suppress(Exception):
            hook()
    if hasattr(OFFLINE, "cleanup"):
        with contextlib.suppress(Exception):
            OFFLINE.cleanup()
    refused = OFFLINE.refused()
    if refused:
        print(f"  [FAIL] public requests were attempted: {refused[:3]}")
    print("-" * 70)
    total = len(checks)
    ok = passed == total and not refused
    print(f"  {passed}/{total} checks passed in {time.monotonic() - started:.0f} s - {'PASS' if ok else 'FAIL'}")
    return ok


def main(title="dock, download task and messages", checks=CHECKS):
    # A hang dumps every thread's stack; OQ3D_HANG_DUMP_S shortens the wait.
    faulthandler.dump_traceback_later(int(os.environ.get("OQ3D_HANG_DUMP_S") or 900), exit=True)
    for stream in (sys.stdout, sys.stderr):
        with contextlib.suppress(AttributeError, ValueError):
            stream.reconfigure(errors="backslashreplace")
    app = QgsApplication([], True)
    app.initQgis()
    from qgis.PyQt.QtGui import QFont

    app.setFont(QFont("Segoe UI", 9))
    try:
        ok = run_checks(title, checks)
    except Exception:
        traceback.print_exc()
        ok = False
    sys.stdout.flush()
    sys.stderr.flush()
    os._exit(0 if ok else 1)


if __name__ == "__main__":
    main()
